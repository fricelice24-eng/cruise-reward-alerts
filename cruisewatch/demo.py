"""Offline end-to-end demonstration of the detection logic.

Replays a scripted sequence of myVIP feed snapshots through the real Monitor
(real diffing, state file, message formatting) with a fake clock and fake API,
and shows exactly which Telegram messages each check would produce.
"""
from __future__ import annotations

import copy
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .monitor import Monitor
from .source import FetchError
from .telegram import Telegram

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _award(feed: dict, award_id: int) -> dict:
    for lane in feed["Lanes"]:
        for a in lane["Awards"]:
            if a["AwardID"] == award_id:
                return a
    raise KeyError(award_id)


def _set(feed: dict, award_id: int, **changes) -> None:
    for lane in feed["Lanes"]:
        for a in lane["Awards"]:
            if a["AwardID"] == award_id:
                a.update(changes)


def _remove(feed: dict, award_id: int) -> None:
    for lane in feed["Lanes"]:
        lane["Awards"] = [a for a in lane["Awards"] if a["AwardID"] != award_id]


NEW_CRUISE = {
    "AwardID": 99001, "OfferID": 99901, "PartnerName": "Royal Caribbean",
    "Title": "6 Night Taiwan & Japan", "LocationName": "", "OutletName": "Spectrum of the Seas",
    "Price": 380000, "Quantity": 4, "SnipeText": "NEW REWARD!", "SnipeCategory": None,
    "StrikeOutPrice": None, "ExpireTime": "2026-12-31T00:00:00+00:00", "IsPremium": False,
    "ShortDescription": "Enjoy a sailing for 2 on Royal Caribbeans Spectrum of the Seas out of "
                        "Hong Kong, China for 6 nights sailing on 02/14/2027 in an interior cabin.",
}
NEW_CRUISE_DETAIL = {
    "Title": "A Cruise for Two - 6 Night Taiwan & Japan 2/14",
    "Description": NEW_CRUISE["ShortDescription"],
    "OutletName": "Wonder of the Seas",
    "Popouts": [{"Title": "Terms and Conditions",
                 "Body": "Reservations must be made by January 15, 2027. One interior stateroom for two guests."}],
}


def scenario():
    """Yield (label, feed_or_exception) steps."""
    base = _load("cruise_feed.json")
    feed = copy.deepcopy(base)
    yield "Check 1: first run, baseline", copy.deepcopy(feed)
    yield "Check 2: nothing changed", copy.deepcopy(feed)

    for lane in feed["Lanes"]:
        if lane["Meta"]["Title"] in ("Royal Caribbean Rewards in Cruise", "All Rewards in Cruise"):
            lane["Awards"].append(copy.deepcopy(NEW_CRUISE))
    yield "Check 3: brand-new cruise reward appears", copy.deepcopy(feed)

    _set(feed, 99001, Quantity=0, SnipeText="sold out", SnipeCategory="soldout")
    yield "Check 4: it sells out (silent by default)", copy.deepcopy(feed)

    _set(feed, 11024, Quantity=3, SnipeText=None, SnipeCategory=None)
    yield "Check 5: sold-out 'Chan May' cruise restocked", copy.deepcopy(feed)
    yield "Check 6: unchanged again (must NOT re-alert)", copy.deepcopy(feed)

    flick = copy.deepcopy(feed)
    _remove(flick, 11237)
    yield "Check 7: one reward missing for a single check (API blip)", flick
    yield "Check 8: it's back (no alert, never confirmed removed)", copy.deepcopy(feed)

    yield "Check 9: API outage", FetchError("HTTP Error 503: Service Unavailable")
    yield "Check 10: API outage", FetchError("HTTP Error 503: Service Unavailable")
    yield "Check 11: API outage (threshold reached)", FetchError("HTTP Error 503: Service Unavailable")
    yield "Check 12: API recovers", copy.deepcopy(feed)

    _set(feed, 11024, Quantity=8)
    yield "Check 13: restocked cruise topped up 3 → 8", copy.deepcopy(feed)

    # A reward added between two checks that sold out before we looked.
    gone = dict(NEW_CRUISE, AwardID=99002, OfferID=99902, Title="5 Night Okinawa & Taiwan",
                Quantity=0, SnipeText="sold out", OutletName="Ovation of the Seas",
                ShortDescription="Enjoy a sailing for 2 on Royal Caribbeans Ovation of the Seas out of "
                                 "Singapore for 5 nights sailing on 03/06/2027.")
    for lane in feed["Lanes"]:
        if lane["Meta"]["Title"] == "All Rewards in Cruise":
            lane["Awards"].append(gone)
    yield "Check 14: new reward already sold out when first seen (not obtainable: no alert)", copy.deepcopy(feed)

    # Outside your filter: a Florida cruise, an NCL cruise and a free-play top-up -> no messages.
    usa = dict(NEW_CRUISE, AwardID=99003, OfferID=99903, Title="7 Night Western Caribbean",
               OutletName="Wonder of the Seas",
               ShortDescription="Enjoy a sailing for 2 on Royal Caribbeans Wonder of the Seas out of "
                                "Port Canaveral, Florida for 7 nights sailing on 02/14/2027.")
    ncl = dict(NEW_CRUISE, AwardID=99004, OfferID=99904, PartnerName="Norwegian Cruise Line",
               Title="Comp 5 Night NCL Cruise", OutletName=None)
    for lane in feed["Lanes"]:
        if lane["Meta"]["Title"] == "All Rewards in Cruise":
            lane["Awards"] += [usa, ncl]
    _set(feed, 6713, Quantity=40)
    yield "Check 15: Florida cruise, NCL cruise, free-play top-up (all filtered out)", copy.deepcopy(feed)


def run_demo(send_to: Telegram | None = None, out=print) -> list[tuple[str, list[str]]]:
    details = _load("details.json")
    details[str(NEW_CRUISE["OfferID"])] = NEW_CRUISE_DETAIL
    clock = [datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)]
    current_feed: dict = {}

    def fetcher(url: str):
        if "/award/" in url:
            offer = url.rsplit("/", 1)[1]
            if offer in details:
                return details[offer]
            raise FetchError("no detail fixture")
        if url.endswith("/destination/8"):
            if isinstance(current_feed.get("x"), Exception):
                raise current_feed["x"]
            return current_feed["x"]
        return {"Lanes": []}  # other destinations: nothing cruise-related

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(state_file=Path(tmp) / "state.json", daily_summary_hour=-1,
                     sweep_destinations=(1,), dry_run=True)
        tg = Telegram("", "", dry_run=True)
        mon = Monitor(cfg, telegram=tg, fetcher=fetcher, clock=lambda: clock[0])
        for label, feed in scenario():
            current_feed["x"] = feed
            before = len(tg.sent)
            mon.run()
            msgs = [m["text"] for m in tg.sent[before:]]
            results.append((label, msgs))
            out(f"\n━━━ {label} ━━━  → {len(msgs)} Telegram message(s)")
            for m in msgs:
                out("  " + m.replace("\n", "\n  "))
            clock[0] += timedelta(minutes=5)

    if send_to is not None:
        alert_msgs = [m for label, ms in results for m in ms if "monitor is live" not in m]
        send_to.send("🧪 <b>DEMO</b>: the next messages are simulated alerts from the offline "
                     "test scenario, not real rewards.", silent=True)
        for m in alert_msgs:
            send_to.send("🧪 <i>DEMO — not a real reward</i>\n" + m)
    return results
