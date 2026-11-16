"""One monitoring pass: fetch -> detect -> enrich -> alert -> persist -> heartbeat."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import messages
from .filters import describe, wanted
from .config import CRUISE_DESTINATION_ID, Config
from .detect import AVAILABLE, REMOVED, Event, commit_event, diff
from .extract import enrich
from .source import FetchError, Fetcher, fetch_detail, fetch_rewards, http_get_json
from .state import load_state, save_state
from .telegram import Telegram

log = logging.getLogger(__name__)
DETAIL_BACKFILL_PER_RUN = 25
DETAIL_MAX_AGE = timedelta(days=7)


def ping_healthcheck(url: str, suffix: str = "") -> None:
    if not url:
        return
    try:
        with urlopen(Request(url.rstrip("/") + suffix, headers={"User-Agent": "cruise-reward-alerts"}), timeout=10):
            pass
    except Exception as e:  # watchdog pings must never break the run
        log.warning("Healthcheck ping failed: %s", e)


def _sweep_due(state: dict, config: Config, now: datetime) -> bool:
    if not config.sweep_destinations:
        return False
    last = state.get("last_sweep_at")
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=config.sweep_interval_minutes)
    except ValueError:
        return True


def _ensure_detail(rec: dict, fetcher: Fetcher, now: datetime, force: bool = False) -> None:
    d = rec.get("detail")
    fresh = False
    if d and d.get("_offer_id") == rec.get("offer_id"):
        try:
            fresh = now - datetime.fromisoformat(d.get("_fetched_at")) < DETAIL_MAX_AGE
        except (TypeError, ValueError):
            fresh = False
    if fresh and not force:
        return
    raw = fetch_detail(rec.get("offer_id"), fetcher)
    if raw is None and d:
        return  # keep the older detail rather than lose it
    info = enrich(rec, raw)
    info["_offer_id"] = rec.get("offer_id")
    info["_fetched_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    rec["detail"] = info


class Monitor:
    def __init__(
        self,
        config: Config,
        telegram: Telegram | None = None,
        fetcher: Fetcher = http_get_json,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.config = config
        self.tg = telegram or Telegram(config.telegram_token, config.telegram_chat_id, dry_run=config.dry_run)
        self.fetcher = fetcher
        self.clock = clock
        self.report: list[str] = []

    def note(self, msg: str) -> None:
        log.info(msg)
        self.report.append(msg)

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        cfg, now = self.config, self.clock()
        state = load_state(cfg.state_file)
        health = state["health"]

        destinations = [CRUISE_DESTINATION_ID]
        sweeping = _sweep_due(state, cfg, now)
        if sweeping:
            destinations += [d for d in cfg.sweep_destinations if d != CRUISE_DESTINATION_ID]

        # 1. Fetch ---------------------------------------------------------
        try:
            current, fetched_ok, sweep_errors = fetch_rewards(destinations, self.fetcher)
        except FetchError as e:
            health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
            health["last_error"] = str(e)[:300]
            self.note(f"FETCH FAILED ({health['consecutive_failures']} in a row): {e}")
            if (health["consecutive_failures"] >= cfg.failure_alert_threshold
                    and not health.get("failure_alert_sent")):
                if self.tg.send(
                    "⚠️ <b>Cruise monitor problem</b>\n"
                    f"The myVIP rewards API has failed {health['consecutive_failures']} checks in a row, "
                    "so alerts are paused until it recovers. You'll get a message when it does.\n\n"
                    f"<code>{messages.e(str(e)[:300])}</code>"
                ):
                    health["failure_alert_sent"] = True
            save_state(cfg.state_file, state)
            return {"ok": False, "error": str(e)}

        if health.get("failure_alert_sent"):
            self.tg.send(
                "✅ <b>Cruise monitor recovered</b>\nThe myVIP API is responding again; "
                "monitoring has resumed.", silent=True)
        health.update(consecutive_failures=0, failure_alert_sent=False, last_error=None)
        if sweeping:
            state["last_sweep_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        for dest, err in sweep_errors.items():
            self.note(f"Sweep destination {dest} failed (non-fatal): {err}")

        primary_count = sum(1 for r in current.values() if r["destination"] == CRUISE_DESTINATION_ID)
        self.note(f"Fetched {len(current)} cruise rewards ({primary_count} in Cruise feed; "
                  f"destinations ok: {fetched_ok})")

        # 2. Guard against an empty Cruise feed (API change / outage page) ---
        if primary_count == 0:
            health["empty_feed_checks"] = int(health.get("empty_feed_checks") or 0) + 1
            if (health["empty_feed_checks"] >= cfg.failure_alert_threshold
                    and not health.get("empty_feed_alert_sent")):
                if self.tg.send(
                    "⚠️ <b>Cruise monitor warning</b>\nThe myVIP Cruise category has returned "
                    f"<b>zero</b> rewards for {health['empty_feed_checks']} checks in a row. Either "
                    "the store is genuinely empty or the API has changed. Have a look in the app."
                ):
                    health["empty_feed_alert_sent"] = True
        else:
            if health.get("empty_feed_alert_sent"):
                self.tg.send("✅ <b>Cruise rewards are showing again</b> in the myVIP feed.", silent=True)
            health.update(empty_feed_checks=0, empty_feed_alert_sent=False)

        # 3. First run: baseline, no flood of alerts --------------------------
        first_run = not state.get("initialized_at")
        result = diff(state, current, fetched_ok, cfg, now)
        state = result.state
        for line in result.silent_changes:
            log.debug(line)

        if first_run:
            for ev in result.events:  # nothing is "new" on the very first run
                commit_event(state, Event(ev.type, ev.award_id, ev.record, ev.previous, alert=False), now, True)
            state["initialized_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
            records = [r for r in state["rewards"].values() if r.get("status") != REMOVED]
            for rec in records[:DETAIL_BACKFILL_PER_RUN]:
                _ensure_detail(rec, self.fetcher, now)
            self.note(f"First run: baseline of {len(records)} rewards saved")
            if cfg.first_run_summary and primary_count:
                matching = [r for r in records if wanted(r, cfg)]
                self.tg.send(messages.startup_message(matching, now, cfg.timezone, describe(cfg), len(records)),
                             silent=True)
        else:
            self._deliver(state, result.events, now)

        # 4. Backfill details for a few records that lack them -------------
        missing = [r for r in state["rewards"].values()
                   if r.get("status") != REMOVED and not r.get("detail")]
        for rec in missing[:DETAIL_BACKFILL_PER_RUN]:
            _ensure_detail(rec, self.fetcher, now)

        # 5. Daily heartbeat message ----------------------------------------
        self._maybe_daily(state, now)

        changed = save_state(cfg.state_file, state)
        self.note(f"State {'updated' if changed else 'unchanged'}; "
                  f"{sum(1 for e in result.events if e.alert)} alert(s) this run")
        ping_healthcheck(cfg.healthcheck_url)
        return {"ok": True, "events": [e.summary() for e in result.events], "state_changed": changed}

    # ------------------------------------------------------------- helpers
    def _deliver(self, state: dict, events: list[Event], now: datetime) -> None:
        cfg = self.config
        alertable = [e for e in events if e.alert]
        for ev in events:
            if not ev.alert:
                commit_event(state, ev, now, delivered=True)
                self.note(f"(no alert, disabled) {ev.summary()}")
        if not alertable:
            return
        for ev in alertable:
            _ensure_detail(ev.record, self.fetcher, now, force=ev.type in ("NEW", "RETURNED"))
        filtered_out = [e for e in alertable if not wanted(e.record, cfg)]
        for ev in filtered_out:
            ev.alert = False
            commit_event(state, ev, now, delivered=True)
            self.note(f"(filtered out, no alert) {ev.summary()}")
        alertable = [e for e in alertable if e.alert]
        if not alertable:
            return

        if len(alertable) > cfg.max_individual_alerts:
            # Mass change (e.g. a big store refresh): one digest, then the
            # NEW/RESTOCK ones in detail up to the limit.
            ok = self.tg.send(messages.digest_message(alertable, now, cfg.timezone))
            detailed = [e for e in alertable if e.type in ("NEW", "RESTOCK", "RETURNED")]
            for ev in detailed[: cfg.max_individual_alerts]:
                self.tg.send(messages.event_message(ev, now, cfg.timezone))
            for ev in alertable:
                commit_event(state, ev, now, delivered=ok)
                self.note(f"{'ALERTED' if ok else 'ALERT FAILED (will retry)'} {ev.summary()}")
            return

        for ev in alertable:
            ok = self.tg.send(messages.event_message(ev, now, cfg.timezone),
                              silent=ev.type in ("SOLD_OUT", "PRICE"))
            commit_event(state, ev, now, delivered=ok)
            self.note(f"{'ALERTED' if ok else 'ALERT FAILED (will retry)'} {ev.summary()}")

    def _maybe_daily(self, state: dict, now: datetime) -> None:
        cfg = self.config
        if cfg.daily_summary_hour < 0:
            return
        try:
            local = now.astimezone(ZoneInfo(cfg.timezone))
        except Exception:
            local = now
        today = local.date().isoformat()
        if state.get("last_daily_summary") == today or local.hour < cfg.daily_summary_hour:
            return
        if state.get("last_daily_summary") is None and state.get("initialized_at"):
            # Don't send a check-in straight after the startup message.
            init = datetime.fromisoformat(state["initialized_at"])
            if now - init < timedelta(hours=1):
                state["last_daily_summary"] = today
                return
        cutoff = now - timedelta(hours=24)
        recent = []
        for ev in state.get("events", []):
            try:
                if datetime.fromisoformat(ev["at"]) >= cutoff:
                    recent.append(ev)
            except (KeyError, ValueError):
                continue
        records = [r for r in state["rewards"].values() if wanted(r, cfg)]
        if self.tg.send(messages.daily_message(records, now, cfg.timezone, recent, describe(cfg)), silent=True):
            state["last_daily_summary"] = today


def write_step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("### Cruise reward check\n\n")
        for line in lines:
            f.write(f"- {line}\n")


__all__ = ["Monitor", "ping_healthcheck", "write_step_summary", "AVAILABLE"]
