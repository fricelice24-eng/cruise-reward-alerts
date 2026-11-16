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
