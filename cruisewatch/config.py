"""Runtime configuration, read from environment variables.

Secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HEALTHCHECK_URL) come from
GitHub Secrets. Everything else has a sensible default and can be overridden
with GitHub repository *Variables* (Settings -> Secrets and variables -> Actions
-> Variables) without touching code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

API_BASE = "https://loyalty-award-api.myvip.co/api/proxy/rewards"
# Destination 8 is the official "Cruise" category of the myVIP reward store.
CRUISE_DESTINATION_ID = 8
# Other reward-store destinations that existed when this was written
# (1 Las Vegas, 2 West Coast, 3 Canada, 7 Asia). They are swept periodically
# for anything cruise-related, in case a cruise reward is filed elsewhere.
DEFAULT_SWEEP_DESTINATIONS = (1, 2, 3, 7)
REWARD_PAGE_URL = "https://myvip.co/rewardstore/{offer_id}"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _int_list(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    out = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return tuple(out)


@dataclass
class Config:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    healthcheck_url: str = ""
    state_file: Path = Path("data/state.json")

    alert_new: bool = True
    alert_restock: bool = True
    alert_quantity_increase: bool = True
    alert_sold_out: bool = False
    alert_price_change: bool = False
    first_run_summary: bool = True

    sweep_destinations: tuple[int, ...] = DEFAULT_SWEEP_DESTINATIONS
    sweep_interval_minutes: int = 60
    removal_confirm_checks: int = 2
    failure_alert_threshold: int = 3
    daily_summary_hour: int = 9  # local hour, -1 disables
    timezone: str = "Asia/Bangkok"
    max_individual_alerts: int = 15
    dry_run: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            healthcheck_url=os.environ.get("HEALTHCHECK_URL", "").strip(),
            state_file=Path(os.environ.get("STATE_FILE", "data/state.json")),
            alert_new=_bool("ALERT_NEW", True),
            alert_restock=_bool("ALERT_RESTOCK", True),
            alert_quantity_increase=_bool("ALERT_QUANTITY_INCREASE", True),
            alert_sold_out=_bool("ALERT_SOLD_OUT", False),
            alert_price_change=_bool("ALERT_PRICE_CHANGE", False),
            first_run_summary=_bool("FIRST_RUN_SUMMARY", True),
            sweep_destinations=_int_list("SWEEP_DESTINATIONS", DEFAULT_SWEEP_DESTINATIONS),
            sweep_interval_minutes=_int("SWEEP_INTERVAL_MINUTES", 60),
            removal_confirm_checks=max(1, _int("REMOVAL_CONFIRM_CHECKS", 2)),
            failure_alert_threshold=max(1, _int("FAILURE_ALERT_THRESHOLD", 3)),
            daily_summary_hour=_int("DAILY_SUMMARY_HOUR", 9),
            timezone=os.environ.get("TIMEZONE", "Asia/Bangkok").strip() or "Asia/Bangkok",
            max_individual_alerts=max(1, _int("MAX_INDIVIDUAL_ALERTS", 15)),
            dry_run=_bool("DRY_RUN", False),
        )
