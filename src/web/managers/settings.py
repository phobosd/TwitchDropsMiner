"""Settings manager for application configuration."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.config.settings import InventoryFilterSettings, MiningHoursSettings
from src.i18n.translator import _
from src.models.game import Game
from src.services.discord_service import DiscordNotifier
from src.services.steam_service import normalize_game_name
from src.services.steam_watcher import (
    DEFAULT_SYNC_TIME,
    DEFAULT_SYNC_TIMEZONE,
    SUPPORTED_TIMEZONES,
    is_valid_sync_time,
    next_scheduled,
    parse_sync_time,
    resolve_timezone,
)
from src.utils import DropIgnorePolicy


logger = logging.getLogger("TwitchDrops")

# Value returned to web clients in place of the stored Telegram bot token.
# Never echo the real credential through the unauthenticated web API/socket.
TELEGRAM_TOKEN_MASK = "••••••••"
DISCORD_WEBHOOK_MASK = "••••••••"
STEAM_KEY_MASK = "••••••••"


def _format_mining_hours(value: Any) -> str:
    """Return a console-friendly summary that hides times outside range mode."""
    if isinstance(value, dict) and value.get("mode") == "range":
        return f"{value.get('start')}-{value.get('end')}"
    return "always"


if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.web.managers.broadcaster import WebSocketBroadcaster
    from src.web.managers.console import ConsoleOutputManager


class SettingsManager:
    """Manages application settings in the web interface.

    Provides access to and modification of user preferences including
    game priorities, proxy configuration, and UI preferences.
    """

    def __init__(
        self,
        broadcaster: WebSocketBroadcaster,
        settings: Settings,
        console: ConsoleOutputManager,
        on_change: Callable[[], None] | None = None,
    ):
        self._broadcaster = broadcaster
        self._settings = settings
        self._console = console
        self._on_change = on_change
        self._available_games: list[str] = []

    @property
    def settings(self) -> Settings:
        """Return the underlying settings object."""
        return self._settings

    def get_settings(self, legacy_show_not_linked: bool | None = None) -> dict[str, Any]:
        """Get current settings for display.

        Args:
            legacy_show_not_linked: Request-scoped value echoed only in the
                immediate settings POST response for a legacy frontend. It is
                never persisted, mapped to ``link_status``, or returned
                by a later GET/page reload.

        Returns:
            Dictionary containing all user-configurable settings
        """
        settings = vars(self._settings).copy()
        settings["games_available"] = self._available_games
        configured_webhook = bool(settings.get("discord_webhook_url"))
        settings["discord_configured"] = configured_webhook
        settings["discord_webhook_url"] = DISCORD_WEBHOOK_MASK if configured_webhook else ""
        # Never expose the real Telegram bot token to web clients. The token
        # stays server-side; clients only see a configured flag and a mask.
        configured_token = bool(settings.get("telegram_bot_token"))
        settings["telegram_configured"] = configured_token
        settings["telegram_bot_token"] = TELEGRAM_TOKEN_MASK if configured_token else ""
        # Never echo the Steam Web API key; expose only a configured flag/mask.
        configured_steam = bool(settings.get("steam_api_key"))
        settings["steam_configured"] = configured_steam
        settings["steam_api_key"] = STEAM_KEY_MASK if configured_steam else ""
        # The full owned-games list can be large and is only used server-side for
        # prioritization; expose the count instead of the list.
        owned_games = settings.pop("steam_owned_games", []) or []
        settings["steam_owned_count"] = len(owned_games)
        settings.pop("steam_playtime", None)
        # Constrained choices for the schedule UI, plus a preview of the next run.
        timezones = list(SUPPORTED_TIMEZONES)
        current_zone = settings.get("steam_sync_timezone") or DEFAULT_SYNC_TIMEZONE
        if current_zone not in timezones:
            timezones.insert(0, current_zone)
        settings["steam_timezones"] = timezones
        hours, minutes = parse_sync_time(settings.get("steam_sync_time") or DEFAULT_SYNC_TIME)
        tz = resolve_timezone(current_zone)
        settings["steam_next_run"] = next_scheduled(
            datetime.now(timezone.utc), tz, hours, minutes
        ).isoformat()
        # TODO(remove in 1.3.x): Retain this POST-only echo long enough for stale
        # pre-versioned frontends to age out; it never survives a page reload.
        if legacy_show_not_linked is not None:
            inventory_filters = copy.deepcopy(dict(self._settings.inventory_filters))
            inventory_filters["show_not_linked"] = legacy_show_not_linked
            settings["inventory_filters"] = inventory_filters
        return settings

    def get_languages(self) -> dict[str, Any]:
        """Get available languages and current selection.

        Returns:
            Dictionary with available languages and current language
        """
        return {
            "available": _.get_languages(),
            "current": _.current_language,
        }

    def _log_change(self, message: str):
        """Log setting change to both console and system logger."""
        self._console.print(message)

    def update_settings(self, settings_data: dict[str, Any]) -> dict[str, Any]:
        """Update settings from user input.

        Args:
            settings_data: Dictionary of settings to update
        """
        previous_hours: dict[str, str] = getattr(self._settings, "mining_hours", {})
        requested_hours = settings_data.get("mining_hours")
        if requested_hours is not None:
            requested_hours = MiningHoursSettings.normalize(requested_hours)
        previous_webhook = getattr(self._settings, "discord_webhook_url", "")
        new_webhook = previous_webhook
        if settings_data.get("discord_remove_webhook") is True:
            new_webhook = ""
        elif "discord_webhook_url" in settings_data:
            requested_webhook = settings_data.get("discord_webhook_url")
            if requested_webhook is not None and not isinstance(requested_webhook, str):
                raise ValueError("invalid_discord_webhook")
            requested_webhook = (requested_webhook or "").strip()
            if requested_webhook and requested_webhook != DISCORD_WEBHOOK_MASK:
                if not DiscordNotifier.is_valid_webhook(requested_webhook):
                    raise ValueError("invalid_discord_webhook")
                new_webhook = requested_webhook

        should_trigger_update = False
        previous_allow_unlinked = getattr(self._settings, "allow_unlinked_campaigns", False)
        requested_allow_unlinked = settings_data.get("allow_unlinked_campaigns")
        override_changed = (
            isinstance(requested_allow_unlinked, bool)
            and requested_allow_unlinked != previous_allow_unlinked
        )
        should_trigger_update |= self.check_and_update_setting(
            "games_to_watch", settings_data.get("games_to_watch"), True
        )
        drop_name_blacklist = settings_data.get("drop_name_blacklist")
        if drop_name_blacklist is not None:
            drop_name_blacklist = DropIgnorePolicy.normalize_keywords(
                drop_name_blacklist
            )
        should_trigger_update |= self.check_and_update_setting(
            "drop_name_blacklist", drop_name_blacklist, True
        )
        should_trigger_update |= self.check_and_update_setting(
            "dark_mode", settings_data.get("dark_mode")
        )
        should_trigger_update |= self.check_and_update_setting(
            "language", settings_data.get("language"), False, self._set_language
        )
        should_trigger_update |= self.check_and_update_setting(
            "connection_quality", settings_data.get("connection_quality")
        )
        if "proxy" in settings_data:
            proxy_value = settings_data["proxy"]
            should_trigger_update |= self.check_and_update_setting(
                "proxy",
                str(proxy_value).strip() if proxy_value else "",
                True,
                lambda proxy: self._log_change("Proxy cleared") if proxy == "" else None,
            )
        should_trigger_update |= self.check_and_update_setting(
            "minimum_refresh_interval_minutes",
            settings_data.get("minimum_refresh_interval_minutes"),
        )
        if "telegram_bot_token" in settings_data:
            new_token = str(settings_data.get("telegram_bot_token") or "").strip()
            if new_token and new_token != TELEGRAM_TOKEN_MASK:
                self.check_and_update_setting(
                    "telegram_bot_token", new_token
                )
        if "steam_id64" in settings_data:
            self.check_and_update_setting(
                "steam_id64", str(settings_data.get("steam_id64") or "").strip()
            )
        if "steam_api_key" in settings_data:
            new_key = str(settings_data.get("steam_api_key") or "").strip()
            if new_key and new_key != STEAM_KEY_MASK:
                self.check_and_update_setting("steam_api_key", new_key)
        if "steam_auto_watch" in settings_data:
            self.check_and_update_setting(
                "steam_auto_watch", bool(settings_data.get("steam_auto_watch"))
            )
        if "steam_prioritize" in settings_data:
            previous_prioritize = getattr(self._settings, "steam_prioritize", False)
            self.check_and_update_setting(
                "steam_prioritize", bool(settings_data.get("steam_prioritize"))
            )
            # Turning prioritization on immediately reorders the current watch list.
            if self._settings.steam_prioritize and not previous_prioritize:
                should_trigger_update |= self.reorder_steam_first()
        if "steam_sync_time" in settings_data:
            value = str(settings_data.get("steam_sync_time") or "").strip()
            current_time = getattr(self._settings, "steam_sync_time", "")
            if not is_valid_sync_time(value) and value != current_time:
                raise ValueError("invalid_steam_sync_time")
            self.check_and_update_setting("steam_sync_time", value)
        if "steam_sync_timezone" in settings_data:
            value = str(settings_data.get("steam_sync_timezone") or "").strip()
            current_zone = getattr(self._settings, "steam_sync_timezone", "")
            if value not in SUPPORTED_TIMEZONES and value != current_zone:
                raise ValueError("invalid_steam_sync_timezone")
            self.check_and_update_setting("steam_sync_timezone", value)
        if "telegram_chat_id" in settings_data:
            self.check_and_update_setting(
                "telegram_chat_id", settings_data.get("telegram_chat_id") or ""
            )
        inventory_filters = settings_data.get("inventory_filters")
        legacy_show_not_linked = None
        if inventory_filters is not None:
            legacy_value = inventory_filters.get("show_not_linked")
            if isinstance(legacy_value, bool):
                legacy_show_not_linked = legacy_value
            inventory_filters = self._normalize_inventory_filters(inventory_filters)
        should_trigger_update |= self.check_and_update_setting("inventory_filters", inventory_filters)
        should_trigger_update |= self.check_and_update_setting(
            "inventory_list_view", settings_data.get("inventory_list_view")
        )
        should_trigger_update |= self.check_and_update_setting(
            "mining_benefits", settings_data.get("mining_benefits"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "mining_hours",
            requested_hours,
            True,
            log_formatter=_format_mining_hours,
        )

        if override_changed and isinstance(requested_allow_unlinked, bool):
            self._settings.allow_unlinked_campaigns = requested_allow_unlinked
        self._settings.discord_webhook_url = new_webhook
        try:
            self._settings.save()
        except Exception:
            # A rejected save must not silently enable or disable live mining.
            self._settings.discord_webhook_url = previous_webhook
            self._settings.allow_unlinked_campaigns = previous_allow_unlinked
            self._settings.mining_hours = previous_hours
            raise
        if new_webhook != previous_webhook:
            self._log_change(f"Setting changed: discord_webhook_url = {DISCORD_WEBHOOK_MASK}")
        if override_changed:
            self._log_change(
                f"Setting changed: allow_unlinked_campaigns = {requested_allow_unlinked}"
            )
            should_trigger_update = True
        response_settings = self.get_settings(legacy_show_not_linked)
        asyncio.create_task(self._broadcaster.emit("settings_updated", response_settings))

        if should_trigger_update and self._on_change:
            self._on_change()

        return response_settings

    def _normalize_inventory_filters(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge partial filter updates and discard legacy or unknown keys."""
        return dict(InventoryFilterSettings.normalize(updates, self._settings.inventory_filters))

    def check_and_update_setting(
        self,
        key: str,
        new_value: Any,
        should_trigger_update: bool = False,
        action: Callable[[Any], None] = lambda x: None,
        log_formatter: Callable[[Any], str] | None = None,
    ):
        if new_value is None or getattr(self._settings, key, None) == new_value:
            return False
        setattr(self._settings, key, new_value)
        log_value = TELEGRAM_TOKEN_MASK if key in {"telegram_bot_token", "discord_webhook_url", "steam_api_key"} else (
            log_formatter(new_value) if log_formatter else new_value
        )
        self._log_change(f"Setting changed: {key} = {log_value}")
        action(new_value)
        return should_trigger_update

    def _set_language(self, language: str):
        _.set_language(language)
        # Notify clients that translations need to be reloaded
        asyncio.create_task(self._broadcaster.emit("language_changed", {"language": language}))

    def set_games(self, games: set[Game]):
        """Update the list of available games for settings panel.

        Args:
            games: Set of Game objects discovered from campaigns
        """
        # Store and broadcast available games for settings panel
        game_names = sorted([g.name for g in games])
        self._available_games = game_names
        asyncio.create_task(self._broadcaster.emit("games_available", {"games": game_names}))

    def get_available_games(self) -> list[str]:
        """Return the Twitch categories that currently have campaigns."""
        return list(self._available_games)

    def _reorder_steam_first(
        self,
        games: list[str],
        owned_games: list[str],
        playtime: dict[str, int] | None = None,
    ) -> list[str]:
        """Return ``games`` with Steam-owned titles ahead of the rest.

        Owned games are ordered by total playtime (most played first); ties keep
        their existing relative order. Non-owned games follow in their original
        relative order.
        """
        owned_keys = {normalize_game_name(name) for name in owned_games if name}
        playtime_by_key: dict[str, int] = {}
        for name, minutes in (playtime or {}).items():
            key = normalize_game_name(name)
            playtime_by_key[key] = max(playtime_by_key.get(key, 0), int(minutes or 0))

        owned_first = [name for name in games if normalize_game_name(name) in owned_keys]
        others = [name for name in games if normalize_game_name(name) not in owned_keys]
        owned_first.sort(
            key=lambda name: playtime_by_key.get(normalize_game_name(name), 0),
            reverse=True,
        )
        return owned_first + others

    def reorder_steam_first(self) -> bool:
        """Reorder the watch list so Steam-owned games come first.

        Owned games are ordered by playtime, most played first, using the last
        synced library. Returns whether the order changed.
        """
        if not getattr(self._settings, "steam_prioritize", False):
            return False
        owned_games = getattr(self._settings, "steam_owned_games", []) or []
        if not owned_games:
            return False
        current = list(getattr(self._settings, "games_to_watch", []) or [])
        reordered = self._reorder_steam_first(
            current, owned_games, getattr(self._settings, "steam_playtime", {})
        )
        if reordered == current:
            return False
        self._settings.games_to_watch = reordered
        return True

    def apply_steam_import(
        self,
        owned_games: list[str],
        matched_games: list[str],
        *,
        playtime: dict[str, int] | None = None,
        last_sync: str | None = None,
    ) -> int:
        """Record the Steam library and add matched games to the watch list.

        When ``steam_prioritize`` is enabled the resulting list is reordered so
        owned games stay ahead of the rest, most played first. Persists and
        broadcasts once.

        Returns the number of games newly added.
        """
        previous_watch = list(getattr(self._settings, "games_to_watch", []) or [])
        previous_owned = getattr(self._settings, "steam_owned_games", []) or []
        previous_playtime = getattr(self._settings, "steam_playtime", {}) or {}
        previous_sync = getattr(self._settings, "steam_last_sync", "")

        current = list(previous_watch)
        existing = {name.lower() for name in current}
        added: list[str] = []
        for name in matched_games:
            if name and name.lower() not in existing:
                current.append(name)
                existing.add(name.lower())
                added.append(name)

        if getattr(self._settings, "steam_prioritize", False):
            current = self._reorder_steam_first(current, owned_games, playtime)

        self._settings.steam_owned_games = list(owned_games)
        self._settings.steam_playtime = dict(playtime or {})
        self._settings.games_to_watch = current
        if last_sync is not None:
            self._settings.steam_last_sync = last_sync

        order_changed = current != previous_watch
        try:
            self._settings.save()
        except Exception:
            self._settings.games_to_watch = previous_watch
            self._settings.steam_owned_games = previous_owned
            self._settings.steam_playtime = previous_playtime
            self._settings.steam_last_sync = previous_sync
            raise

        if added:
            self._log_change(f"Steam import added {len(added)} game(s)")
        asyncio.create_task(self._broadcaster.emit("settings_updated", self.get_settings()))
        if (added or order_changed) and self._on_change:
            self._on_change()
        return len(added)
