import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.steam_watcher import (
    SteamWatcher,
    is_sync_due,
    most_recent_scheduled,
    next_scheduled,
    parse_sync_time,
    resolve_timezone,
)


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


class FakeSettingsManager:
    def __init__(self, settings, available):
        self.settings = settings
        self._available = available
        self.import_calls = []
        self.last_sync = None

    def get_available_games(self):
        return list(self._available)

    def apply_steam_import(self, owned, matched, *, playtime=None, last_sync=None):
        self.import_calls.append((list(owned), list(matched), dict(playtime or {})))
        self.last_sync = last_sync
        return len(matched)


def make_settings(**overrides):
    values = {
        "steam_api_key": "KEY",
        "steam_id64": "76561198000000000",
        "steam_auto_watch": True,
        "steam_last_sync": "",
        "steam_sync_time": "00:05",
        "steam_sync_timezone": "UTC",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestSyncSchedule(unittest.TestCase):
    # Fixed -05:00 offset stands in for Central Time (DST-independent here).
    TZ = timezone(timedelta(hours=-5))

    def test_parse_sync_time_valid_and_invalid(self):
        self.assertEqual(parse_sync_time("00:05"), (0, 5))
        self.assertEqual(parse_sync_time("23:59"), (23, 59))
        self.assertEqual(parse_sync_time("bogus"), (0, 5))
        self.assertEqual(parse_sync_time("25:00"), (0, 5))

    def test_resolve_timezone_falls_back_to_utc(self):
        self.assertEqual(resolve_timezone("Not/AZone"), timezone.utc)
        self.assertEqual(resolve_timezone(""), timezone.utc)

    def test_most_recent_scheduled_same_day(self):
        now = datetime(2026, 1, 2, 5, 30, tzinfo=timezone.utc)  # 00:30 local
        self.assertEqual(
            most_recent_scheduled(now, self.TZ, 0, 5),
            datetime(2026, 1, 2, 5, 5, tzinfo=timezone.utc),
        )

    def test_most_recent_scheduled_rolls_back_before_time(self):
        now = datetime(2026, 1, 2, 4, 30, tzinfo=timezone.utc)  # 23:30 local Jan 1
        self.assertEqual(
            most_recent_scheduled(now, self.TZ, 0, 5),
            datetime(2026, 1, 1, 5, 5, tzinfo=timezone.utc),
        )

    def test_next_scheduled_rolls_forward(self):
        now = datetime(2026, 1, 2, 5, 30, tzinfo=timezone.utc)  # 00:30 local
        self.assertEqual(
            next_scheduled(now, self.TZ, 0, 5),
            datetime(2026, 1, 3, 5, 5, tzinfo=timezone.utc),
        )

    def test_next_scheduled_same_day(self):
        now = datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc)  # 22:00 local Jan 1
        self.assertEqual(
            next_scheduled(now, self.TZ, 0, 5),
            datetime(2026, 1, 2, 5, 5, tzinfo=timezone.utc),
        )

    def test_empty_is_due(self):
        self.assertTrue(is_sync_due("", NOW, timezone.utc, 0, 5))

    def test_malformed_is_due(self):
        self.assertTrue(is_sync_due("not-a-date", NOW, timezone.utc, 0, 5))

    def test_before_scheduled_is_due(self):
        # now = 05:30 UTC = 00:30 CT; last sync 04:00 UTC is before 00:05 CT.
        now = datetime(2026, 1, 2, 5, 30, tzinfo=timezone.utc)
        self.assertTrue(is_sync_due("2026-01-02T04:00:00+00:00", now, self.TZ, 0, 5))

    def test_after_scheduled_is_not_due(self):
        # last sync 05:10 UTC is after today's 00:05 CT, so wait until tomorrow.
        now = datetime(2026, 1, 2, 5, 30, tzinfo=timezone.utc)
        self.assertFalse(is_sync_due("2026-01-02T05:10:00+00:00", now, self.TZ, 0, 5))

    def test_naive_timestamp_treated_as_utc(self):
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc)
        naive = "2026-01-02T04:00:00"  # naive, before 00:05 CT
        self.assertTrue(is_sync_due(naive, now, self.TZ, 0, 5))


class TestSteamWatcher(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_does_nothing(self):
        manager = FakeSettingsManager(make_settings(steam_auto_watch=False), ["Game X"])
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games", AsyncMock(return_value=["Game X"])
        ) as fetch:
            added = await watcher.sync_if_due(now=NOW)
        self.assertEqual(added, 0)
        fetch.assert_not_called()

    async def test_missing_credentials_does_nothing(self):
        manager = FakeSettingsManager(make_settings(steam_api_key=""), ["Game X"])
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games", AsyncMock(return_value=["Game X"])
        ) as fetch:
            added = await watcher.sync_if_due(now=NOW)
        self.assertEqual(added, 0)
        fetch.assert_not_called()

    async def test_not_due_skips(self):
        recent = (NOW - timedelta(hours=2)).isoformat()
        manager = FakeSettingsManager(
            make_settings(steam_last_sync=recent), ["Game X"]
        )
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games", AsyncMock(return_value=["Game X"])
        ) as fetch:
            added = await watcher.sync_if_due(now=NOW)
        self.assertEqual(added, 0)
        fetch.assert_not_called()

    async def test_due_sync_adds_matches_and_records_timestamp(self):
        from src.services.steam_service import OwnedSteamGame

        manager = FakeSettingsManager(make_settings(), ["Game X", "Other"])
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games",
            AsyncMock(
                return_value=[OwnedSteamGame("Game X", 300), OwnedSteamGame("Unknown", 0)]
            ),
        ):
            added = await watcher.sync_now(now=NOW)
        self.assertEqual(added, 1)
        self.assertEqual(
            manager.import_calls,
            [(["Game X", "Unknown"], ["Game X"], {"Game X": 300, "Unknown": 0})],
        )
        self.assertEqual(manager.last_sync, NOW.isoformat())

    async def test_unreachable_steam_is_skipped(self):
        from src.services.steam_service import SteamError

        manager = FakeSettingsManager(make_settings(), ["Game X"])
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games",
            AsyncMock(side_effect=SteamError("steam_unreachable")),
        ):
            added = await watcher.sync_if_due(now=NOW)
        self.assertEqual(added, 0)
        self.assertEqual(manager.import_calls, [])
        self.assertIsNone(manager.last_sync)

    async def test_refresh_returns_structured_result(self):
        from src.services.steam_service import OwnedSteamGame

        manager = FakeSettingsManager(make_settings(), ["Game X", "Other"])
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games",
            AsyncMock(
                return_value=[OwnedSteamGame("Game X", 300), OwnedSteamGame("Unknown", 0)]
            ),
        ):
            result = await watcher.refresh(now=NOW)
        self.assertTrue(result["success"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["matched"], ["Game X"])
        self.assertEqual(result["owned_count"], 2)
        self.assertEqual(manager.last_sync, NOW.isoformat())

    async def test_refresh_reports_missing_credentials(self):
        manager = FakeSettingsManager(make_settings(steam_api_key=""), ["Game X"])
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games", AsyncMock(return_value=[])
        ) as fetch:
            result = await watcher.refresh(now=NOW)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "missing_api_key")
        fetch.assert_not_called()

    async def test_refresh_returns_error_code_on_steam_failure(self):
        from src.services.steam_service import SteamError

        manager = FakeSettingsManager(make_settings(), ["Game X"])
        watcher = SteamWatcher(manager)
        with patch(
            "src.services.steam_watcher.fetch_owned_games",
            AsyncMock(side_effect=SteamError("steam_unreachable")),
        ):
            result = await watcher.refresh(now=NOW)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "steam_unreachable")
        self.assertEqual(manager.import_calls, [])
        self.assertIsNone(manager.last_sync)


if __name__ == "__main__":
    unittest.main()
