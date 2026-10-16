"""Background auto-watch that imports Steam library games with active drops.

Opt-in: only runs when ``steam_auto_watch`` is enabled and credentials are
configured. Re-syncs on a fixed daily schedule (``steam_sync_time`` in
``steam_sync_timezone``), so the time does not drift with restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.services.steam_service import SteamError, fetch_owned_games, match_available_games


if TYPE_CHECKING:
    from src.diagnostics import Diagnostics
    from src.web.managers.settings import SettingsManager


class SteamRefreshResult(TypedDict, total=False):
    """Outcome of a Steam library sync, suitable for the web API."""

    success: bool
    added: int
    matched: list[str]
    owned_count: int
    error: str


logger = logging.getLogger("TwitchDrops")

DEFAULT_SYNC_TIME = "00:05"
DEFAULT_SYNC_TIMEZONE = "America/Chicago"
# How often the watcher wakes to check whether the scheduled sync time arrived.
CHECK_INTERVAL_SECONDS = 60

SYNC_TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")

# Curated IANA zones offered in the settings UI. Constraining the choices keeps
# typos out of the schedule; the resolver still tolerates a legacy stored value.
SUPPORTED_TIMEZONES: tuple[str, ...] = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "America/Mexico_City",
    "America/Sao_Paulo",
    "UTC",
    "Europe/London",
    "Europe/Lisbon",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Amsterdam",
    "Europe/Stockholm",
    "Europe/Warsaw",
    "Europe/Moscow",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Perth",
    "Australia/Adelaide",
    "Australia/Sydney",
    "Pacific/Auckland",
)


def is_valid_sync_time(value: str) -> bool:
    """Return whether ``value`` is a well-formed 24-hour ``HH:MM`` string."""
    return SYNC_TIME_PATTERN.fullmatch(str(value or "")) is not None


def resolve_timezone(name: str, *, default: tzinfo = timezone.utc) -> tzinfo:
    """Resolve an IANA timezone name, falling back to ``default`` (UTC)."""
    name = (name or "").strip()
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown Steam sync timezone %r; using UTC", name)
        return default


def parse_sync_time(value: str) -> tuple[int, int]:
    """Parse a ``HH:MM`` clock string, falling back to the default."""
    try:
        hours, minutes = str(value).split(":")
        hours_i, minutes_i = int(hours), int(minutes)
        if 0 <= hours_i < 24 and 0 <= minutes_i < 60:
            return hours_i, minutes_i
    except (ValueError, AttributeError):
        pass
    hours, minutes = DEFAULT_SYNC_TIME.split(":")
    return int(hours), int(minutes)


def most_recent_scheduled(now: datetime, tz: tzinfo, hours: int, minutes: int) -> datetime:
    """Return the most recent occurrence of the daily time, in UTC."""
    local = now.astimezone(tz)
    candidate = local.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if candidate > local:
        candidate -= timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def next_scheduled(now: datetime, tz: tzinfo, hours: int, minutes: int) -> datetime:
    """Return the next occurrence of the daily time, in UTC."""
    local = now.astimezone(tz)
    candidate = local.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def is_sync_due(
    last_sync: str,
    now: datetime,
    tz: tzinfo,
    hours: int,
    minutes: int,
) -> bool:
    """Return whether the daily sync has come due since the last successful run.

    A missing or unparseable timestamp is treated as due (first run).
    """
    if not last_sync:
        return True
    try:
        parsed = datetime.fromisoformat(last_sync)
    except (ValueError, TypeError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    scheduled = most_recent_scheduled(now, tz, hours, minutes)
    return parsed.astimezone(timezone.utc) < scheduled


class SteamWatcher:
    """Periodically matches the Steam library against active drop campaigns."""

    def __init__(
        self,
        settings_manager: SettingsManager,
        *,
        check_interval: float = CHECK_INTERVAL_SECONDS,
        diagnostics: Diagnostics | None = None,
    ):
        self._settings_manager = settings_manager
        self._check_interval = check_interval
        self._diagnostics = diagnostics
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.sync_if_due()
            except Exception:
                logger.exception("Steam auto-watch sync failed")
            await asyncio.sleep(self._check_interval)

    def _settings(self):
        return self._settings_manager.settings

    async def sync_if_due(self, *, now: datetime | None = None) -> int:
        """Run a sync when enabled, configured, and the daily time has passed."""
        settings = self._settings()
        if not getattr(settings, "steam_auto_watch", False):
            return 0
        if not getattr(settings, "steam_api_key", "") or not getattr(
            settings, "steam_id64", ""
        ):
            return 0
        now = now or datetime.now(timezone.utc)
        tz = resolve_timezone(
            getattr(settings, "steam_sync_timezone", DEFAULT_SYNC_TIMEZONE)
        )
        hours, minutes = parse_sync_time(
            getattr(settings, "steam_sync_time", DEFAULT_SYNC_TIME)
        )
        if not is_sync_due(
            getattr(settings, "steam_last_sync", ""), now, tz, hours, minutes
        ):
            return 0
        return await self.sync_now(now=now)

    async def sync_now(self, *, now: datetime | None = None) -> int:
        """Fetch the library, add matched campaigns, and record the timestamp.

        Returns the number of games newly added to "Games to Watch".
        """
        result = await self.refresh(now=now)
        return result["added"] if result["success"] else 0

    async def refresh(self, *, now: datetime | None = None) -> SteamRefreshResult:
        """Run one library sync regardless of the daily schedule.

        Used both by the scheduled check and by the dashboard's manual
        "Check now" action. Returns a structured result so callers can report
        success, the matched games, and any Steam error code.
        """
        settings = self._settings()
        api_key = getattr(settings, "steam_api_key", "")
        steam_id64 = getattr(settings, "steam_id64", "")
        if not api_key or not steam_id64:
            return {"success": False, "error": "missing_api_key"}

        try:
            owned = await fetch_owned_games(
                api_key, steam_id64, diagnostics=self._diagnostics
            )
        except SteamError as exc:
            logger.warning("Steam sync skipped (%s)", exc.code)
            return {"success": False, "error": exc.code}

        names = [game.name for game in owned]
        playtime = {game.name: game.playtime_minutes for game in owned}
        matched = match_available_games(names, self._settings_manager.get_available_games())
        added = self._settings_manager.apply_steam_import(
            names,
            matched,
            playtime=playtime,
            last_sync=(now or datetime.now(timezone.utc)).isoformat(),
        )
        if added:
            logger.info("Steam sync added %d game(s) with active drops", added)
        return {
            "success": True,
            "added": added,
            "matched": matched,
            "owned_count": len(owned),
        }
