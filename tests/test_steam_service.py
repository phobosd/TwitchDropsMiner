import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.steam_service import (
    OwnedSteamGame,
    SteamError,
    fetch_owned_games,
    match_available_games,
    normalize_game_name,
)


class MockResponseContext:
    def __init__(self, response_or_exc):
        self.response_or_exc = response_or_exc

    async def __aenter__(self):
        if isinstance(self.response_or_exc, Exception):
            raise self.response_or_exc
        return self.response_or_exc

    async def __aexit__(self, exc_type, exc, tb):
        pass


class TestNameMatching(unittest.TestCase):
    def test_normalize_strips_decoration_and_punctuation(self):
        self.assertEqual(normalize_game_name("Game™: X & Y"), "game x and y")
        self.assertEqual(normalize_game_name("  ACE  COMBAT  "), "ace combat")

    def test_normalize_can_strip_edition(self):
        self.assertEqual(
            normalize_game_name("Game X: Definitive Edition", strip_edition=True),
            "game x",
        )

    def test_match_exact_and_edition_fallback(self):
        owned = ["Counter-Strike 2", "Game X: Definitive Edition", "Not A Drop Game"]
        available = ["Counter-Strike 2", "Game X", "Other Game"]
        self.assertEqual(
            match_available_games(owned, available),
            ["Counter-Strike 2", "Game X"],
        )

    def test_match_is_case_insensitive_and_deduplicated(self):
        owned = ["final fantasy", "FINAL FANTASY"]
        self.assertEqual(
            match_available_games(owned, ["Final Fantasy"]), ["Final Fantasy"]
        )

    def test_no_matches(self):
        self.assertEqual(match_available_games(["Nothing"], ["Something"]), [])


class TestFetchOwnedGames(unittest.TestCase):
    def setUp(self):
        self.session_patcher = patch("aiohttp.ClientSession")
        self.mock_session_cls = self.session_patcher.start()
        self.mock_session = MagicMock()
        self.mock_session_cls.return_value.__aenter__.return_value = self.mock_session

    def tearDown(self):
        self.session_patcher.stop()

    def _respond(self, status, payload=None):
        response = AsyncMock()
        response.status = status
        response.json = AsyncMock(return_value=payload)
        self.mock_session.get.side_effect = lambda *args, **kwargs: MockResponseContext(
            response
        )

    def test_missing_api_key(self):
        with self.assertRaises(SteamError) as ctx:
            asyncio.run(fetch_owned_games("", "76561198000000000"))
        self.assertEqual(ctx.exception.code, "missing_api_key")

    def test_invalid_steam_id(self):
        with self.assertRaises(SteamError) as ctx:
            asyncio.run(fetch_owned_games("KEY", "123"))
        self.assertEqual(ctx.exception.code, "invalid_steam_id")

    def test_returns_owned_games_with_playtime(self):
        self._respond(
            200,
            {
                "response": {
                    "games": [
                        {"name": "A", "playtime_forever": 120},
                        {"name": "B"},
                        {},
                    ]
                }
            },
        )
        owned = asyncio.run(fetch_owned_games("KEY", "76561198000000000"))
        self.assertEqual(
            owned, [OwnedSteamGame("A", 120), OwnedSteamGame("B", 0)]
        )

    def test_rejected_key(self):
        self._respond(403)
        with self.assertRaises(SteamError) as ctx:
            asyncio.run(fetch_owned_games("KEY", "76561198000000000"))
        self.assertEqual(ctx.exception.code, "invalid_api_key")

    def test_private_profile_returns_empty(self):
        self._respond(200, {"response": {}})
        self.assertEqual(asyncio.run(fetch_owned_games("KEY", "76561198000000000")), [])

    def test_unreachable(self):
        self.mock_session.get.side_effect = lambda *args, **kwargs: MockResponseContext(
            Exception("boom")
        )
        with self.assertRaises(SteamError) as ctx:
            asyncio.run(fetch_owned_games("KEY", "76561198000000000"))
        self.assertEqual(ctx.exception.code, "steam_unreachable")


if __name__ == "__main__":
    unittest.main()
