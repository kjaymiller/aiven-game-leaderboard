# Aiven Game Leaderboard

A shared leaderboard, score-claim and events service for games in the Aiven ecosystem.
It was extracted from [inkless-game-web](https://github.com/Aiven-Labs/inkless-game-web)
(branch `spacetimedb-authoritative`), with the history of `score_server/` preserved.
Disk Invaders is still its first game.

Built with FastAPI and PostgreSQL.

## What it does

- **Games**: any number of games, each with its own score label, format (`int`, `money`,
  `time_ms`) and sort order.
- **Per-game API tokens**: a game server posts scores with a bearer token scoped to one game.
- **Events**: self-serve events (conferences, booths) that can overlap in time and run several
  games each. Every (event, game) pair gets its own leaderboard, game link, cabinet URL and QR
  codes. See [EVENTS.md](EVENTS.md).
- **Claims**: players claim a score with a nickname and email, usually from a QR code.
- **Admin console** at `/admin` for scores, games, events and tokens, plus CSV export.

## Adding a game

The integration guide is served at `/guide`. Its source is [app/guide.md](app/guide.md).
In short: create the game and a token in `/admin`, then have your game **server**
`POST /api/v1/scores`.

## Endpoints

| Path | Purpose |
|---|---|
| `GET /api/v1/games` | Public list of games |
| `GET /api/v1/games/{game}/leaderboard?event=&source=&limit=` | Public leaderboard |
| `POST /api/v1/scores` | Game server submits a score (bearer token) |
| `/api/v1/admin/...` | Admin API (admin token or console session) |
| `GET /api/events` | Public list of events |
| `/claim/{session_id}` | Player claim page |
| `/leaderboard?game=&event=` | Leaderboard page |
| `/admin` | Admin console |
| `/guide` | Integration guide |
| `/docs` | OpenAPI docs |

The legacy Disk Invaders endpoints (`/api/scores`, `/api/leaderboard`,
`/api/check-high-score/...`) are kept so the current game keeps working.

## Signing in and API tokens

**Console sign-in.** Open `/admin` and enter the `ADMIN_KEY` password. The server swaps it for
a signed, HttpOnly session cookie (12 hours). `ADMIN_KEY` must be set: if it is not, the
console fails closed and `POST /admin/login` returns `503`. Rotating `ADMIN_KEY` ends every
live session. Tokens cannot be used on the console password form.

**Tokens (API tab).** The console's **API** tab mints database-backed tokens
(`GET/POST/DELETE /admin/api/tokens`, console session only). Pick a kind:

- **Game token**: scoped to one game; lets that game's server `POST /api/v1/scores`.
- **Admin token**: can call the management API (`/api/v1/admin/...`, see `/docs`) but cannot
  create or revoke tokens.

The token (`lbk_...`) is shown **once** when created. Only its hash is stored, so a lost token
has to be revoked and replaced. Treat both kinds as secrets; an admin token gives full
management access to scores, games and events. Revoke tokens you no longer need in the API tab.

Send a token as a bearer header. No session-cookie exchange is needed:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/api/v1/admin/games
```

## Configuration

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL connection string. Tables are created and migrated at startup |
| `ADMIN_KEY` | yes | Admin console password. If unset, `/admin/login` returns 503 and the legacy `/api/admin/emails` rejects every request |
| `INGEST_SECRET` | no | When set, the legacy `/api/scores` endpoint requires `X-Ingest-Secret` |
| `CORS_ORIGINS` | no | Comma-separated origins |
| `PUBLIC_LEADERBOARD_URL` | no | Stable base URL for event leaderboard links and QR codes. Default: the host the admin console is opened on |
| `PUBLIC_GAME_URL` | no | Only used once, on upgrade, to seed Disk Invaders' play URL (default `https://inkless.dev`). Game links now come from each game's play URL |
| `LOG_LEVEL` | no | Default `INFO` |

## Running locally

With Docker:

```bash
docker compose up --build        # http://localhost:8000/admin  (password: local_dev_admin_key)
```

Without Docker, against any Postgres:

```bash
uv venv && uv pip install -r requirements.txt
DATABASE_URL=postgresql://... ADMIN_KEY=dev uv run uvicorn app.main:app --reload
```

## Tests

`tests/api_smoke.py` is a stdlib-only end-to-end check. It creates a throwaway game, so run
it against a local or staging instance, never production:

```bash
ADMIN_KEY=local_dev_admin_key INGEST_SECRET=local_dev_ingest python3 tests/api_smoke.py http://localhost:8000
```
