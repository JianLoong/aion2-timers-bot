# aion2-timerbot

A Discord bot that sets timers and shows them, made for Aion 2's recurring content —
world bosses, rifts, sieges, daily and weekly resets.

- `/timer set` — named countdown, one-off or repeating, with an optional role/`@here` ping
- `/timer list` — what is running in this server, with Discord timestamps everyone reads in
  their own timezone
- `/timer cancel` — stop one
- `/timer preset list|start|add|remove` — save recurring spawn intervals so starting a boss
  timer is one command instead of typing a duration

Timers live in a SQLite file, so they survive restarts. A timer that was due while the bot
was offline still fires, once, and says how late it was — it does not replay every missed cycle.

## Start the bot

Do this once.

**1. Make a bot and copy its token.** https://discord.com/developers/applications →
*New Application* → *Bot* → *Reset Token* → copy. Nothing to toggle in the portal: the bot
enables only the plain GUILDS intent, which is not a privileged one.

**2. Invite it to your server** with the `bot` and `applications.commands` scopes.
`applications.commands` is the scope that makes `/timer` show up.

**3. Put the token in a `.env` file** next to `bot.py`:

```ini
DISCORD_TOKEN=paste-the-token-here
DEV_GUILD_ID=your-server-id
```

`DEV_GUILD_ID` is optional but worth setting: commands sync to that one server in seconds
instead of over Discord's global sync, which can take an hour. Right-click the server →
*Copy Server ID* (if that option is missing, turn on Developer Mode in Discord's advanced
settings).

**4. Run it** from this folder.

Windows — cmd or PowerShell:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python bot.py
```

Git Bash takes the same thing with forward slashes: `.venv/Scripts/python bot.py`.

Linux or macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python bot.py
```

After that first setup, starting it is just `.venv\Scripts\python bot.py`
(Linux/macOS: `.venv/bin/python bot.py`).

It is working when the log ends with `synced N commands` and `logged in as ...`.
Leave it running — Ctrl-C stops it. Timers are in `data/timers.db`, so they survive a restart.
If something looks wrong, set `LOG_LEVEL=DEBUG` in `.env`; the log names the failing command.
A missing or rejected token says so in one line instead of a traceback.

## Run with Docker

Skips the Python install and comes back by itself after a crash or reboot. Same commands on
Windows, macOS and Linux. You still need `.env` from step 3 above — the compose file reads it.

```bash
docker compose up -d --build     # build and start in the background
docker compose logs -f           # watch the log (Ctrl-C stops watching, not the bot)
docker compose down              # stop it
```

Timers live in `data/timers.db` on your machine, mounted into the container, so they survive
`docker compose down` and rebuilds — back up that one file. The compose file pins `DB_PATH`
inside the container so timers always land there, whatever your `.env` says. After changing
the code, run `docker compose up -d --build` again.

**Run one or the other, not both.** A local `python bot.py` and the container share the same
token and the same `data/timers.db`: you would have two bots answering Discord and two
processes writing one SQLite file. Stop the local one with Ctrl-C, or `docker compose down`,
before switching.

## Commands

### `/timer set name duration [repeat] [note] [ping] [channel] [replace]`

| Option | Meaning |
| --- | --- |
| `name` | What the timer is for, e.g. `World Boss`. One timer per name per server. |
| `duration` | `90m`, `2h30m`, `1d12h`, or a bare number meaning minutes. |
| `repeat` | Fire again every `duration` after each alert. |
| `note` | Extra text in the alert, e.g. `use the west portal`. |
| `ping` | `here`, `everyone`, or a role name / `@role` / role ID. |
| `channel` | Where to post. Defaults to the channel you used. |
| `replace` | Overwrite a timer that already has this name instead of erroring. |

Duration notes: `1h30m` needs a unit on every part — `1h30` is rejected rather than silently
read as 31 hours. The shortest timer is 1 second, the longest is 365 days.

### `/timer list`, `/timer cancel name`

`list` shows every timer in the server; `cancel` has autocomplete. Only the person who set a
timer, or someone who can manage channels, may cancel it. Confirmations are ephemeral; the
alerts themselves are posted in the chosen channel.

### `/timer preset list [category]`, `/timer preset start preset [delay] [repeat] [note] [ping] [channel] [replace]`

`start` defaults to repeating every preset interval, first firing one interval from now.
Pass `delay` when you know the real timing — e.g. you just killed the boss and it respawns in
3h50m: `/timer preset start preset:"World Boss" delay:3h50m`.

### `/timer preset add` and `/timer preset remove` (Manage Server)

Save your own presets per server, e.g.
`/timer preset add name:"Ancient Boss" interval:3h30m category:"Open world" aliases:ab`.
Server presets show as `(server)` in autocomplete and can be deleted in Discord; built-in ones
are edited in the preset file.

## Presets

`aion2_timerbot/data/presets.json` ships starter presets. **The intervals in it are examples,
not official Aion 2 spawn data** — edit them to match your server's schedule:

```json
{
  "presets": [
    { "name": "World Boss", "interval": "4h", "category": "Open world", "aliases": ["wb"] }
  ]
}
```

`name` and `interval` are required; `category`, `aliases` and `note` are optional. Point
`PRESETS_FILE` at another file to keep your edits out of the package. A server preset with the
same name replaces the built-in one and inherits its aliases, so `wb` keeps working.

## Configuration

All in `.env` (see `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Required. |
| `DB_PATH` | `data/timers.db` | Where timers are stored. |
| `DEV_GUILD_ID` | — | Sync commands to one guild instantly. |
| `PRESETS_FILE` | bundled file | Alternative preset file. |
| `TICK_SECONDS` | `5` | How often due timers are checked. |
| `MISSED_AFTER_SECONDS` | `120` | A timer later than this says it fired late. |
| `MAX_TIMERS_PER_GUILD` | `100` | Cap per server. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## Hosting

It is a long-running process holding one SQLite file, so run it under something that restarts
it. `docker compose up -d` (above) is the least work and behaves the same on a VPS as on your
desktop; otherwise run `python bot.py` under systemd with the venv's interpreter. Back up
`data/timers.db` if the schedule matters. Deleting a message it posted is harmless — timers are
in the database, not reconstructed from Discord.

## Development

```bash
.venv/Scripts/python.exe -m pytest -q
```

The suite runs offline, with no token and no gateway connection: the command layer is driven
through fake Discord objects (`tests/conftest.py`).

```
bot.py                        entrypoint: config, store, presets, command sync
docker-compose.yml            one-command start (see "Run with Docker")
Dockerfile                    image: python:3.14-slim, non-root, timers in /app/data
aion2_timerbot/
  duration.py                 "2h30m" parsing and formatting
  models.py                   Timer, GuildPreset
  db.py                       SQLite store (timers, per-guild presets)
  scheduler.py                polls for due timers, advances repeating ones
  presets.py                  preset library, built-ins merged with per-guild ones
  config.py                   environment configuration
  data/presets.json           starter presets (edit these)
  cogs/timers.py              the slash commands and the alerts
tests/                        pytest suite
```
