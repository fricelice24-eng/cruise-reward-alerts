"""Change detection: compares the live feed with stored state and emits events.

Event types
-----------
NEW        reward ID never seen before (alerted even if it is already sold out)
RESTOCK    reward was sold out and is purchasable again
RETURNED   reward had disappeared from the store and is back and purchasable
QTY_UP     purchasable reward's remaining quantity went up (inventory top-up)
SOLD_OUT   purchasable reward sold out            (off by default)
PRICE      loyalty-point price changed            (off by default)

Duplicate prevention: an alert fires on a *transition*, and the transition
is stored only after the alert is delivered. An unchanged reward never
re-alerts, and a failed send is retried on the next run instead of being lost.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import CRUISE_DESTINATION_ID, Config

AVAILABLE = "available"
SOLD_OUT = "sold_out"
REMOVED = "removed"

# Keep records of rewards that vanished, so a later return is recognised as
# "back" rather than "new". Prune after this long.
REMOVED_RETENTION = timedelta(days=180)

TRACKED_FIELDS = (
    "offer_id", "partner", "title", "subtitle", "location", "outlet",
    "short_description", "price", "strike_price", "quantity", "snipe",
    "premium", "expires", "destination", "url",
)


@dataclass
class Event:
    type: str
    award_id: str
    record: dict           # the record as it should be stored once alerted
    previous: dict | None  # the stored record before this run (None if new)
    alert: bool = True     # False = state change only, no notification

    def summary(self) -> str:
        r = self.record
        return f"{self.type} {r.get('partner')} | {r.get('title')} | qty={r.get('quantity')} | {r.get('price')} pts"


@dataclass
class DiffResult:
    state: dict
    events: list[Event] = field(default_factory=list)
    silent_changes: list[str] = field(default_factory=list)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _status(reward: dict) -> str:
    return AVAILABLE if reward.get("available") else SOLD_OUT


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_record(reward: dict, previous: dict | None, now: datetime) -> dict:
    rec = copy.deepcopy(previous) if previous else {}
    for key in TRACKED_FIELDS:
        rec[key] = reward.get(key)
    rec["award_id"] = reward["award_id"]
    rec["status"] = _status(reward)
    rec["missing_checks"] = 0
    rec.pop("removed_at", None)
    rec.setdefault("first_seen", _iso(now))
    q = _num(reward.get("quantity"))
    prev_max = _num(rec.get("max_quantity"))
    if q is not None:
        rec["max_quantity"] = max(q, prev_max) if prev_max is not None else q
    if previous and previous.get("offer_id") != reward.get("offer_id"):
        rec.pop("detail", None)  # offer changed, so its detail text must be re-read
    return rec


def diff(
    state: dict,
    current: dict[str, dict],
    fetched_destinations: list[int],
    config: Config,
    now: datetime,
) -> DiffResult:
    new_state = copy.deepcopy(state)
    stored: dict[str, dict] = new_state.setdefault("rewards", {})
    baselined = set(new_state.get("baselined_destinations") or [])
    result = DiffResult(state=new_state)
    primary_empty = CRUISE_DESTINATION_ID in fetched_destinations and not any(
        r.get("destination") == CRUISE_DESTINATION_ID for r in current.values()
    )

    for award_id, reward in current.items():
        prev = state.get("rewards", {}).get(award_id)
        rec = build_record(reward, prev, now)

        if prev is None:
            if reward.get("destination") not in baselined:
                # First look at this destination: record quietly, no alert.
                stored[award_id] = rec
                result.silent_changes.append(f"baseline {award_id} {rec['title']}")
            else:
                result.events.append(Event("NEW", award_id, rec, None, alert=config.alert_new))
            continue

        prev_status = prev.get("status")
        now_status = rec["status"]
        event_type: str | None = None
        alert = True

        if now_status == AVAILABLE and prev_status == SOLD_OUT:
            event_type, alert = "RESTOCK", config.alert_restock
        elif now_status == AVAILABLE and prev_status == REMOVED:
            event_type, alert = "RETURNED", config.alert_restock
        elif now_status == AVAILABLE and prev_status == AVAILABLE:
            q_old, q_new = _num(prev.get("quantity")), _num(rec.get("quantity"))
            if q_old is not None and q_new is not None and q_new > q_old:
                event_type, alert = "QTY_UP", config.alert_quantity_increase
        elif now_status == SOLD_OUT and prev_status == AVAILABLE:
            event_type, alert = "SOLD_OUT", config.alert_sold_out

        if event_type is None and prev.get("price") != rec.get("price") and prev_status != REMOVED:
            event_type, alert = "PRICE", config.alert_price_change

        if event_type:
            result.events.append(Event(event_type, award_id, rec, prev, alert=alert))
        else:
            if {k: prev.get(k) for k in rec} != rec:
                result.silent_changes.append(f"update {award_id} {rec['title']}")
            stored[award_id] = rec

    # Rewards no longer listed. Only judge destinations we actually fetched,
    # never trust an empty primary feed, and require several consecutive
    # misses before declaring a reward gone (protects against API blips).
    for award_id, prev in list(state.get("rewards", {}).items()):
        if award_id in current:
            continue
        rec = stored[award_id]
        if rec.get("destination") not in fetched_destinations:
            continue
        if primary_empty and rec.get("destination") == CRUISE_DESTINATION_ID:
            continue
        if rec.get("status") == REMOVED:
            removed_at = rec.get("removed_at")
            try:
                if removed_at and now - datetime.fromisoformat(removed_at) > REMOVED_RETENTION:
                    del stored[award_id]
            except ValueError:
                pass
            continue
        rec["missing_checks"] = int(rec.get("missing_checks") or 0) + 1
        if rec["missing_checks"] >= config.removal_confirm_checks:
            rec["status"] = REMOVED
            rec["removed_at"] = _iso(now)
            result.silent_changes.append(f"removed {award_id} {rec.get('title')}")

    for dest in fetched_destinations:
        if dest not in baselined and not (dest == CRUISE_DESTINATION_ID and primary_empty):
            baselined.add(dest)
    new_state["baselined_destinations"] = sorted(baselined)
    return result


def commit_event(state: dict, event: Event, now: datetime, delivered: bool) -> None:
    """Store the event's record (if delivered or alert-less) and log it."""
    if delivered or not event.alert:
        state["rewards"][event.award_id] = event.record
        state.setdefault("events", []).append({
            "at": _iso(now),
            "type": event.type,
            "award_id": event.award_id,
            "title": event.record.get("title"),
            "partner": event.record.get("partner"),
            "quantity": event.record.get("quantity"),
            "price": event.record.get("price"),
            "alerted": bool(event.alert and delivered),
        })
    # Not delivered: leave the old record in place so the same transition is
    # detected, and retried, on the next run.
