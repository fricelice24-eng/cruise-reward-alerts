"""Fetches reward data from the public myVIP loyalty API and normalises it.

This is the same unauthenticated JSON API the myVIP reward store website uses.
No account, cookie or credential is needed or used.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import API_BASE, CRUISE_DESTINATION_ID, REWARD_PAGE_URL

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; cruise-reward-alerts/1.0; personal notification bot)"
)

CRUISE_KEYWORDS = re.compile(
    r"cruise|sailing|\bship\b|of the seas|royal caribbean|\bncl\b|norwegian|carnival|"
    r"celebrity|princess|\bmsc\b|virgin voyages|holland america|disney cruise|cunard|"
    r"oceania|regent seven|silversea|seabourn|viking|windstar|azamara|explora|"
    r"costa cruise|p&o|resorts world cruises|genting|star cruises|casino royale",
    re.IGNORECASE,
)


class FetchError(Exception):
    """The API could not be reached or returned something unusable."""


Fetcher = Callable[[str], Any]


def http_get_json(url: str, *, timeout: int = 25, attempts: int = 3) -> Any:
    """GET a JSON document with retries on transient failures."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body.decode("utf-8"))
        except HTTPError as e:
            last_error = e
            # 4xx (other than 408/429) won't fix themselves with a retry.
            if 400 <= e.code < 500 and e.code not in (408, 429):
                break
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            last_error = e
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise FetchError(f"{url}: {last_error}")


def feed_url(destination_id: int) -> str:
    return f"{API_BASE}/section/destination/{destination_id}"


def detail_url(offer_id: Any) -> str:
    return f"{API_BASE}/award/{offer_id}"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def is_available(quantity: Any, snipe_text: Any, snipe_category: Any = None) -> bool:
    """A reward is purchasable unless it shows 0 quantity or a sold-out badge.

    The API omits Quantity for unlimited rewards (e.g. NCL $400-off), so with
    no quantity we fall back to the sold-out badge.
    """
    # A numeric quantity is the store's live inventory count and wins over the
    # badge, which can lag behind a restock.
    if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
        return quantity > 0
    snipe = f"{snipe_text or ''} {snipe_category or ''}".lower().replace(" ", "")
    return "soldout" not in snipe


def normalise_award(award: dict, destination_id: int) -> dict | None:
    award_id = award.get("AwardID")
    if award_id in (None, ""):
        return None
    quantity = award.get("Quantity")
    if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
        quantity = None
    location = _clean(award.get("LocationName"))
    outlet = _clean(award.get("OutletName"))
    return {
        "award_id": str(award_id),
        "offer_id": award.get("OfferID"),
        "partner": _clean(award.get("PartnerName")) or "Unknown cruise line",
        "title": _clean(award.get("Title")) or "(untitled reward)",
        "subtitle": _clean(award.get("SubTitle")) or _clean(award.get("SubTitle2")),
        "location": location or outlet,
        "outlet": outlet,
        "short_description": _clean(award.get("ShortDescription")),
        "price": award.get("Price"),
        "strike_price": award.get("StrikeOutPrice"),
        "quantity": quantity,
        "snipe": _clean(award.get("SnipeText")),
        "available": is_available(quantity, award.get("SnipeText"), award.get("SnipeCategory")),
        "premium": bool(award.get("IsPremium")),
        "expires": award.get("ExpireTime"),
        "destination": destination_id,
        "url": REWARD_PAGE_URL.format(offer_id=award.get("OfferID") or award_id),
    }


def parse_feed(data: Any, destination_id: int, *, cruise_only: bool) -> dict[str, dict]:
    """Turn a destination feed into {award_id: reward}, de-duplicated across lanes."""
    if not isinstance(data, dict) or not isinstance(data.get("Lanes"), list):
        raise FetchError(
            f"destination {destination_id}: unexpected response shape "
            f"(keys: {sorted(data)[:10] if isinstance(data, dict) else type(data).__name__})"
        )
    rewards: dict[str, dict] = {}
    for lane in data["Lanes"]:
        for award in (lane or {}).get("Awards") or []:
            if not isinstance(award, dict):
                continue
            if cruise_only:
                haystack = " ".join(
                    str(award.get(k) or "")
                    for k in ("PartnerName", "Title", "SubTitle", "ShortDescription",
                              "LocationName", "OutletName", "LoyaltyProgramName")
                )
                if not CRUISE_KEYWORDS.search(haystack):
                    continue
            reward = normalise_award(award, destination_id)
            if reward:
                rewards[reward["award_id"]] = reward
    return rewards


def fetch_rewards(
    destinations: list[int],
    fetcher: Fetcher = http_get_json,
) -> tuple[dict[str, dict], list[int], dict[int, str]]:
    """Fetch the cruise feed plus any sweep destinations.

    Returns (rewards, destinations_fetched_ok, errors_by_destination).
    Raises FetchError only if the primary Cruise feed fails, since that is
    the one that must work for the monitor to be trustworthy.
    """
    rewards: dict[str, dict] = {}
    ok: list[int] = []
    errors: dict[int, str] = {}
    for dest in destinations:
        primary = dest == CRUISE_DESTINATION_ID
        try:
            data = fetcher(feed_url(dest))
            found = parse_feed(data, dest, cruise_only=not primary)
        except FetchError as e:
            if primary:
                raise
            errors[dest] = str(e)
            log.warning("Sweep of destination %s failed: %s", dest, e)
            continue
        for award_id, reward in found.items():
            # The Cruise feed wins if a reward appears in several destinations.
            if award_id not in rewards or primary:
                rewards[award_id] = reward
        ok.append(dest)
    return rewards, ok, errors


def fetch_detail(offer_id: Any, fetcher: Fetcher = http_get_json) -> dict | None:
    if offer_id in (None, ""):
        return None
    try:
        data = fetcher(detail_url(offer_id))
    except FetchError as e:
        log.warning("Could not fetch detail for offer %s: %s", offer_id, e)
        return None
    return data if isinstance(data, dict) else None
