# Steam integration

This fork of Twitch Drops Miner adds an optional Steam integration on top of the
upstream project. It can read the games you own on Steam, add the ones that currently
have Twitch Drops to **Games to Watch**, keep your most-played games at the top, and
re-check your library automatically once a day.

Everything here is **opt-in** and configured from the dashboard's **Settings** tab.
Nothing is sent anywhere except to Steam's official Web API and to Twitch, and your
Steam API key never leaves your own machine/container.

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Import from Steam](#import-from-steam)
- [Automatic daily check](#automatic-daily-check)
- [Prioritize by hours played](#prioritize-by-hours-played)
- [Unlinked campaigns in the queue](#unlinked-campaigns-in-the-queue)
- [Running this fork](#running-this-fork)
- [Where settings are stored](#where-settings-are-stored)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)

## What it does

| Feature | Where | What happens |
| --- | --- | --- |
| Import from Steam | Settings → Games to Watch → **Import from Steam** | Reads your owned Steam games and adds the ones that currently have Twitch Drops to Games to Watch |
| Automatic daily check | Settings → **Steam Auto-Check** | Once a day at a time you pick, re-reads your library and adds any newly-available drops |
| Prioritize by hours played | Settings → Steam Auto-Check | Keeps owned games above the rest, ordered by how much you've played them |
| "Connect account" warning | Main → **Wanted Drop Queue** | Shows reward campaigns that need an account link, in red, with a link to connect |

The miner itself still only *mines* watch-time Drops. Campaigns that require a
subscription/purchase, or that need an account link you haven't connected, are handled
as described below.

## Requirements

To read your Steam library you need three things. This is a one-time setup.

1. **A public game list.** In Steam: your profile → **Edit Profile** → **Privacy
   Settings** → set **Game details** to **Public**. If this is private, Steam returns
   an empty list and the import finds nothing.
2. **A Steam Web API key.** Free from <https://steamcommunity.com/dev/apikey>. Sign in,
   enter any name for the domain (e.g. `localhost`), and copy the key.
3. **Your SteamID64.** The 17-digit numeric ID. The easiest way: open
   <https://steamid.io>, paste your Steam profile link, and copy the **steamID64**
   value.

You do **not** need Steam installed to use this, and games do **not** need to be
installed — it uses the list of everything you've bought.

## Import from Steam

1. Open the dashboard and go to the **Settings** tab.
2. Under **Games to Watch**, click **Import from Steam**.
3. The popup shows the steps above. Paste your **SteamID64** and **Steam Web API key**.
4. Optionally tick **Automatically add new drops daily** and **Prioritize my Steam
   games** (see the next two sections).
5. Click **Fetch library**. The app lists owned games that currently have Twitch Drops.
6. Click **Add N games** to add them to Games to Watch.

Matching is forgiving: punctuation, symbols like `™`/`®`, and edition suffixes such as
"Definitive Edition" are ignored, so `Street Fighter™ 6` (Steam) still matches
`Street Fighter 6` (Twitch). Only games that currently have a Drop campaign are added.

## Automatic daily check

In **Settings → Steam Auto-Check**:

- ☑ **Check my Steam library automatically (daily)** turns the daily job on.
- **Time** uses two dropdowns (hour, and minutes in 5-minute steps) — no typing.
- **Timezone** is a dropdown of common zones, so it can't be mistyped.
- A **"Next automatic check: …"** line shows exactly when it will run next.

The job runs **once per day at the time you choose**, in the timezone you choose
(default `00:05` / `America/Chicago`). It is anchored to the clock, so restarts and
manual imports don't shift it. A manual import after the daily time simply counts as
that day's check.

Because the schedule is fixed, the "every 24 hours" behaviour is really "every day at
this time". If the container is offline at that moment, the check runs shortly after it
comes back up.

## Prioritize by hours played

Tick **Prioritize my Steam games (most-played owned games first)**. While enabled:

- Owned games are kept **above** games you don't own on Steam.
- Among owned games, the ones you've **played the most come first** (Steam's total
  playtime).
- This is applied when you import and on every daily check, so it stays in place.

If you'd rather order things yourself, turn the option off — then the daily check won't
reorder your list.

## Unlinked campaigns in the queue

Some campaigns require you to connect a game account to Twitch (for example a Ubisoft
or Warframe account). The miner can't earn those until the account is linked, but it
still shows them so you know what you're missing:

- The campaign appears in **Main → Wanted Drop Queue** with a red banner:
  **"NEED TO CONNECT ACCOUNT TO GET DROPS"**.
- A **Connect account** button opens Twitch's connect page for that game.
- These campaigns are shown but **not mined** until the account is linked, so the miner
  never wastes time on something it can't earn.

Once you connect the account and click **Reload**, the banner disappears and the
campaign is mined normally.

> Note: rewards that require a **purchase/subscription** (not watching) never appear in
> the queue, because the miner only mines watch-time Drops.

## Running this fork

This fork isn't published to a Docker registry, so build it locally from the source.

### Docker (recommended)

From the folder where you cloned this fork:

```bash
docker build -t twitch-drops-miner:local .
docker run -d \
  --name twitch-drops-miner \
  -p 8080:8080 \
  -e TZ=America/Chicago \
  -v "${PWD}/data:/app/data" \
  -v "${PWD}/logs:/app/logs" \
  twitch-drops-miner:local
```

Then open <http://localhost:8080>.

On **Windows PowerShell**, use `${PWD}` and quoted paths, and single-line the command:

```powershell
docker build -t twitch-drops-miner:local .
docker run -d --name twitch-drops-miner -p 8080:8080 -e TZ=America/Chicago -v "${PWD}\data:/app/data" -v "${PWD}\logs:/app/logs" twitch-drops-miner:local
```

> Set `TZ` to the timezone of the network you connect to Twitch from. A mismatched
> timezone can make Twitch refuse the login browser ("browser not supported"). The
> Steam schedule uses its own timezone setting and is independent of `TZ`.

### Keeping your data

The `data` folder holds your Twitch login, settings, and drop history. Keep it when you
rebuild so you don't have to sign in again. When this fork changes, re-run
`docker build` and recreate the container; your mounted `data` folder is preserved.

## Where settings are stored

All Steam settings live in `data/settings.json` alongside the rest of the miner's
settings. You normally change them from the dashboard, but they're readable/writable
directly if needed:

| Key | Meaning |
| --- | --- |
| `steam_id64` | Your 17-digit SteamID64 |
| `steam_api_key` | Your Steam Web API key (kept server-side; shown as a mask in the UI) |
| `steam_auto_watch` | `true` to run the daily check |
| `steam_prioritize` | `true` to keep owned games on top, most-played first |
| `steam_sync_time` | Daily time as `HH:MM` (24-hour) |
| `steam_sync_timezone` | IANA timezone name, e.g. `America/Chicago` |
| `steam_owned_games` | Last library read (names) — server-side only |
| `steam_playtime` | Last read playtime in minutes — server-side only |
| `steam_last_sync` | When the last successful check ran |

## Privacy and security

- The only network calls added are to Steam's `IPlayerService/GetOwnedGames` endpoint.
- Your **Steam Web API key is stored server-side** in `settings.json` and is never sent
  to the browser (the UI only shows a mask). Your full game list and playtime are also
  kept server-side.
- Treat the `data` folder (and `settings.json`) as private — it also contains your
  Twitch session.
- The Steam features are entirely opt-in; with them turned off, this fork behaves like
  upstream.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Import finds 0 games | "Game details" isn't **Public**, or the SteamID64/API key is wrong |
| `invalid_api_key` | The API key was mistyped or revoked — generate a new one |
| `invalid_steam_id` | The SteamID64 isn't the 17-digit number (you may have pasted the vanity name) |
| `steam_unreachable` / `steam_unavailable` | Temporary network or Steam outage — retry later |
| A game with drops isn't in the queue | Its campaign needs an account link (see the red banner), or its reward is purchase-based, or you already claimed it |
| Daily check didn't run at the time | The container was offline at that moment; it runs once it's back, and the "Next automatic check" line shows the schedule |
| Games got reordered after a check | **Prioritize my Steam games** is on — turn it off to control order yourself |
