import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.steam_service import OwnedSteamGame
from src.web.app import SettingsUpdate, SteamImportRequest, SteamPreviewRequest
from src.web.managers.settings import STEAM_KEY_MASK, SettingsManager


web_app = importlib.import_module("src.web.app")


def _stored_settings(**overrides):
    values = {
        "discord_webhook_url": "",
        "games_to_watch": [],
        "steam_api_key": "",
        "steam_auto_watch": False,
        "steam_id64": "",
        "steam_last_sync": "",
        "steam_owned_games": [],
        "steam_playtime": {},
        "steam_prioritize": False,
        "steam_sync_time": "00:05",
        "steam_sync_timezone": "America/Chicago",
        "telegram_bot_token": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _manager(settings):
    settings.save = MagicMock()
    return SettingsManager(AsyncMock(), settings, MagicMock())


class TestSteamSettings(unittest.IsolatedAsyncioTestCase):
    def test_settings_update_model_accepts_steam_fields(self):
        model = SettingsUpdate(
            steam_id64="76561198000000000",
            steam_api_key="KEY",
            steam_auto_watch=True,
            steam_prioritize=True,
        )
        self.assertEqual(model.steam_id64, "76561198000000000")
        self.assertEqual(model.steam_api_key, "KEY")
        self.assertTrue(model.steam_auto_watch)
        self.assertTrue(model.steam_prioritize)

    def test_get_settings_masks_api_key_and_hides_owned_list(self):
        settings = _stored_settings(
            steam_api_key="SECRET", steam_owned_games=["A", "B", "C"]
        )
        manager = SettingsManager(AsyncMock(), settings, MagicMock())

        result = manager.get_settings()

        self.assertEqual(result["steam_api_key"], STEAM_KEY_MASK)
        self.assertTrue(result["steam_configured"])
        self.assertNotIn("steam_owned_games", result)
        self.assertEqual(result["steam_owned_count"], 3)

    def test_get_settings_without_key(self):
        manager = SettingsManager(AsyncMock(), _stored_settings(), MagicMock())

        result = manager.get_settings()

        self.assertEqual(result["steam_api_key"], "")
        self.assertFalse(result["steam_configured"])
        self.assertEqual(result["steam_owned_count"], 0)

    async def test_update_stores_key_and_ignores_mask(self):
        settings = _stored_settings()
        manager = _manager(settings)

        manager.update_settings(
            {
                "steam_id64": "76561198000000000",
                "steam_api_key": "NEWKEY",
                "steam_auto_watch": True,
            }
        )
        await asyncio.sleep(0)

        self.assertEqual(settings.steam_api_key, "NEWKEY")
        self.assertEqual(settings.steam_id64, "76561198000000000")
        self.assertTrue(settings.steam_auto_watch)
        settings.save.assert_called_once()

        manager.update_settings({"steam_api_key": STEAM_KEY_MASK})
        await asyncio.sleep(0)
        self.assertEqual(settings.steam_api_key, "NEWKEY")

    async def test_apply_steam_import_adds_and_records(self):
        settings = _stored_settings(games_to_watch=["Existing"])
        callback = MagicMock()
        settings.save = MagicMock()
        manager = SettingsManager(AsyncMock(), settings, MagicMock(), on_change=callback)

        added = manager.apply_steam_import(
            ["Steam A", "Steam B"],
            ["Steam A"],
            last_sync="2026-01-02T00:00:00+00:00",
        )
        await asyncio.sleep(0)

        self.assertEqual(added, 1)
        self.assertEqual(settings.games_to_watch, ["Existing", "Steam A"])
        self.assertEqual(settings.steam_owned_games, ["Steam A", "Steam B"])
        self.assertEqual(settings.steam_last_sync, "2026-01-02T00:00:00+00:00")
        callback.assert_called_once()
        settings.save.assert_called_once()

    async def test_apply_steam_import_prioritizes_owned_first(self):
        settings = _stored_settings(
            games_to_watch=["Not Owned", "Owned B", "Owned A"],
            steam_prioritize=True,
        )
        manager = _manager(settings)

        manager.apply_steam_import(["Owned A", "Owned B"], [], last_sync="t")
        await asyncio.sleep(0)

        self.assertEqual(settings.games_to_watch, ["Owned B", "Owned A", "Not Owned"])

    async def test_apply_steam_import_sorts_owned_by_playtime(self):
        settings = _stored_settings(
            games_to_watch=["A", "B", "C", "Not Owned"],
            steam_prioritize=True,
        )
        manager = _manager(settings)

        manager.apply_steam_import(
            ["A", "B", "C"],
            [],
            playtime={"A": 10, "B": 500, "C": 100},
            last_sync="t",
        )
        await asyncio.sleep(0)

        self.assertEqual(settings.games_to_watch, ["B", "C", "A", "Not Owned"])

    async def test_enabling_prioritize_reorders_existing_watch_list(self):
        settings = _stored_settings(
            games_to_watch=["Skyrim Mod Tool", "The Elder Scrolls V: Skyrim"],
            steam_owned_games=["The Elder Scrolls V: Skyrim"],
            steam_prioritize=False,
        )
        manager = _manager(settings)

        manager.update_settings({"steam_prioritize": True})
        await asyncio.sleep(0)

        self.assertTrue(settings.steam_prioritize)
        self.assertEqual(
            settings.games_to_watch,
            ["The Elder Scrolls V: Skyrim", "Skyrim Mod Tool"],
        )

    def test_reorder_steam_first_noop_when_disabled(self):
        settings = _stored_settings(
            games_to_watch=["Owned"], steam_owned_games=["Owned"], steam_prioritize=False
        )
        manager = SettingsManager(AsyncMock(), settings, MagicMock())
        self.assertFalse(manager.reorder_steam_first())

    def test_get_settings_exposes_schedule_choices(self):
        settings = _stored_settings(
            steam_sync_time="06:30", steam_sync_timezone="America/New_York"
        )
        manager = SettingsManager(AsyncMock(), settings, MagicMock())

        result = manager.get_settings()

        self.assertEqual(result["steam_sync_time"], "06:30")
        self.assertEqual(result["steam_sync_timezone"], "America/New_York")
        self.assertIn("America/New_York", result["steam_timezones"])
        self.assertTrue(result["steam_next_run"])

    async def test_update_validates_schedule_input(self):
        settings = _stored_settings()
        manager = _manager(settings)

        manager.update_settings(
            {"steam_sync_time": "13:30", "steam_sync_timezone": "America/New_York"}
        )
        await asyncio.sleep(0)
        self.assertEqual(settings.steam_sync_time, "13:30")
        self.assertEqual(settings.steam_sync_timezone, "America/New_York")

        with self.assertRaises(ValueError):
            manager.update_settings({"steam_sync_time": "99:99"})
        with self.assertRaises(ValueError):
            manager.update_settings({"steam_sync_timezone": "Not/AZone"})


class TestSteamEndpoints(unittest.IsolatedAsyncioTestCase):
    def _gui_manager(self, settings, available):
        manager = _manager(settings)
        manager._available_games = list(available)
        return SimpleNamespace(settings=manager)

    async def test_preview_matches_owned_games_with_campaigns(self):
        settings = _stored_settings(steam_api_key="KEY", steam_id64="76561198000000000")
        gui = self._gui_manager(settings, ["Game X", "Other"])
        with (
            patch.object(web_app, "gui_manager", gui),
            patch(
                "src.services.steam_service.fetch_owned_games",
                AsyncMock(
                    return_value=[OwnedSteamGame("Game X", 10), OwnedSteamGame("Nope", 0)]
                ),
            ),
        ):
            result = await web_app.steam_preview(SteamPreviewRequest())

        self.assertTrue(result["success"])
        self.assertEqual(result["matched"], ["Game X"])
        self.assertEqual(result["owned_count"], 2)
        self.assertEqual(result["available_count"], 2)

    async def test_preview_surfaces_steam_errors(self):
        from src.services.steam_service import SteamError

        settings = _stored_settings(steam_api_key="KEY", steam_id64="76561198000000000")
        gui = self._gui_manager(settings, [])
        with (
            patch.object(web_app, "gui_manager", gui),
            patch(
                "src.services.steam_service.fetch_owned_games",
                AsyncMock(side_effect=SteamError("invalid_api_key")),
            ),
        ):
            result = await web_app.steam_preview(SteamPreviewRequest())

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "invalid_api_key")

    async def test_import_adds_matches_and_prioritizes(self):
        settings = _stored_settings(
            steam_api_key="KEY",
            steam_id64="76561198000000000",
            games_to_watch=["NotOwned"],
        )
        gui = self._gui_manager(settings, ["Owned A"])
        with (
            patch.object(web_app, "gui_manager", gui),
            patch(
                "src.services.steam_service.fetch_owned_games",
                AsyncMock(return_value=[OwnedSteamGame("Owned A", 5)]),
            ),
        ):
            result = await web_app.steam_import(
                SteamImportRequest(
                    steam_id64="76561198000000000",
                    steam_auto_watch=True,
                    steam_prioritize=True,
                )
            )
        await asyncio.sleep(0)

        self.assertTrue(result["success"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(settings.games_to_watch, ["Owned A", "NotOwned"])
        self.assertTrue(settings.steam_auto_watch)
        self.assertTrue(settings.steam_prioritize)
        self.assertEqual(result["settings"]["steam_owned_count"], 1)

    async def test_import_surfaces_steam_errors(self):
        from src.services.steam_service import SteamError

        settings = _stored_settings(steam_api_key="KEY", steam_id64="76561198000000000")
        gui = self._gui_manager(settings, [])
        with (
            patch.object(web_app, "gui_manager", gui),
            patch(
                "src.services.steam_service.fetch_owned_games",
                AsyncMock(side_effect=SteamError("steam_unreachable")),
            ),
        ):
            result = await web_app.steam_import(SteamImportRequest())

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "steam_unreachable")

    async def test_refresh_endpoint_runs_import(self):
        settings = _stored_settings(
            steam_api_key="KEY",
            steam_id64="76561198000000000",
            games_to_watch=["NotOwned"],
        )
        gui = self._gui_manager(settings, ["Owned A"])
        with (
            patch.object(web_app, "gui_manager", gui),
            patch(
                "src.services.steam_watcher.fetch_owned_games",
                AsyncMock(return_value=[OwnedSteamGame("Owned A", 5)]),
            ),
        ):
            result = await web_app.steam_refresh()
        await asyncio.sleep(0)

        self.assertTrue(result["success"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["matched"], ["Owned A"])
        self.assertEqual(result["owned_count"], 1)
        self.assertEqual(settings.games_to_watch, ["NotOwned", "Owned A"])
        self.assertEqual(result["settings"]["steam_owned_count"], 1)

    async def test_refresh_endpoint_reports_unconfigured(self):
        settings = _stored_settings()
        gui = self._gui_manager(settings, ["Owned A"])
        with (
            patch.object(web_app, "gui_manager", gui),
            patch(
                "src.services.steam_watcher.fetch_owned_games", AsyncMock(return_value=[])
            ) as fetch,
        ):
            result = await web_app.steam_refresh()

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "missing_api_key")
        fetch.assert_not_called()

    async def test_refresh_endpoint_surfaces_steam_errors(self):
        from src.services.steam_service import SteamError

        settings = _stored_settings(steam_api_key="KEY", steam_id64="76561198000000000")
        gui = self._gui_manager(settings, [])
        with (
            patch.object(web_app, "gui_manager", gui),
            patch(
                "src.services.steam_watcher.fetch_owned_games",
                AsyncMock(side_effect=SteamError("steam_unreachable")),
            ),
        ):
            result = await web_app.steam_refresh()

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "steam_unreachable")


if __name__ == "__main__":
    unittest.main()
