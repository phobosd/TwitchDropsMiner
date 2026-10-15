"""Steam library integration via the Steam Web API.

Fetches the games a user owns (installed or not) so they can be matched
against Twitch drop campaigns and added to "Games to Watch".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from src.diagnostics import Diagnostics


logger = logging.getLogger("TwitchDrops")


@dataclass(frozen=True)
class OwnedSteamGame:
    """A single game owned on Steam, with the total minutes played."""

    name: str
    playtime_minutes: int = 0

STEAM_OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"

# A SteamID64 is a 17-digit unsigned 64-bit account identifier.
STEAM_ID64_PATTERN = re.compile(r"^\d{17}$")

# Symbols publishers put on Steam store names that Twitch categories omit.
_DECORATION_CHARS = str.maketrans(dict.fromkeys("\u00ae\u2122\u2120\u00a9", " "))

# Trailing edition markers like "Game X: Definitive Edition".
_EDITION_PATTERN = re.compile(
    r"\b(?:definitive|deluxe|ultimate|complete|gold|premium|standard|special|"
    r"collector'?s|enhanced|game of the year|goty)\s+edition\b"
)


class SteamError(Exception):
    """Raised when the Steam Web API cannot return an owned-games list."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_game_name(name: str, *, strip_edition: bool = False) -> str:
    """Reduce a game name to a comparable key.

    ``strip_edition`` additionally drops a trailing edition marker so that
    "Game X: Definitive Edition" can match Twitch's "Game X".
    """
    text = str(name or "").lower().replace("&", " and ")
    text = text.translate(_DECORATION_CHARS)
    if strip_edition:
        text = _EDITION_PATTERN.sub(" ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def match_available_games(owned_names: list[str], available_names: list[str]) -> list[str]:
    """Return the Twitch category names that match an owned Steam game.

    Matching is exact on the normalized name first, then falls back to a
    normalized name with any edition marker removed.
    """
    exact: dict[str, str] = {}
    editionless: dict[str, str] = {}
    for name in available_names:
        exact.setdefault(normalize_game_name(name), name)
        editionless.setdefault(normalize_game_name(name, strip_edition=True), name)

    matched: list[str] = []
    seen: set[str] = set()
    for owned in owned_names:
        candidates = (
            exact.get(normalize_game_name(owned)),
            editionless.get(normalize_game_name(owned, strip_edition=True)),
        )
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                matched.append(candidate)
                break
    return matched


async def fetch_owned_games(
    api_key: str,
    steam_id64: str,
    *,
    diagnostics: Diagnostics | None = None,
    timeout: float = 15.0,
) -> list[OwnedSteamGame]:
    """Return every game the account owns, installed or not, with playtime.

    Raises:
        SteamError: with a machine-readable ``code`` when the request cannot
            be completed or the credentials are rejected.
    """
    api_key = (api_key or "").strip()
    steam_id64 = (steam_id64 or "").strip()
    if not api_key:
        raise SteamError("missing_api_key")
    if not STEAM_ID64_PATTERN.match(steam_id64):
        raise SteamError("invalid_steam_id")

    import aiohttp

    params = {
        "key": api_key,
        "steamid": steam_id64,
        "include_appinfo": "1",
        "include_played_free_games": "1",
        "format": "json",
    }
    try:
        async with aiohttp.ClientSession() as session, session.get(
            STEAM_OWNED_GAMES_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if diagnostics is not None:
                diagnostics.record_http("GET", STEAM_OWNED_GAMES_URL, response.status)
            if response.status in (401, 403):
                raise SteamError("invalid_api_key")
            if response.status != 200:
                raise SteamError("steam_unavailable")
            payload = await response.json(content_type=None)
    except SteamError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a code
        if diagnostics is not None:
            outcome = "timeout" if isinstance(exc, TimeoutError) else "connection"
            diagnostics.record("steam", outcome=outcome)
        raise SteamError("steam_unreachable") from exc

    games = (payload or {}).get("response", {}).get("games")
    if not games:
        # An empty/absent list usually means the profile's game details are
        # private; callers surface this as "0 owned games".
        return []
    owned: list[OwnedSteamGame] = []
    for game in games:
        name = game.get("name")
        if not name:
            continue
        try:
            minutes = int(game.get("playtime_forever") or 0)
        except (TypeError, ValueError):
            minutes = 0
        owned.append(OwnedSteamGame(str(name), max(minutes, 0)))
    return owned
