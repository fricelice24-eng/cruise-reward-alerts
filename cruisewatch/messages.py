"""Builds the Telegram message text (HTML parse mode)."""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Iterable
from zoneinfo import ZoneInfo

HEADERS = {
    "NEW": "🆕 <b>NEW cruise reward</b>",
    "RESTOCK": "🔁 <b>BACK IN STOCK</b>",
    "RETURNED": "♻️ <b>Reward is BACK in the store</b>",
    "QTY_UP": "📈 <b>More inventory added</b>",
    "SOLD_OUT": "⛔ <b>Sold out</b>",
    "PRICE": "💲 <b>Price changed</b>",
}


def e(value) -> str:
    return escape(str(value), quote=False)


def fmt_points(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.0f} pts"
    return "price not shown"


def availability(rec: dict) -> str:
    q = rec.get("quantity")
    if rec.get("status") == "sold_out" or not rec.get("available", rec.get("status") == "available"):
        return "❌ SOLD OUT"
    if isinstance(q, (int, float)):
        return f"✅ {q:,.0f} left"
    return "✅ Available (no quantity limit shown)"


def _local(now: datetime, tz: str) -> str:
    try:
        return now.astimezone(ZoneInfo(tz)).strftime("%d %b %H:%M %Z")
    except Exception:
        return now.strftime("%d %b %H:%M UTC")


def reward_lines(rec: dict, *, compact: bool = False) -> list[str]:
    d = rec.get("detail") or {}
    lines = [f"<b>{e(rec.get('partner'))}</b>", f"<b>{e(rec.get('title'))}</b>"]
    if d.get("ship"):
        lines.append(f"🚢 Ship: {e(d['ship'])}")
    itinerary = d.get("page_title")
    if itinerary and itinerary.strip().casefold() != str(rec.get("title")).strip().casefold():
        lines.append(f"🗺 Itinerary: {e(itinerary)}" + (f" ({e(d['nights'])})" if d.get("nights") else ""))
    elif d.get("nights"):
        lines.append(f"🗺 Length: {e(d['nights'])}")
    if d.get("sailing"):
        lines.append(f"📅 Sailing: {e(d['sailing'])}")
    port = d.get("port")
    if port:
        lines.append(f"⚓ Departs: {e(port)}")
    elif rec.get("location"):
        lines.append(f"📍 {e(rec['location'])}")
    price = f"💎 Price: <b>{e(fmt_points(rec.get('price')))}</b>"
    strike = rec.get("strike_price")
    if isinstance(strike, (int, float)) and strike and strike != rec.get("price"):
        price += f" (was {e(fmt_points(strike))})"
    lines.append(price)
    lines.append(f"📦 {availability(rec)}")
    if compact:
        return lines
    if d.get("book_by"):
        lines.append(f"🗓 Book cruise by: {e(d['book_by'])}")
    if rec.get("snipe") and "sold" not in str(rec.get("snipe")).lower():
        lines.append(f"🏷 Badge: {e(rec['snipe'])}")
    if rec.get("url"):
        lines.append(f'🔗 <a href="{e(rec["url"])}">Open in myVIP Rewards</a>')
    return lines


def event_message(event, now: datetime, tz: str, prefix: str = "") -> str:
    rec, prev = event.record, event.previous or {}
    header = HEADERS.get(event.type, e(event.type))
    if event.type == "NEW" and rec.get("status") == "sold_out":
        header += " <i>(already sold out)</i>"
    lines = [f"{prefix}{header}", ""] + reward_lines(rec)
    if event.type in ("QTY_UP", "RESTOCK") and isinstance(prev.get("quantity"), (int, float)):
        lines.append(f"↕️ Quantity: {prev['quantity']:,.0f} → {rec.get('quantity') if rec.get('quantity') is not None else '?'}")
    if event.type == "PRICE":
        lines.append(f"↕️ {e(fmt_points(prev.get('price')))} → {e(fmt_points(rec.get('price')))}")
    lines.append(f"<i>Detected {e(_local(now, tz))} · ID {e(rec.get('award_id'))}</i>")
    return "\n".join(lines)


def digest_message(events: list, now: datetime, tz: str) -> str:
    lines = [f"📣 <b>{len(events)} cruise reward changes</b>", ""]
    for ev in events:
        r = ev.record
        lines.append(
            f"{HEADERS.get(ev.type, ev.type).split('<b>')[0]}{e(ev.type)}: "
            f"{e(r.get('partner'))} — {e(r.get('title'))} · {e(fmt_points(r.get('price')))} · "
            f"{availability(r)}"
        )
    lines.append(f"\n<i>Detected {e(_local(now, tz))}</i>")
    return "\n".join(lines)


def status_list(records: Iterable[dict]) -> list[str]:
    def key(r):
        return (r.get("status") != "available", str(r.get("partner")), r.get("price") or 0)
    lines = []
    for r in sorted(records, key=key):
        icon = "✅" if r.get("status") == "available" else "❌"
        q = r.get("quantity")
        qty = f"{q:,.0f} left" if isinstance(q, (int, float)) and r.get("status") == "available" else (
            "sold out" if r.get("status") != "available" else "available")
        lines.append(f"{icon} {e(r.get('partner'))} — {e(r.get('title'))} · {e(fmt_points(r.get('price')))} · {qty}")
    return lines


def startup_message(records: list[dict], now: datetime, tz: str, filt: str = "", total: int = 0) -> str:
    avail = sum(1 for r in records if r.get("status") == "available")
    lines = [
        "🛳 <b>Cruise reward monitor is live</b>",
        f"Watching: <b>{e(filt or 'all cruise rewards')}</b>",
        f"<b>{len(records)}</b> matching rewards in the store now ({avail} purchasable)"
        + (f", out of {total} cruise rewards tracked" if total else "") + ". "
        "You'll be alerted to new ones and restocks only.",
        "",
    ] + (status_list(records) or ["(none right now)"]) + [f"\n<i>{e(_local(now, tz))}</i>"]
    return "\n".join(lines)


def daily_message(records: list[dict], now: datetime, tz: str, recent_events: list[dict], filt: str = "") -> str:
    active = [r for r in records if r.get("status") != "removed"]
    avail = sum(1 for r in active if r.get("status") == "available")
    alerts = sum(1 for ev in recent_events if ev.get("alerted"))
    lines = [
        "🩺 <b>Daily check-in: monitor is running</b>",
        (f"Watching: {e(filt)}\n" if filt else "")
        + f"{len(active)} matching rewards listed, {avail} purchasable. Alerts sent in the last 24h: {alerts}.",
        "",
    ] + (status_list(active) or ["(none right now)"]) + [f"\n<i>{e(_local(now, tz))}</i>"]
    return "\n".join(lines)
