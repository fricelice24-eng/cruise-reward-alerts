"""Which rewards you actually want to hear about.

Defaults (change with GitHub repository Variables, no code needed):
  ONLY_CRUISE_LINE       "Royal Caribbean"  ("any" = any cruise line)
  ONLY_FULL_CRUISES      true               (actual "N Night" sailings, not free play / credits / discounts)
  ONLY_DEPARTURE_REGION  "asia"             ("any" = anywhere)

Everything is still tracked in the background, so if you widen the filter
later nothing is lost. Only alerts and the summary lists are filtered.
"""
from __future__ import annotations

import re

from .config import Config

FULL_CRUISE = re.compile(r"\b\d{1,2}\s*(?:-\s*\d{1,2}\s*)?[- ]?(?:nights?|days?)\b|cruise for (?:two|2)", re.IGNORECASE)
NOT_A_SAILING = re.compile(r"free ?play|onboard credit|\boff\b|discount|wild card|%|\$\d", re.IGNORECASE)

# Departure ports / countries counted as "Asia" (incl. common cruise homeports).
ASIA = re.compile(
    r"hong kong|china|shanghai|baoshan|tianjin|beijing|xiamen|shenzhen|guangzhou|nansha|qingdao|"
    r"japan|tokyo|yokohama|kobe|osaka|nagasaki|fukuoka|hakata|okinawa|naha|"
    r"singapore|taiwan|keelung|taipei|kaohsiung|korea|busan|incheon|seoul|jeju|"
    r"thailand|bangkok|laem chabang|phuket|vietnam|ho chi minh|phu my|chan may|da nang|ha long|"
    r"malaysia|penang|port klang|kuala lumpur|philippines|manila|indonesia|bali|benoa|jakarta|"
    r"india|mumbai|chennai|cochin|sri lanka|colombo|cambodia|sihanoukville|asia",
    re.IGNORECASE,
)


def region_matches(port: str | None, region: str) -> bool | None:
    """True / False, or None when the port is unknown."""
    if region in ("", "any", "all"):
        return True
    if not port:
        return None
    if region == "asia":
        return bool(ASIA.search(port))
    return region.casefold() in port.casefold()


def wanted(rec: dict, cfg: Config) -> bool:
    if cfg.only_cruise_line and cfg.only_cruise_line.casefold() not in str(rec.get("partner") or "").casefold():
        return False
    detail = rec.get("detail") or {}
    if cfg.only_full_cruises:
        text = f"{rec.get('title') or ''} {detail.get('page_title') or ''}"
        if not FULL_CRUISE.search(text) or NOT_A_SAILING.search(str(rec.get("title") or "")):
            return False
    match = region_matches(detail.get("port"), cfg.only_departure_region)
    # Unknown port: alert anyway (better one extra message than a missed cruise).
    return match is not False


def describe(cfg: Config) -> str:
    parts = [cfg.only_cruise_line or "All cruise lines"]
    if cfg.only_full_cruises:
        parts.append("actual cruises only")
    if cfg.only_departure_region not in ("", "any", "all"):
        parts.append(f"departing {cfg.only_departure_region.title()}")
    return " · ".join(parts)


# ---------------------------------------------------------------- obtainable
from datetime import date, datetime, timedelta, timezone  # noqa: E402

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_LONG = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?(?:,? (\d{4}))?",
                   re.IGNORECASE)


def parse_dates(text: str | None) -> list[date]:
    """All dates in a string. A month-day without a year borrows the next year given."""
    if not text:
        return []
    out: list[date] = []
    for m, d, y in _NUM.findall(text):
        y = int(y) + (2000 if len(y) == 2 else 0)
        try:
            out.append(date(y, int(m), int(d)))
        except ValueError:
            pass
    longs = _LONG.findall(text)
    years = [int(y) for _, _, y in longs if y]
    for i, (mon, d, y) in enumerate(longs):
        year = int(y) if y else next((int(yy) for _, _, yy in longs[i:] if yy), years[-1] if years else None)
        if year:
            try:
                out.append(date(year, _MONTHS[mon[:3].lower()], int(d)))
            except ValueError:
                pass
    return out


def obtainable(rec: dict, cfg: Config, now: datetime) -> tuple[bool, str]:
    """Can this reward actually be bought and used right now?"""
    if rec.get("status") != "available":
        return False, "not in stock"
    today = now.astimezone(timezone.utc).date()
    expires = rec.get("expires")
    if expires:
        try:
            if datetime.fromisoformat(str(expires).replace("Z", "+00:00")) <= now:
                return False, f"reward expired {expires}"
        except ValueError:
            pass
    detail = rec.get("detail") or {}
    book_by = parse_dates(detail.get("book_by"))
    if book_by and max(book_by) < today:
        return False, f"booking deadline {detail.get('book_by')} has passed"
    sailing = parse_dates(detail.get("sailing")) or parse_dates(detail.get("page_title"))
    if sailing:
        last = max(sailing)
        if last < today + timedelta(days=max(cfg.min_days_before_sailing, 0)):
            return False, f"sailing {detail.get('sailing')} is past or too soon"
    return True, "ok"


def already_sailed(rec: dict, now: datetime) -> bool:
    detail = rec.get("detail") or {}
    dates = parse_dates(detail.get("sailing")) or parse_dates(detail.get("page_title"))
    return bool(dates) and max(dates) < now.astimezone(timezone.utc).date()
