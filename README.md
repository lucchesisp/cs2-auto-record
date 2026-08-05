# CS2 Auto Record

OBS script that records your Counter-Strike 2 matches hands-free: it starts recording when a match goes live, stops when it ends, renames the file with the match result, and can upload it straight to YouTube.

```
Senkes 13-10｜KDA 24/15｜Rating 1.42｜MIRAGE｜FACEIT｜Aug 05, 2026.mp4
Senkes 7-13｜KDA 12/18｜INFERNO｜MM｜Aug 05, 2026.mp4
Senkes 16-14｜KDA 30/22｜ANCIENT｜PREMIER｜Aug 05, 2026.mp4
```

> Windows does not allow `|` in file names, so the fullwidth `｜` (U+FF5C) is used instead. YouTube titles get the real `|` back.

## How it works

1. CS2's built-in [Game State Integration](https://developer.valvesoftware.com/wiki/Counter-Strike:_Global_Offensive_Game_State_Integration) posts live match data to a small local HTTP server the script runs inside OBS.
2. When the match goes live, OBS starts recording. On game over (or when you leave the server), it stops.
3. The script figures out where you played:

   | Platform | Detection | Stats source |
   |---|---|---|
   | FACEIT | Match appears in your FACEIT history | FACEIT Data API (includes an HLTV 1.0-style rating) |
   | Premier | GSI reports `premier` mode | In-game stats from GSI |
   | Gamers Club | Knife round detected (knife-only inventory in round 0) | In-game stats from GSI |
   | Valve MM | Competitive mode with no FACEIT match found | In-game stats from GSI |
   | Wingman | GSI reports `scrimcomp2v2` mode | In-game stats from GSI |

4. The recording is renamed (the remuxed copy too, if you use auto-remux) and optionally uploaded to YouTube in the background.

## Requirements

- Windows, OBS Studio 30+
- Python 3.11 or 3.12 (64-bit) for OBS scripting — OBS does not load newer versions yet
- Any Python 3.10+ on the system for the YouTube uploader (can be the same one)
- A free FACEIT account (for the API key)

## Setup

### 1. OBS script

1. Install [Python 3.12 x64](https://www.python.org/downloads/) if you don't have it.
2. In OBS: `Tools → Scripts → Python Settings` and point it to the Python install folder.
3. Back on the `Scripts` tab, click `+` and add `faceit_auto_rename.py`.

### 2. CS2 game state integration

Copy `gamestate_integration_obs_autorecord.cfg` into your CS2 cfg folder:

```
C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg
```

Restart CS2 afterwards. If you change the GSI port in the script settings, change it in this file too.

### 3. FACEIT API key

1. Go to [developers.faceit.com](https://developers.faceit.com) and sign in with your FACEIT account.
2. Create an app (name and description can be anything).
3. Open the app → `API Keys` → create a **server-side** key.
4. Paste the key into the script settings in OBS, along with your exact FACEIT nickname.

### 4. YouTube upload (optional)

The uploader needs its own Google credential because videos go to *your* channel:

1. Install the client libraries:
   ```
   py -3 -m pip install google-api-python-client google-auth-oauthlib
   ```
2. In [Google Cloud Console](https://console.cloud.google.com), create a project and enable the **YouTube Data API v3** (`APIs & Services → Library`).
3. `APIs & Services → OAuth consent screen`: configure it as **External**, and add your own Google account under **Test users**.
4. `Credentials → Create credentials → OAuth client ID` → application type **Desktop app**. Download the JSON and save it as `client_secret.json` next to `youtube_upload.py`.
5. Authorize once (opens a browser):
   ```
   py -3 youtube_upload.py --auth-only
   ```
6. In the OBS script settings, enable **Upload to YouTube after rename** and pick the privacy level.

Good to know:

- Google locks uploads from unverified OAuth apps to **private**. You can publish manually afterwards, or go through Google's app verification.
- Channels without [phone verification](https://www.youtube.com/verify) can't upload videos longer than 15 minutes — a full match always is.
- The default API quota allows about 6 uploads per day.
- While the consent screen is in *Testing* status, tokens expire every 7 days; publish the app to *In production* to avoid weekly re-login.

## Script settings

| Setting | Description |
|---|---|
| FACEIT nickname | Your exact FACEIT nickname (used for API lookups) |
| Display name in filename | Optional different name to show in file names |
| FACEIT API key | Server-side key from developers.faceit.com |
| FACEIT template | File name template for FACEIT matches |
| MM/Premier/GC template | File name template for everything else |
| Wait for FACEIT stats (min) | How long to wait for FACEIT to publish stats before assuming the match was MM |
| Retry interval (s) | Time between FACEIT API polls |
| Auto start/stop recording with CS2 (GSI) | Toggle the automation |
| GSI port | Local port for the GSI listener (must match the cfg) |
| Stop delay after leaving server (s) | How long to wait without game data before assuming you left the server |
| Upload to YouTube after rename | Toggle uploads |
| YouTube privacy | private / unlisted / public |
| Upload speed limit (MB/s) | Caps upload bandwidth so a running upload doesn't cause ping spikes in your next match (0 = unlimited) |

Template placeholders: `{nickname}` `{score}` `{kda}` `{kd}` `{kills}` `{deaths}` `{assists}` `{rating}` `{map}` `{platform}` `{date}`.

The score is always from your team's perspective. `{rating}` is an HLTV 1.0-style rating computed from the FACEIT stats (kills, deaths, survival and multi-kills per round); for non-FACEIT matches it falls back to K/D.

## Troubleshooting

- All script activity is logged in `Tools → Scripts → Script Log` with the `[faceit-rename]` prefix; uploads log to `youtube_upload.log`.
- Nothing triggers in game: make sure CS2 was restarted after copying the cfg, and that the GSI port matches.
- `FACEIT nickname not found`: the nickname must match your FACEIT profile exactly (check the URL: `faceit.com/en/players/<nickname>`).
- A practice/scrim server in competitive mode also triggers recording; it gets labeled `MM` (or `GC` if it has a knife round) since GSI can't tell servers apart.
- Recording never stops after you leave mid-match: it stops on its own after the game stops sending data (configurable, 45 s by default).

## License

MIT
