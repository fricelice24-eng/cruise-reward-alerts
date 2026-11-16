"""Pull ship / departure port / sailing dates / itinerary out of reward text.

The myVIP API has no structured fields for these. They live in free text
(the title, description and Terms & Conditions), so this is best-effort
pattern matching. Any field we can't find is simply left out of the alert
rather than guessed.
"""
from __future__ import annotations

import html
import re
from typing import Any

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
LONG_DATE = rf"{MONTH}\.? \d{{1,2}}(?:st|nd|rd|th)?(?:, \d{{4}})?"
NUM_DATE = r"\d{1,2}/\d{1,2}/\d{2,4}"

SHIP_PATTERNS = [
    r"\b([A-Z][A-Za-z'’]+ of the Seas)\b",                       # Royal Caribbean
    r"\b(Norwegian [A-Z][a-z]+)\b",                               # NCL
    r"\b(Pride of America)\b",
    r"\b(Carnival [A-Z][a-z]+)\b",
    r"\b(Celebrity [A-Z][a-z]+)\b",
    r"\b(MSC [A-Z][a-z]+)\b",
    r"\b([A-Z][a-z]+ Princess)\b",
    r"\b((?:Scarlet|Valiant|Resilient|Brilliant) Lady)\b",        # Virgin Voyages
    r"\baboard (?:the )?([A-Z][A-Za-z'’]+(?: [A-Z][A-Za-z'’]+){0,3})",
]
NOT_SHIPS = re.compile(
    r"^(Norwegian (Cruise.*|Line|In|Reward|Rewards|Ports?|Ships?)|Celebrity Cruises?.*|Carnival Cruise.*|MSC Cruises?.*|"
    r"Royal Caribbean|The|A|An)$",
    re.IGNORECASE,
)

PORT_PATTERNS = [
    r"\bout of ([A-Z][^.;]{2,60}?) for \d+ nights?",
    r"\b[Ss]et sail from ([A-Z][^.;]{2,60}?) (?:aboard|on|to)\b",
    r"\b[Dd]epart(?:s|ing)? from ([A-Z][^.;]{2,60}?) (?:aboard|on|to|for)\b",
    r"\b[Ss]ailings? from ([A-Z][^.;]{2,60}?) to\b",
    r"\bstarts in ([A-Z][^.;]{2,60}?)\.",
    r"^Port: (.+)$",
]
BAD_PORT = re.compile(r"myvip|reward|offer|purchase|redeem|now|select|any", re.IGNORECASE)


def clean_html(value: Any) -> str:
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", str(value), flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\r", "\n")
    text = re.sub(r"\s*[®™]", "", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _first(patterns: list[str], text: str, flags: int = 0) -> str | None:
    for pattern in patterns:
        for m in re.finditer(pattern, text, flags | re.MULTILINE):
            value = m.group(1).strip(" .,")
            if value:
                return value
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        key = v.casefold()
        if key not in seen:
            seen.add(key)
            out.append(v)
    # Drop fragments of longer names ("Utopia" when "Utopia of the Seas" exists).
    return [v for v in out if not any(v != o and v.casefold() in o.casefold() for o in out)]


def find_ships(*texts: str) -> list[str]:
    ships: list[str] = []
    joined = "\n".join(t for t in texts if t)
    for pattern in SHIP_PATTERNS:
        for m in re.finditer(pattern, joined):
            name = m.group(1).strip(" .,'’")
            name = re.sub(r"'s$|’s$", "", name)
            if not NOT_SHIPS.match(name) and len(name) <= 40:
                ships.append(name)
    return _dedupe(ships)


def find_port(*texts: str) -> str | None:
    for text in texts:
        if not text:
            continue
        port = _first(PORT_PATTERNS, text)
        if port and not BAD_PORT.search(port):
            return re.sub(r"\s+", " ", port)
    return None


def find_dates(*texts: str) -> str | None:
    joined = "\n".join(t for t in texts if t)
    # "sailing on 11/16/2026"
    exact = re.findall(rf"\bsailing on ({NUM_DATE})", joined, re.IGNORECASE)
    if exact:
        return ", ".join(_dedupe(exact))
    # "from January 2 through February 6, 2027" / "Sail from Jan 2, 2027, to Feb 6, 2027"
    m = re.search(
        rf"\b(?:from|between) ({LONG_DATE}),? (?:through|to|and|until|-) ({LONG_DATE})",
        joined, re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)} – {m.group(2)}"
    m = re.search(rf"\b({LONG_DATE}) through ({LONG_DATE})", joined, re.IGNORECASE)
    if m:
        return f"{m.group(1)} – {m.group(2)}"
    m = re.search(
        rf"\bsailings? (?:departing|that depart|must depart) (?:by|before|on or before) ({LONG_DATE}|{NUM_DATE})",
        joined, re.IGNORECASE,
    )
    if m:
        return f"Sailings departing by {m.group(1)}"
    m = re.search(rf"\b(?:through|until) ({LONG_DATE}, \d{{4}}|{NUM_DATE})", joined, re.IGNORECASE)
    if m:
        return f"Select sailings through {m.group(1)}"
    return None


def find_nights(*texts: str) -> str | None:
    joined = " ".join(t for t in texts if t)
    m = re.search(r"\b(\d{1,2}(?:\s*-\s*\d{1,2})?)[- ](?:Night|Day)s?\b", joined, re.IGNORECASE)
    if not m:
        return None
    unit = "night" if re.search(r"night", m.group(0), re.IGNORECASE) else "day"
    return f"{m.group(1).replace(' ', '')} {unit}s"


def find_booking_deadline(terms: str) -> str | None:
    m = re.search(
        rf"\b(?:Reservations?|Bookings?) must be (?:made|booked) (?:by|before|no later than) ({LONG_DATE}|{NUM_DATE})",
        terms, re.IGNORECASE,
    )
    return m.group(1) if m else None


def find_redeem_by(popouts: list[dict]) -> str | None:
    for p in popouts or []:
        body = clean_html(p.get("Body"))
        m = re.search(r"redeemed by ([0-9/]+ [0-9:]+ ?[AP]M(?: \(UTC\))?)", body, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def enrich(reward: dict, detail: dict | None) -> dict:
    """Return {ship, port, sailing, nights, page_title, ...} for one reward."""
    detail = detail or {}
    popouts = detail.get("Popouts") or []
    terms = ""
    for p in popouts:
        if re.search(r"terms", str(p.get("Title") or ""), re.IGNORECASE):
            terms = clean_html(p.get("Body"))
            break
    terms = terms or clean_html(detail.get("TermsAndConditionsExtended"))
    page_title = clean_html(detail.get("Title")) or reward.get("title") or ""
    description = clean_html(detail.get("Description")) or clean_html(detail.get("ShortDescription"))
    description = description or (reward.get("short_description") or "")
    title = reward.get("title") or ""
    location = reward.get("location") or ""

    structured_ship = [
        s for s in (clean_html(detail.get("OutletName")), clean_html(detail.get("SubTitle2")),
                    reward.get("outlet")) if s and find_ships(s)
    ]
    excluded = _dedupe(re.findall(r"[Nn]ot valid on ([A-Z][A-Za-z ]+?) sailings", terms))
    ships = [
        s for s in _dedupe(structured_ship + find_ships(location, title, page_title, description, terms))
        if s.casefold() not in {e.casefold() for e in excluded}
    ]
    if excluded and not ships:
        ships = [f"Any ship (excluding {', '.join(_dedupe(excluded))})"]

    port = find_port(location, description, terms)
    if not port and location and not re.search(r"various|any sailing|of the seas", location, re.IGNORECASE):
        port = location.removeprefix("Port:").strip() or None
    if not port and re.search(r"various", location, re.IGNORECASE):
        port = "Various ports"

    sailing = find_dates(description, terms)
    if not sailing:
        m = re.search(rf"({NUM_DATE})\s*$", page_title)
        if m:
            sailing = m.group(1)

    return {
        "page_title": page_title or None,
        "ship": ", ".join(ships[:4]) or None,
        "port": port,
        "sailing": sailing,
        "nights": find_nights(title, page_title),
        "book_by": find_booking_deadline(terms),
        "redeem_by": find_redeem_by(popouts),
    }
