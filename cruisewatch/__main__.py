"""CLI.

  python -m cruisewatch check         run one monitoring pass (what the schedule runs)
  python -m cruisewatch test          send a test notification
  python -m cruisewatch test --live   test notification + a sample alert built from live data
  python -m cruisewatch chat-id       print chat IDs that have messaged your bot
  python -m cruisewatch demo [--send] offline simulation of new/restock detection
  python -m cruisewatch status        print what the monitor currently knows
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from . import messages
from .config import CRUISE_DESTINATION_ID, Config
from .detect import Event
from .monitor import Monitor, _ensure_detail, ping_healthcheck, write_step_summary
from .source import FetchError, fetch_rewards, http_get_json
from .state import load_state
from .telegram import Telegram, TelegramError


def cmd_check(cfg: Config) -> int:
    mon = Monitor(cfg)
    try:
        result = mon.run()
    except Exception as e:  # unexpected bug: make it loud everywhere
        logging.exception("Monitor crashed")
        ping_healthcheck(cfg.healthcheck_url, "/fail")
        mon.tg.send(f"🛑 <b>Cruise monitor crashed</b>\n<code>{messages.e(repr(e)[:500])}</code>\n"
                    "Check the GitHub Actions log.")
        write_step_summary(mon.report + [f"CRASH: {e!r}"])
        return 1
    write_step_summary(mon.report)
    print(json.dumps(result, indent=1))
    return 0


def cmd_test(cfg: Config, live: bool) -> int:
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    if not tg.configured:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set.", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    ok = tg.send("✅ <b>Test notification</b>\nYour cruise reward bot can reach you. "
                 f"<i>{messages.e(messages._local(now, cfg.timezone))}</i>")
    if not ok:
        print("Test notification FAILED. See the error above.", file=sys.stderr)
        return 1
    print("Test notification sent.")
    if live:
        try:
            rewards, _, _ = fetch_rewards([CRUISE_DESTINATION_ID])
        except FetchError as e:
            tg.send(f"⚠️ Could not reach the myVIP API for the live sample: <code>{messages.e(e)}</code>")
            return 1
        cruises = sorted(rewards.values(), key=lambda r: (not r["available"], -(r["price"] or 0)))
        sample = next((r for r in cruises if "night" in r["title"].lower() or "cruise" in r["title"].lower()),
                      cruises[0] if cruises else None)
        if sample:
            rec = dict(sample, status="available" if sample["available"] else "sold_out")
            _ensure_detail(rec, http_get_json, now)
            ev = Event("NEW", rec["award_id"], rec, None)
            tg.send("🧪 <i>SAMPLE: real data from the live store, formatted as a new-reward alert "
                    "(this is not actually a new reward)</i>\n" + messages.event_message(ev, now, cfg.timezone))
            tg.send(f"📊 Live store right now: {len(rewards)} cruise rewards, "
                    f"{sum(r['available'] for r in rewards.values())} purchasable.", silent=True)
            print(f"Live sample sent: {sample['partner']} — {sample['title']}")
    return 0


def cmd_chat_id(cfg: Config) -> int:
    tg = Telegram(cfg.telegram_token, "")
    try:
        me = tg.get_me()
        updates = tg.get_updates()
    except TelegramError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Bot: @{me.get('username')}")
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("my_chat_member") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title")
    if not chats:
        print("No chats found. Open Telegram, find your bot, tap START (or send 'hi'), then run this again.")
        return 1
    for cid, name in chats.items():
        replier = Telegram(cfg.telegram_token, str(cid))
        ok = replier.send(
            f"🔑 Your chat ID is:\n<code>{cid}</code>\n\nCopy it into the GitHub secret "
            "<b>TELEGRAM_CHAT_ID</b>.")
        masked = str(cid)[:2] + "…" + str(cid)[-2:]
        print(f"Chat {masked} ({'reply sent' if ok else 'reply FAILED'}) — check Telegram for the full ID.")
    return 0


def cmd_status(cfg: Config) -> int:
    state = load_state(cfg.state_file)
    print(f"Initialized: {state.get('initialized_at')}   health: {state['health']}")
    for line in messages.status_list(r for r in state["rewards"].values() if r.get("status") != "removed"):
        print(line)
    print(f"\nLast events:")
    for ev in state.get("events", [])[-15:]:
        print(f"  {ev['at']} {ev['type']:<8} {ev.get('partner')} — {ev.get('title')} (alerted={ev.get('alerted')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="cruisewatch")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("check")
    t = sub.add_parser("test")
    t.add_argument("--live", action="store_true")
    sub.add_parser("chat-id")
    d = sub.add_parser("demo")
    d.add_argument("--send", action="store_true", help="also send the simulated alerts to Telegram")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    cfg = Config.from_env()

    if args.cmd in (None, "check"):
        return cmd_check(cfg)
    if args.cmd == "test":
        return cmd_test(cfg, args.live)
    if args.cmd == "chat-id":
        return cmd_chat_id(cfg)
    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "demo":
        from .demo import run_demo
        target = Telegram(cfg.telegram_token, cfg.telegram_chat_id) if args.send else None
        if args.send and not target.configured:
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to use --send.", file=sys.stderr)
            return 2
        run_demo(target)
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
