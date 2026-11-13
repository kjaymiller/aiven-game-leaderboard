from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncpg
import segno
from datetime import datetime, timezone
import asyncio
import csv
import hashlib
import hmac
import html
import secrets
import io
import json
import logging
import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager

from .database import database
from .models import (
    EventUpsert,
    GameUpsert,
    ScoreSubmission,
    ScoreV1,
    TokenCreate,
    ClaimData,
    ScoreResponse,
    HighScoreCheck,
    ClaimResponse,
    AdminScoreUpdate,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await database.connect()
    logger.info("Application started")
    yield
    # Shutdown
    await database.disconnect()
    logger.info("Application stopped")

app = FastAPI(
    title="Inkless Game Score API",
    description="High score system for the Inkless space invaders game",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),  # In production, specify your game's domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static game build (the arcade frontend).
#
# `docs/` is the canonical build directory: `npm run build` writes docs/game.js
# into it and rollup's dev server uses it as its contentBase. Mounting it here
# means the game and the score API share an origin, so the arcade build needs
# neither CORS nor a hardcoded API host.
GAME_STATIC_DIR = os.getenv(
    "GAME_STATIC_DIR",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "docs",
    ),
)

class GameStaticFiles(StaticFiles):
    """StaticFiles that stops browsers serving a stale game after a deploy.

    Starlette sends only last-modified and etag, with no Cache-Control. Browsers
    then apply heuristic freshness and can keep a cached game.js for a long time
    without revalidating - so a deployed change silently fails to reach the
    arcade cabinet and anyone who has played before.

    index.html, game.js and game.config.js change on every deploy, so they are
    marked no-cache: the browser must revalidate, and the etag makes that a cheap
    304 when nothing changed. Assets keep a normal cache lifetime; they are large
    and effectively immutable.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)

        content_type = response.headers.get("content-type", "")
        revalidate = path.endswith((".html", ".js")) or content_type.startswith("text/html")

        response.headers["Cache-Control"] = (
            "no-cache" if revalidate else "public, max-age=86400"
        )

        return response


def _write_stdb_config() -> None:
    """Point the game client at the SpacetimeDB server named by the environment.

    Same job as the nginx image's entrypoint: the client loads stdb-config.js
    before its own config, so the address is a deploy setting, not baked into
    the bundle.
    """
    uri = os.getenv("STDB_URI", "").strip()
    if not uri or not os.path.isdir(GAME_STATIC_DIR):
        return
    db_name = os.getenv("STDB_DATABASE", "inkless-game").strip() or "inkless-game"
    try:
        with open(os.path.join(GAME_STATIC_DIR, "stdb-config.js"), "w") as f:
            f.write(f"window.INKLESS_STDB_URI = {json.dumps(uri)};\n")
            f.write(f"window.INKLESS_STDB_DATABASE = {json.dumps(db_name)};\n")
        logger.info(f"Game client will connect to {uri} ({db_name})")
    except OSError as e:
        logger.warning(f"Could not write stdb-config.js: {e}")


_write_stdb_config()

if os.path.isdir(GAME_STATIC_DIR):
    # html=True serves index.html for /game/, and Starlette redirects /game to
    # /game/ so the game's relative asset paths resolve inside /game/ rather
    # than against the site root.
    app.mount("/game", GameStaticFiles(directory=GAME_STATIC_DIR, html=True), name="game")
    # The same build is also served at /qrcode/ for the arcade cabinet. The bundle picks its
    # end-of-game flow from the URL it is loaded at: /game/ shows an on-screen sign-up form,
    # /qrcode/ shows a QR code that stays up until the score is scanned.
    app.mount("/qrcode", GameStaticFiles(directory=GAME_STATIC_DIR, html=True), name="qrcode")
    logger.info(f"Serving game build from {GAME_STATIC_DIR} at /game and /qrcode")
else:
    logger.warning(f"No game build at {GAME_STATIC_DIR}; /game will return 404")

def format_money(amount: int) -> str:
    """Format money display"""
    if amount >= 1000:
        return f"${amount/1000:.1f}k"
    return f"${amount}"


def format_value(fmt, amount):
    """Show a score the way its game defines: int (1,234), money ($1.2k) or time_ms (1:02.345)"""
    if amount is None:
        return None
    if fmt == "money":
        return format_money(amount)
    if fmt == "time_ms":
        minutes, rest = divmod(int(amount), 60000)
        return f"{minutes}:{rest / 1000:06.3f}"
    return f"{int(amount):,}"


DEFAULT_GAME = "disk-invaders"


def _json_or_none(value):
    """asyncpg hands jsonb back as text"""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _game_public(game: dict) -> dict:
    return {
        "slug": game["slug"], "name": game["name"],
        "score_label": game["score_label"], "score_format": game["score_format"],
        "sort_order": game["sort_order"],
        "secondary_label": game["secondary_label"], "secondary_format": game["secondary_format"],
        "highscore_text": game["highscore_text"],
        "play_url": game.get("play_url"),
        "cabinet_url": game.get("cabinet_url"),
    }


async def _games_by_slug() -> dict:
    return {g["slug"]: g for g in await database.list_games()}


def _displays(row: dict, game: dict | None):
    """(score, secondary) formatted for a row, using its game's formats"""
    game = game or {"score_format": "int", "secondary_format": "int", "secondary_label": None}
    score = format_value(game["score_format"], row["score"])
    has_secondary = row.get("secondary") is not None and game.get("secondary_label")
    secondary = format_value(game.get("secondary_format") or "int", row["secondary"]) if has_secondary else None
    return score, secondary

@app.get("/")
async def root():
    """API health check"""
    return {
        "message": "Inkless Game Score API",
        "status": "healthy",
        "game": "/game/",
        "arcade": "/qrcode/",
        "leaderboard": "/leaderboard",
    }


AQUARIUM_CSS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aquarium-dark.css")

# Shared <head> tags for every page: Inter (Aquarium's typeface) and the dark Aquarium tokens.
# Absolute path so it also resolves inside the inkless.dev leaderboard iframe.
AQUARIUM_HEAD = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap">'
    '<link rel="stylesheet" href="/assets/aquarium-dark.css">'
)


@app.get("/assets/aquarium-dark.css", include_in_schema=False)
async def aquarium_css():
    """Aiven Aquarium design tokens and base styles (dark), shared by every page"""
    return FileResponse(AQUARIUM_CSS_PATH, media_type="text/css",
                        headers={"Cache-Control": "public, max-age=300"})


def _simple_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """A small centred Aquarium card page (not found, already claimed). `body` must be safe HTML."""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    {AQUARIUM_HEAD}
    <style>
        body {{ padding: var(--aq-space-6) var(--aq-space-5); }}
        .aq-card {{ max-width: 560px; margin: var(--aq-space-8) auto; text-align: center; }}
        .aq-card h1 {{ margin: 0 0 var(--aq-space-5); }}
        .score {{ background: var(--aq-bg-raised); border-radius: var(--aq-radius); padding: var(--aq-space-5);
                  margin: var(--aq-space-5) 0; font-size: var(--aq-fs-large); }}
        .score strong {{ color: var(--aq-text-muted); font-weight: 500; }}
        .claimed {{ margin: var(--aq-space-5) 0; }}
        .claimed strong {{ color: var(--aq-success); }}
        .meta {{ color: var(--aq-text-subtle); font-size: var(--aq-fs-small); }}
        .meta p {{ margin: var(--aq-space-2) 0; }}
        p {{ color: var(--aq-text-muted); }}
    </style>
</head>
<body>
    <div class="aq-card">
        <h1>{html.escape(title)}</h1>
        {body}
    </div>
</body>
</html>""", status_code=status_code)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the game's favicon at the site root"""
    icon_path = os.path.join(GAME_STATIC_DIR, "favicon.ico")
    if os.path.isfile(icon_path):
        return FileResponse(icon_path)
    raise HTTPException(status_code=404, detail="Not found")

def _check_ingest_secret(provided: str | None) -> None:
    """Only the game server may create scores once INGEST_SECRET is configured.

    With the SpacetimeDB build the score is computed server-side and posted here by
    the module, so a client must not be able to POST its own. Unset keeps the
    legacy behaviour (anyone can submit) for the old client-side build.
    """
    expected = os.getenv("INGEST_SECRET")
    if not expected:
        return
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")


@app.post("/api/scores", response_model=ScoreResponse)
async def submit_score(score: ScoreSubmission, x_ingest_secret: str | None = Header(default=None)):
    """Submit a game score"""
    _check_ingest_secret(x_ingest_secret)
    try:
        logger.info(f"Attempting to submit score: {score}")
        
        await database.submit_score(
            score.session_id, 
            score.final_bill, 
            score.total_savings, 
            score.timestamp,
            score.source,
            score.event
        )
        
        logger.info(f"Score submitted for session {score.session_id}: bill=${score.final_bill}, savings=${score.total_savings}")
        
        return ScoreResponse(
            success=True,
            session_id=score.session_id,
            message="Score submitted successfully"
        )
        
    except Exception as e:
        logger.error(f"Error submitting score: {e}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Error details: {str(e)}")
        
        if "duplicate key" in str(e).lower():
            raise HTTPException(status_code=400, detail=f"Session ID already exists: {score.session_id}")
        
        # For debugging, return more detailed error info
        raise HTTPException(status_code=500, detail=f"Failed to submit score: {str(e)}")

@app.get("/api/check-high-score/{session_id}", response_model=HighScoreCheck)
async def check_high_score(session_id: str):
    """Check if a score is a high score"""
    try:
        result = await database.check_high_score(session_id)
        if not result:
            raise HTTPException(status_code=404, detail="Score not found")
        
        return HighScoreCheck(**result)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking high score: {e}")
        raise HTTPException(status_code=500, detail="Failed to check high score")

@app.get("/api/scores/{session_id}/status")
async def score_status(session_id: str):
    """Lightweight poll target for the arcade cabinet: has this score been claimed yet?

    The cabinet leaves the QR code and claim button up until the score is claimed (or it
    times out), polling this rather than a fixed on-screen timer, so it doesn't take the QR
    code away before someone's had a chance to scan it.
    """
    try:
        row = await database.get_score(session_id)
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")

        return {"claimed": row["claimed_at"] is not None}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking score status: {e}")
        raise HTTPException(status_code=500, detail="Failed to check score status")

@app.get("/claim/{session_id}", response_class=HTMLResponse)
async def claim_page(session_id: str, request: Request):
    """Display the claim page for a session"""
    try:
        row = await database.get_score(session_id)
        
        if not row:
            return _simple_page(
                "Score not found",
                "<p>The score you're looking for doesn't exist or has been removed.</p>"
                '<p><a href="/leaderboard">View the leaderboard</a></p>',
                status_code=404,
            )
        
        game_cfg = await database.get_game(row['game'])
        score_display, secondary_display = _displays(dict(row), game_cfg)
        score_label = html.escape(game_cfg['score_label'] if game_cfg else 'Score')
        secondary_label = html.escape((game_cfg or {}).get('secondary_label') or '')
        # The score lines shown on the "already claimed" and "claim" pages
        score_block = f"<strong>{score_label}:</strong> {score_display}"
        if secondary_display is not None:
            score_block += f"<br><strong>{secondary_label}:</strong> {secondary_display}"
        score_block_form = (
            f'<div><strong>{score_label}:</strong> <span class="savings-value">{score_display}</span></div>'
            + (f'<div style="margin-top: 10px;"><strong>{secondary_label}:</strong> <span class="bill-value">{secondary_display}</span></div>'
               if secondary_display is not None else '')
        )
        # Goes inside a JS template literal, so neutralise backticks, backslashes and ${
        _hs = (game_cfg or {}).get('highscore_text') or 'Your score: {score}'
        highscore_line = html.escape(_hs.replace('{score}', str(score_display))) \
            .replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
        total_savings_formatted = score_display  # kept: used below by older fragments

        # Post-claim CTA: the score's event can set its own, otherwise the Inkless one
        cta_text, cta_url = "Learn More About Inkless →", "https://aiven.io/inkless"
        if row.get('event'):
            try:
                event_cta = await database.get_event_cta(row['event'])
                if event_cta:
                    cta_text, cta_url = event_cta
            except Exception as e:
                logger.warning(f"Could not load CTA for event {row['event']}: {e}")
        # "Back to game" link: only ever the game's admin-set play_url (validated http(s)), else "/game/"
        back_url = "/game/"
        play_tpl = (game_cfg or {}).get('play_url')
        if play_tpl:
            if row.get('event'):
                back_url = _fill_event(play_tpl, row['event']) or "/game/"
            elif '{event}' not in play_tpl:
                back_url = play_tpl
        back_url = html.escape(back_url, quote=True)
        claim_cta = (
            '<div class="thank-you" id="claimCta" hidden>'
            '<h2>Thank you for playing!</h2>'
            "<p>You've been added to our Inkless updates list. We'll keep you informed "
            'about new features and product releases.</p>'
            f'<a href="{html.escape(cta_url)}" target="_blank" rel="noopener noreferrer">{html.escape(cta_text)}</a>'
            '</div>'
        )
        
        # If already claimed
        if row['email']:
            company_display = (f"<br>{html.escape(row['company'])}" if row.get('company') else "")
            return _simple_page("Score already claimed", f"""
                <div class="score">{score_block}</div>
                <div class="claimed">Claimed by<br><strong>{html.escape(row['nickname'] or 'Anonymous')}</strong>{company_display}</div>
                <div class="meta">
                    <p>Claimed {row['claimed_at'].strftime('%B %d, %Y at %I:%M %p')}</p>
                    <p>Played {row['timestamp'].strftime('%B %d, %Y at %I:%M %p')}</p>
                </div>""")
        
       # Show claim form
        return HTMLResponse(f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <title>Claim Your Score - Inkless Game</title>
                {AQUARIUM_HEAD}
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    /* Aquarium (dark) via /assets/aquarium-dark.css; layout only here */
                    body {{
                        padding: var(--aq-space-6) var(--aq-space-5);
                        min-height: 100vh;
                    }}
                    .container {{ max-width: 560px; margin: 0 auto; }}
                    h1 {{
                        text-align: center;
                        margin: var(--aq-space-5) 0 var(--aq-space-6);
                    }}
                    .score, .form, .thank-you {{
                        background: var(--aq-bg-layer);
                        border: 1px solid var(--aq-border-muted);
                        border-radius: var(--aq-radius-lg);
                        padding: var(--aq-space-6);
                        margin-bottom: var(--aq-space-5);
                    }}
                    .score {{ text-align: center; font-size: var(--aq-fs-large); }}
                    .score strong {{
                        color: var(--aq-text-muted);
                        font-weight: 500;
                        display: inline-block;
                        min-width: 120px;
                    }}
                    .savings-value {{ color: var(--aq-success); font-weight: 600; font-size: var(--aq-fs-heading); }}
                    .bill-value {{ color: var(--aq-danger); font-weight: 600; }}
                    .meta {{
                        text-align: center;
                        color: var(--aq-text-subtle);
                        font-size: var(--aq-fs-small);
                        margin-bottom: var(--aq-space-5);
                    }}
                    /* Aquarium Banner, warning tint: the high-score callout */
                    .high-score {{
                        background: var(--aq-warning-surface);
                        border: 1px solid var(--aq-warning-border);
                        border-radius: var(--aq-radius);
                        color: var(--aq-warning);
                        padding: var(--aq-space-5);
                        margin: 0 0 var(--aq-space-5);
                        text-align: center;
                        font-size: var(--aq-fs-large);
                        font-weight: 600;
                    }}
                    .form p {{
                        margin: 0 0 var(--aq-space-5);
                        font-size: var(--aq-fs-subheading);
                        text-align: center;
                    }}
                    .form input[type="email"], .form input[type="text"] {{
                        width: 100%;
                        min-height: 44px; /* comfortable touch target on phones */
                        margin: var(--aq-space-3) 0 var(--aq-space-2);
                        font-size: var(--aq-fs-large); /* 16px stops iOS zooming on focus */
                    }}
                    .form small {{
                        display: block;
                        color: var(--aq-text-subtle);
                        font-size: var(--aq-fs-small);
                        margin: 0 0 var(--aq-space-3);
                    }}
                    .form small.optional {{ color: var(--aq-text-inactive); }}
                    .consent-text {{
                        background: var(--aq-bg-raised);
                        border: 1px solid var(--aq-border-muted);
                        border-radius: var(--aq-radius);
                        padding: var(--aq-space-4) var(--aq-space-5);
                        margin: var(--aq-space-5) 0;
                        color: var(--aq-text-muted);
                    }}
                    .consent-label {{
                        display: flex;
                        align-items: flex-start;
                        gap: var(--aq-space-4);
                        cursor: pointer;
                    }}
                    .consent-label input[type="checkbox"] {{
                        width: 20px;
                        height: 20px;
                        min-width: 20px;
                        margin: 0;
                    }}
                    .consent-label a {{ word-break: break-all; }}
                    /* Aquarium primary button, full width on the form */
                    .form button {{ width: 100%; min-height: 44px; font-size: var(--aq-fs-large); }}
                    .loading {{ display: none; }}
                    /* Aquarium Alert: success / danger */
                    .success, .error {{
                        border: 1px solid;
                        border-radius: var(--aq-radius);
                        padding: var(--aq-space-4) var(--aq-space-5);
                        margin: 0 0 var(--aq-space-5);
                        text-align: center;
                        font-weight: 500;
                    }}
                    .success {{ background: var(--aq-success-surface); border-color: var(--aq-success-border); color: var(--aq-success); }}
                    .error {{ background: var(--aq-danger-surface); border-color: var(--aq-danger-border); color: var(--aq-danger); }}
                    .thank-you h2 {{ margin: 0 0 var(--aq-space-4); }}
                    .thank-you p {{ color: var(--aq-text-muted); margin: 0 0 var(--aq-space-5); }}
                    /* The CTA link, styled as an Aquarium primary button */
                    .thank-you a {{
                        display: inline-flex;
                        align-items: center;
                        min-height: 40px;
                        padding: var(--aq-space-3) var(--aq-space-5);
                        background: var(--aq-btn-primary-bg);
                        color: var(--aq-btn-primary-text);
                        border-radius: var(--aq-radius);
                        font-weight: 500;
                        text-decoration: none;
                    }}
                    .thank-you a:hover {{ background: var(--aq-btn-primary-hover); }}
                    /* Warning-toned text link: leaving without claiming costs the player the event */
                    .back-without-claim {{
                        display: block;
                        width: fit-content;
                        margin: var(--aq-space-5) auto 0;
                        padding: var(--aq-space-3) var(--aq-space-4);
                        color: var(--aq-warning);
                        font-size: var(--aq-fs-small);
                        text-decoration: underline;
                        text-underline-offset: 3px;
                    }}
                    .back-without-claim:hover {{ background: var(--aq-warning-surface); }}
                    .privacy-note {{
                        text-align: center;
                        color: var(--aq-text-inactive);
                        font-size: var(--aq-fs-small);
                        margin-top: var(--aq-space-5);
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>Claim your score</h1>
                    
                    <div class="score">
                        {score_block_form}
                    </div>
                    
                    <div class="meta">Game played on: {row['timestamp'].strftime('%B %d, %Y at %I:%M %p')}</div>
                    
                    <div id="highScoreCheck"></div>
                    
                    <div class="form" id="claimSection">
                        <p><strong>Join the leaderboard!</strong></p>
                        <form id="claimForm">
                            <input type="email" id="email" placeholder="your@email.com" required>
                            
                            <input type="text" id="nickname" placeholder="Your name" maxlength="50" required>
                            <small>Your name will appear on the leaderboard</small>
                            
                            <input type="text" id="company" placeholder="Company name (optional)" maxlength="100">
                            
                            <input type="text" id="title" placeholder="Job title (optional)" maxlength="100">
                            <small class="optional">Company and title fields are optional</small>
                            
                            <div class="consent-text">
                                <label class="consent-label">
                                    <input type="checkbox" id="privacyConsent" name="privacyConsent" required>
                                    <span>By submitting your information, you agree to Aiven's privacy policy: <a href="https://aiven.io/privacy" target="_blank" rel="noopener noreferrer">https://aiven.io/privacy</a></span>
                                </label>
                            </div>

                            <button type="submit" id="submitBtn">
                                <span class="loading">Claiming…</span>
                                <span class="normal">Claim score</span>
                            </button>
                        </form>

                        <a href="{back_url}" class="back-without-claim" id="backWithoutClaim">Back to game without claiming score</a>
                        
                        <div class="privacy-note">
                            Your email is used for score verification and product updates.
                        </div>
                    </div>
                    
                    <div id="message"></div>
                    {claim_cta}
                </div>

                <script>
                // Cache the player's form entry on this device so a repeat claim
                // (another game, same phone) comes prefilled. Consent is never cached.
                const CLAIM_CACHE_KEY = 'aiven-game-claim-form';
                const CACHED_FIELDS = ['email', 'nickname', 'company', 'title'];

                function loadClaimCache() {{
                    try {{
                        const cached = JSON.parse(localStorage.getItem(CLAIM_CACHE_KEY) || '{{}}');
                        CACHED_FIELDS.forEach((id) => {{
                            const input = document.getElementById(id);
                            if (input && typeof cached[id] === 'string' && !input.value) input.value = cached[id];
                        }});
                    }} catch (err) {{ /* storage unavailable or corrupt - ignore */ }}
                }}

                function saveClaimCache() {{
                    try {{
                        const entry = {{}};
                        CACHED_FIELDS.forEach((id) => {{
                            const input = document.getElementById(id);
                            if (input) entry[id] = input.value;
                        }});
                        localStorage.setItem(CLAIM_CACHE_KEY, JSON.stringify(entry));
                    }} catch (err) {{ /* storage unavailable - ignore */ }}
                }}

                // Leaving unclaimed forfeits the event, so ask first; Cancel keeps the form as is
                document.getElementById('backWithoutClaim').addEventListener('click', (e) => {{
                    if (!confirm("If you don't claim your score, you can't win the event. Go back to the game without claiming?")) {{
                        e.preventDefault();
                    }}
                }});

                loadClaimCache();
                CACHED_FIELDS.forEach((id) => {{
                    const input = document.getElementById(id);
                    if (input) input.addEventListener('input', saveClaimCache);
                }});

                // Check if this is a high score
                fetch('/api/check-high-score/{session_id}')
                    .then(response => response.json())
                    .then(data => {{
                        if (data.is_high_score) {{
                            document.getElementById('highScoreCheck').innerHTML = 
                                `<div class="high-score">High score! {highscore_line}<br>
                                Rank #${{data.rank}} out of ${{data.total_scores}} players!</div>`;
                        }}
                    }})
                    .catch(err => console.log('Could not check high score status'));

                document.getElementById('claimForm').addEventListener('submit', async (e) => {{
                    e.preventDefault();
                    if (!document.getElementById('privacyConsent').checked) {{
                        document.getElementById('message').innerHTML = '<div class="error">Please agree to the privacy policy to continue.</div>';
                        return;
                    }}
                    const email = document.getElementById('email').value;
                    const nickname = document.getElementById('nickname').value;
                    const company = document.getElementById('company').value || null;
                    const title = document.getElementById('title').value || null;
                    const messageDiv = document.getElementById('message');
                    const submitBtn = document.getElementById('submitBtn');
                    
                    // Show loading state
                    submitBtn.disabled = true;
                    submitBtn.querySelector('.loading').style.display = 'inline';
                    submitBtn.querySelector('.normal').style.display = 'none';
                    
                    try {{
                        // First, submit to your backend for leaderboard
                        const response = await fetch('/api/claim/{session_id}', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ 
                                email: email, 
                                nickname: nickname, 
                                company: company 
                            }})
                        }});
                        
                        const data = await response.json();
                        
                        if (response.ok) {{
                            const nameParts = nickname.trim().split(' ');
                            const firstName = nameParts[0] || nickname;
                            const lastName = nameParts.slice(1).join(' ') || nameParts[0];
                            
                            // Submit to Salesforce via hidden form (bypasses CORS)
                            const iframe = document.createElement('iframe');
                            iframe.name = 'salesforce-target';
                            iframe.style.display = 'none';
                            document.body.appendChild(iframe);
                            
                            const form = document.createElement('form');
                            form.target = 'salesforce-target';
                            form.method = 'POST';
                            form.action = 'https://go.aiven.io/l/890043/2025-09-25/wqyqc';
                            
                            const fields = {{
                                email: email,
                                first_name: firstName,
                                last_name: lastName,
                                company: company || 'Not Provided',
                                title: title || '',
                                source: 'Inkless Game - Disk Invaders'
                            }};
                            
                            Object.entries(fields).forEach(([key, value]) => {{
                                const input = document.createElement('input');
                                input.type = 'hidden';
                                input.name = key;
                                input.value = value;
                                form.appendChild(input);
                            }});
                            
                            document.body.appendChild(form);
                            form.submit();
                            
                            // Cleanup after 2 seconds
                            setTimeout(() => {{
                                if (document.body.contains(form)) document.body.removeChild(form);
                                if (document.body.contains(iframe)) document.body.removeChild(iframe);
                            }}, 2000);
                            
                            // Show success message
                            let message = '<div class="success">Score claimed</div>';
                            if (data.is_high_score) {{
                                message += `<div class="high-score">High score confirmed. You ranked #${{data.rank}}!</div>`;
                            }}
                            
                            messageDiv.innerHTML = message;
                            // Reveal the post-claim CTA (the event's own, or the Inkless default)
                            document.getElementById('claimCta').hidden = false;
                            document.getElementById('claimSection').style.display = 'none';
                        }} else {{
                            let errorMessage = 'An error occurred';
                            
                            if (data.detail) {{
                                if (typeof data.detail === 'string') {{
                                    errorMessage = data.detail;
                                }} else if (Array.isArray(data.detail)) {{
                                    const errors = data.detail.map(err => {{
                                        const field = err.loc ? err.loc.join('.') : 'unknown';
                                        return `${{field}}: ${{err.msg}}`;
                                    }});
                                    errorMessage = errors.join('<br>');
                                }} else if (typeof data.detail === 'object') {{
                                    errorMessage = JSON.stringify(data.detail);
                                }}
                            }}
                            
                            messageDiv.innerHTML = '<div class="error">' + errorMessage + '</div>';
                            submitBtn.disabled = false;
                            submitBtn.querySelector('.loading').style.display = 'none';
                            submitBtn.querySelector('.normal').style.display = 'inline';
                        }}
                    }} catch (err) {{
                        console.error('Error:', err);
                        messageDiv.innerHTML = '<div class="error">Could not claim your score. Please try again.</div>';
                        submitBtn.disabled = false;
                        submitBtn.querySelector('.loading').style.display = 'none';
                        submitBtn.querySelector('.normal').style.display = 'inline';
                    }}
                }});
            </script>
            </body>
            </html>
        """)
        
    except Exception as e:
        logger.error(f"Error displaying claim page: {e}")
        return HTMLResponse("Internal server error", status_code=500)

@app.post("/api/claim/{session_id}", response_model=ClaimResponse)
async def claim_score(session_id: str, claim_data: ClaimData):
    """Claim a score with email, nickname, and optional company"""
    try:
        # First check if the score exists
        score = await database.get_score(session_id)
        if not score:
            raise HTTPException(status_code=404, detail="Score not found")
        
        # Check if already claimed
        if score['email']:
            raise HTTPException(status_code=400, detail=f"Score has already been claimed")
        
        # Check if nickname is already taken by a different email
        existing_user = await database.get_user_by_nickname(claim_data.nickname, score['event'], score['game'])
        if existing_user:
            if existing_user['email'].lower() != claim_data.email.lower():
                raise HTTPException(status_code=400, detail=f"Nickname '{claim_data.nickname}' is already taken. Please choose another.")
            # Same email can reuse the nickname - this allows users to claim multiple scores with the same nickname
        
        # Claim the score
        success = await database.claim_score(session_id, claim_data.email, claim_data.nickname, claim_data.company)
        if not success:
            raise HTTPException(status_code=400, detail="Failed to claim score")
        
        # Check if it's a high score
        high_score_info = await database.check_high_score(session_id)
        
        company_log = f" from {claim_data.company}" if claim_data.company else ""
        logger.info(f"Score claimed for session {session_id} by '{claim_data.nickname}' ({claim_data.email}){company_log}")
        
        return ClaimResponse(
            success=True,
            message="Score claimed successfully!",
            is_high_score=high_score_info['is_high_score'] if high_score_info else False,
            rank=high_score_info['rank'] if high_score_info else None
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error claiming score: {e}")
        raise HTTPException(status_code=500, detail="Failed to claim score")

EVENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@app.get("/api/events")
async def list_events():
    """Events with a board of their own, for the leaderboard page's picker"""
    try:
        events = await database.list_events()
        return {"events": [
            {
                "slug": e["slug"],
                "name": e["name"],
                "starts_at": e["starts_at"].isoformat() if e["starts_at"] else None,
                "ends_at": e["ends_at"].isoformat() if e["ends_at"] else None,
                "games": e["games"],
            }
            for e in events
        ]}
    except Exception as e:
        logger.error(f"Error listing events: {e}")
        raise HTTPException(status_code=500, detail="Failed to list events")


def _leaderboard_entry(rank: int, row: dict, game: dict | None) -> dict:
    score_display, secondary_display = _displays(row, game)
    entry = {
        "rank": rank,
        "nickname": row.get("nickname") or "Anonymous",
        "score": row["score"],
        "score_display": score_display,
        "secondary": row.get("secondary"),
        "secondary_display": secondary_display,
        # Original Disk Invaders field names, so older pages keep working
        "total_savings": score_display,
        "final_bill": secondary_display if secondary_display is not None else "—",
        "timestamp": row["timestamp"].isoformat(),
        "source": row.get("source", "arcade"),
        "event": row.get("event"),
        "claimed": True,  # only claimed scores are ever listed
    }
    if row.get("company"):
        entry["company"] = row["company"]
    details = _json_or_none(row.get("details"))
    if details:
        entry["details"] = details
    return entry


async def _build_leaderboard(game_slug: str, limit: int, source: str, event: str) -> dict:
    if source not in ("all", "arcade", "web", "other"):
        raise HTTPException(status_code=400, detail="source must be all, arcade, web or other")
    if event not in ("all", "none") and not EVENT_SLUG_RE.match(event):
        raise HTTPException(status_code=400, detail="event must be all, none or an event slug")
    game = await database.get_game(game_slug)
    if not game:
        raise HTTPException(status_code=404, detail="Unknown game")

    rows = await database.get_leaderboard(
        source=None if source == "all" else source,
        event=None if event == "all" else event,
        game=game_slug,
    )
    # Only claimed scores (a nickname and an email) are shown; the query already ranks them
    claimed = [r for r in rows if r.get("nickname") and r.get("email")][:limit]
    return {
        "leaderboard": [_leaderboard_entry(i + 1, r, game) for i, r in enumerate(claimed)],
        "ranked_by": "score",
        "game": _game_public(game),
        "source": source,
        "event": event,
    }


@app.get("/api/leaderboard")
async def get_leaderboard(limit: int = 10, source: str = "all", event: str = "all", game: str = DEFAULT_GAME):
    """The leaderboard: claimed scores only, best first.

    `game` picks the game (default Disk Invaders). `source` filters by where it was played
    ("arcade", "web", "other" or "all"). `event` picks one event's board by slug, "none" for scores
    played at no event, or "all"."""
    try:
        return await _build_leaderboard(game, limit, source, event)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting leaderboard: {e}")
        raise HTTPException(status_code=500, detail="Failed to get leaderboard")


@app.get("/api/all-scores")
async def get_all_scores(game: str = DEFAULT_GAME):
    """Every score of a game (claimed or not), minimal data, for high score detection"""
    try:
        rows = await database.get_leaderboard(game=game)
        return [
            {"total_savings": r["score"], "final_bill": r["secondary"], "score": r["score"],
             "rank": i + 1, "claimed": bool(r.get("email"))}
            for i, r in enumerate(rows)
        ]
    except Exception as e:
        logger.error(f"Error getting all scores: {e}")
        raise HTTPException(status_code=500, detail="Failed to get all scores")


# ---------------------------------------------------------------------------
# Admin console authentication
#
# A single shared password (ADMIN_KEY) exchanged for an HMAC-signed, HttpOnly
# session cookie, so the secret stops travelling in URLs and referrers the way
# ?admin_key= does. The cookie is signed with ADMIN_KEY itself, which means
# rotating the password invalidates every live session.
#
# If ADMIN_KEY is unset the console fails closed rather than opening up.
# ---------------------------------------------------------------------------

ADMIN_SESSION_COOKIE = "inkless_admin"
ADMIN_SESSION_TTL_SECONDS = 12 * 60 * 60


def _admin_key() -> str:
    return os.getenv("ADMIN_KEY", "")


def _sign_admin_session(expires_at: int) -> str:
    mac = hmac.new(
        _admin_key().encode(), str(expires_at).encode(), hashlib.sha256
    ).hexdigest()
    return f"{expires_at}.{mac}"


def _admin_session_valid(token: str) -> bool:
    key = _admin_key()
    if not key or not token:
        return False

    expires_raw, _, mac = token.partition(".")
    if not mac:
        return False

    try:
        expires_at = int(expires_raw)
    except ValueError:
        return False

    if expires_at < time.time():
        return False

    expected = hmac.new(key.encode(), expires_raw.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, mac)


def require_admin_cookie(request: Request):
    """Admin console session only. Guards token management: a token must never be able to mint tokens."""
    if not _admin_session_valid(request.cookies.get(ADMIN_SESSION_COOKIE, "")):
        raise HTTPException(status_code=401, detail="Not authenticated")


TOKEN_PREFIX = "lbk_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def _token_from_request(request: Request):
    token = _bearer(request)
    if not token.startswith(TOKEN_PREFIX):
        return None
    return await database.authenticate_token(_hash_token(token))


async def require_admin(request: Request):
    """Dependency guarding every admin data route: the console's session cookie, or an admin API token"""
    if _admin_session_valid(request.cookies.get(ADMIN_SESSION_COOKIE, "")):
        return
    tok = await _token_from_request(request)
    if tok and tok["kind"] == "admin":
        return
    raise HTTPException(status_code=401, detail="Not authenticated")


@app.post("/admin/login")
async def admin_login(request: Request, response: Response, password: str = Form(...)):
    """Exchange the shared password for a session cookie"""
    key = _admin_key()

    if not key:
        logger.error("Admin login attempted but ADMIN_KEY is not configured")
        raise HTTPException(status_code=503, detail="Admin console is not configured")

    if not hmac.compare_digest(password, key):
        # Small fixed delay to take the edge off password guessing
        await asyncio.sleep(0.5)
        logger.warning("Failed admin login attempt")
        raise HTTPException(status_code=401, detail="Incorrect password")

    expires_at = int(time.time()) + ADMIN_SESSION_TTL_SECONDS
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        _sign_admin_session(expires_at),
        max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    logger.info("Admin logged in")
    return {"success": True}


@app.post("/admin/logout")
async def admin_logout(response: Response):
    """Clear the admin session cookie"""
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return {"success": True}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Serve the admin console.

    Always served; the page itself shows a password prompt until
    /admin/api/scores stops answering 401.
    """
    admin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")

    try:
        with open(admin_path, "r", encoding="utf-8") as handle:
            return HTMLResponse(content=handle.read())
    except FileNotFoundError:
        logger.error(f"Admin template missing at {admin_path}")
        raise HTTPException(status_code=500, detail="Admin console template missing")


@app.get("/admin/api/scores", dependencies=[Depends(require_admin)])
@app.get("/api/v1/admin/scores", dependencies=[Depends(require_admin)])
async def admin_list_scores(game: str | None = None, event: str | None = None,
                            claimed: bool | None = None, limit: int = 5000, offset: int = 0):
    """Scores of every game (or one with ?game=), ranked within their game. Filters: game, event
    ("none" = no event), claimed. Paged with limit / offset."""
    try:
        games = await _games_by_slug()
        rows = await database.get_all_scores_admin(game)

        # Rank within each game's own order (the query already sorts by it)
        position: dict = {}
        scores = []
        for row in rows:
            position[row["game"]] = position.get(row["game"], 0) + 1
            score_display, secondary_display = _displays(row, games.get(row["game"]))
            scores.append({
                "rank": position[row["game"]],
                "session_id": row["session_id"],
                "game": row["game"],
                "nickname": row["nickname"],
                "company": row["company"],
                "email": row["email"],
                "score": row["score"],
                "score_display": score_display,
                "secondary": row["secondary"],
                "secondary_display": secondary_display,
                "details": _json_or_none(row["details"]),
                "claimed": bool(row["email"]),
                "source": row["source"],
                "event": row["event"],
                "played_at": row["timestamp"].isoformat() if row["timestamp"] else None,
                "claimed_at": row["claimed_at"].isoformat() if row["claimed_at"] else None,
            })

        if event == "none":
            scores = [x for x in scores if not x["event"]]
        elif event:
            scores = [x for x in scores if x["event"] == event]
        if claimed is not None:
            scores = [x for x in scores if x["claimed"] == claimed]

        total = len(scores)
        limit = max(1, min(limit, 20000))
        return {"total": total, "scores": scores[offset:offset + limit]}

    except Exception as e:
        logger.error(f"Error listing scores for admin: {e}")
        raise HTTPException(status_code=500, detail="Failed to list scores")


# Event links and QR codes are built per (event, game): each game says where it is played
# (games.play_url / cabinet_url), and its event board lives on this service.
# PUBLIC_LEADERBOARD_URL pins the board links to a stable host (the Aiven app URL can change
# if the service is recreated); without it they use the host the console was opened on.
PUBLIC_LEADERBOARD_URL = os.getenv("PUBLIC_LEADERBOARD_URL", "").strip().rstrip("/")
QR_KINDS = ("game", "leaderboard", "cabinet")


def _origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


def _fill_event(template: str | None, event: str) -> str | None:
    """A game's URL template with the event in it: {event} is replaced, else ?event= is added"""
    if not template:
        return None
    if "{event}" in template:
        return template.replace("{event}", urllib.parse.quote(event, safe=""))
    base, _, fragment = template.partition("#")
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}event={urllib.parse.quote(event, safe='')}"
    return f"{url}#{fragment}" if fragment else url


def _board_links(event: str, game: dict, request: Request) -> dict:
    """The links (and so the QR codes) for one game at one event. A link is None when the
    game has no URL of that kind set in the Games tab."""
    board_base = PUBLIC_LEADERBOARD_URL or _origin(request)
    q = urllib.parse.urlencode({"game": game["slug"], "event": event})
    return {
        "game": _fill_event(game.get("play_url"), event),
        "leaderboard": f"{board_base}/leaderboard?{q}",
        "cabinet": _fill_event(game.get("cabinet_url"), event),
    }


def _event_boards(e: dict, games_by_slug: dict, request: Request) -> list:
    return [
        {"game": slug, "game_name": games_by_slug[slug]["name"],
         "links": _board_links(e["slug"], games_by_slug[slug], request)}
        for slug in e["games"] if slug in games_by_slug
    ]


def _event_status(e: dict) -> str:
    now = datetime.utcnow()
    if e["starts_at"] and now < e["starts_at"]:
        return "upcoming"
    if e["ends_at"] and now > e["ends_at"]:
        return "closed"
    return "open"


@app.get("/admin/api/events", dependencies=[Depends(require_admin)])
@app.get("/api/v1/admin/events", dependencies=[Depends(require_admin)])
async def admin_list_events(request: Request):
    """Every event with one board per game: its game, leaderboard and cabinet links"""
    events = await database.list_events()
    games_by_slug = await _games_by_slug()
    out = []
    for e in events:
        boards = _event_boards(e, games_by_slug, request)
        out.append({
            "slug": e["slug"], "name": e["name"],
            "starts_at": e["starts_at"].isoformat() if e["starts_at"] else None,
            "ends_at": e["ends_at"].isoformat() if e["ends_at"] else None,
            "status": _event_status(e),
            "games": e["games"],
            "cta_text": e.get("cta_text"), "cta_url": e.get("cta_url"),
            "boards": boards,
            # Before events had games: the first board's links, for older scripts
            "links": boards[0]["links"] if boards else None,
        })
    return {"events": out}


@app.get("/admin/api/events/{slug}/qr", dependencies=[Depends(require_admin)])
@app.get("/api/v1/admin/events/{slug}/qr", dependencies=[Depends(require_admin)])
async def admin_event_qr(slug: str, request: Request, kind: str = "game", format: str = "png",
                         game: str | None = None):
    """A QR code for one game's link at an event, ready for a slide or poster.

    game: one of the event's games (may be left out when the event has only one).
    kind: game (players' phones), leaderboard (that game's board for the event) or cabinet
    (the arcade URL). High error correction, so it still scans when projected or printed small."""
    if not EVENT_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="bad event slug")
    if kind not in QR_KINDS or format not in ("png", "svg"):
        raise HTTPException(status_code=400, detail="kind must be game, leaderboard or cabinet; format png or svg")
    event = next((e for e in await database.list_events() if e["slug"] == slug), None)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if game is None:
        if len(event["games"]) != 1:
            raise HTTPException(status_code=400, detail="This event has several games (or none): pass ?game=<slug>")
        game = event["games"][0]
    if game not in event["games"]:
        raise HTTPException(status_code=404, detail="That game is not part of this event")
    game_row = await database.get_game(game)
    if not game_row:
        raise HTTPException(status_code=404, detail="Unknown game")

    url = _board_links(slug, game_row, request)[kind]
    if not url:
        raise HTTPException(status_code=404, detail=f"{game_row['name']} has no {kind} URL; set it in the Games tab")
    buffer = io.BytesIO()
    qr = segno.make(url, error="h", micro=False)
    if format == "svg":
        qr.save(buffer, kind="svg", scale=10, border=4, xmldecl=False)
        media = "image/svg+xml"
    else:
        qr.save(buffer, kind="png", scale=16, border=4)
        media = "image/png"
    return Response(
        content=buffer.getvalue(),
        media_type=media,
        headers={"Content-Disposition": f'inline; filename="{slug}-{game}-{kind}.{format}"', "Cache-Control": "no-store"},
    )


@app.put("/admin/api/events/{slug}", dependencies=[Depends(require_admin)])
@app.put("/api/v1/admin/events/{slug}", dependencies=[Depends(require_admin)])
async def admin_upsert_event(slug: str, event: EventUpsert):
    """Create an event or edit its name, window and games. Times are UTC; leave them out for
    always-open. `games` replaces the event's game list; leave it out to keep the current one."""
    if not EVENT_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug must be lowercase letters, numbers and hyphens")

    def naive_utc(dt):
        if dt is None:
            return None
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt

    try:
        await database.upsert_event(slug, event.name, naive_utc(event.starts_at), naive_utc(event.ends_at), event.games,
                                    event.cta())
        logger.info(f"Admin saved event {slug} (games: {event.games if event.games is not None else 'unchanged'})")
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error saving event {slug}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save event")


@app.patch("/admin/api/scores/{session_id}", dependencies=[Depends(require_admin)])
@app.patch("/api/v1/admin/scores/{session_id}", dependencies=[Depends(require_admin)])
async def admin_update_score(session_id: str, update: AdminScoreUpdate):
    """Edit a score's nickname, company or email"""
    try:
        updated = await database.admin_update_score(
            session_id,
            update.nickname,
            update.company,
            str(update.email) if update.email else None,
        )

        if not updated:
            raise HTTPException(status_code=404, detail="Score not found")

        logger.info(f"Admin updated score {session_id}")
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating score {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update score")


@app.delete("/admin/api/scores/{session_id}", dependencies=[Depends(require_admin)])
@app.delete("/api/v1/admin/scores/{session_id}", dependencies=[Depends(require_admin)])
async def admin_delete_score(session_id: str):
    """Delete a score - for clearing out test runs and junk entries"""
    try:
        deleted = await database.admin_delete_score(session_id)

        if not deleted:
            raise HTTPException(status_code=404, detail="Score not found")

        logger.info(f"Admin deleted score {session_id}")
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting score {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete score")


@app.get("/admin/export.csv", dependencies=[Depends(require_admin)])
@app.get("/api/v1/admin/export.csv", dependencies=[Depends(require_admin)])
async def admin_export_csv(event: str = "all", game: str | None = None):
    """Download scores as CSV, for spreadsheets and follow-up. ?event=<slug> exports one event's
    entrants (for its prize draw); ?game=<slug> one game's."""
    try:
        rows = await database.get_all_scores_admin(game)
        if event == "none":
            rows = [r for r in rows if not r["event"]]
        elif event != "all":
            rows = [r for r in rows if r["event"] == event]
        games = await _games_by_slug()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["rank", "nickname", "company", "email", "score", "secondary", "source",
             "event", "game", "played_at", "claimed_at", "session_id"]
        )

        position: dict = {}
        exported = 0
        for row in rows:
            position[row["game"]] = position.get(row["game"], 0) + 1
            writer.writerow([
                position[row["game"]],
                row["nickname"] or "",
                row["company"] or "",
                row["email"] or "",
                row["score"],
                row["secondary"] if row["secondary"] is not None else "",
                row["source"],
                row["event"] or "",
                row["game"],
                row["timestamp"].isoformat() if row["timestamp"] else "",
                row["claimed_at"].isoformat() if row["claimed_at"] else "",
                row["session_id"],
            ])
            exported += 1

        filename = f"leaderboard-scores-{datetime.utcnow().strftime('%Y-%m-%d')}.csv"
        logger.info(f"Admin exported {exported} scores as CSV")

        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as e:
        logger.error(f"Error exporting scores: {e}")
        raise HTTPException(status_code=500, detail="Failed to export scores")


@app.get("/api/admin/emails")
async def get_emails(admin_key: str = None):
    """Get all collected emails - protected endpoint for business use"""
    # Simple admin protection - in production use proper authentication
    if admin_key != os.getenv("ADMIN_KEY", "your_secret_admin_key"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        async with database.pool.acquire() as conn:
            # Check if company column exists first
            try:
                company_exists = await conn.fetchval("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='scores' AND column_name='company';
                """)
                
                if company_exists:
                    emails = await conn.fetch("""
                        SELECT email, nickname, company, score AS total_savings, secondary AS final_bill, claimed_at, timestamp
                        FROM scores 
                        WHERE email IS NOT NULL 
                        ORDER BY claimed_at DESC
                    """)
                else:
                    emails = await conn.fetch("""
                        SELECT email, nickname, score AS total_savings, secondary AS final_bill, claimed_at, timestamp
                        FROM scores 
                        WHERE email IS NOT NULL 
                        ORDER BY claimed_at DESC
                    """)
            except Exception:
                # Fallback query without company column
                emails = await conn.fetch("""
                    SELECT email, nickname, score AS total_savings, secondary AS final_bill, claimed_at, timestamp
                    FROM scores 
                    WHERE email IS NOT NULL 
                    ORDER BY claimed_at DESC
                """)
        
        email_list = []
        for record in emails:
            entry = {
                "email": record['email'],
                "nickname": record['nickname'],
                "total_savings": record['total_savings'],
                "final_bill": record['final_bill'],
                "claimed_at": record['claimed_at'].isoformat(),
                "game_played": record['timestamp'].isoformat()
            }
            
            # Add company if it exists in the record
            if 'company' in record._mapping and record['company']:
                entry["company"] = record['company']
            
            email_list.append(entry)
        
        return {
            "total_emails": len(email_list),
            "emails": email_list
        }
        
    except Exception as e:
        logger.error(f"Error getting emails: {e}")
        raise HTTPException(status_code=500, detail="Failed to get emails")

@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page():
    """Serve the leaderboard page"""
    try:
        # Use absolute path relative to the current file location
        current_dir = os.path.dirname(os.path.abspath(__file__))
        leaderboard_path = os.path.join(current_dir, "leaderboard.html")
        
        logger.info(f"Looking for leaderboard.html at: {leaderboard_path}")
        
        with open(leaderboard_path, "r", encoding="utf-8") as f:
            content = f.read()
            logger.info("Successfully loaded leaderboard.html")
            return HTMLResponse(content)
            
    except FileNotFoundError:
        logger.error(f"leaderboard.html not found at {leaderboard_path}")
        # List files in current directory for debugging
        try:
            files = os.listdir(os.path.dirname(os.path.abspath(__file__)))
            logger.error(f"Files in directory: {files}")
        except Exception as e:
            logger.error(f"Could not list directory: {e}")
        raise HTTPException(status_code=404, detail="Leaderboard page not found")
    except Exception as e:
        logger.error(f"Error serving leaderboard page: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/debug/info")
async def debug_info():
    """Debug endpoint to check deployment status"""
    from .models import ClaimData
    
    # Fix for Pydantic v2 compatibility
    try:
        # Try Pydantic v2 syntax first
        claim_fields = list(ClaimData.model_fields.keys())
    except AttributeError:
        # Fallback to Pydantic v1 syntax
        try:
            claim_fields = list(ClaimData.__fields__.keys())
        except AttributeError:
            claim_fields = ["error: could not get fields"]
    
    # Check database schema safely
    company_column = None
    try:
        if hasattr(database, 'pool') and database.pool:
            async with database.pool.acquire() as conn:
                company_column = await conn.fetchval("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='scores' AND column_name='company';
                """)
        else:
            company_column = "database pool not available"
    except Exception as e:
        company_column = f"error: {str(e)}"
    
    # Check file system
    current_dir = os.path.dirname(os.path.abspath(__file__))
    leaderboard_exists = os.path.exists(os.path.join(current_dir, "leaderboard.html"))
    
    try:
        files_in_dir = os.listdir(current_dir)
    except Exception as e:
        files_in_dir = [f"error listing files: {str(e)}"]
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "current_working_directory": os.getcwd(),
        "file_location": __file__,
        "claim_data_fields": claim_fields,
        "has_company_field": "company" in claim_fields,
        "company_column_exists": bool(company_column),
        "company_column_value": company_column,
        "files_in_directory": files_in_dir,
        "leaderboard_file_exists": leaderboard_exists,
        "leaderboard_path": os.path.join(current_dir, "leaderboard.html"),
        "database_pool_status": "available" if hasattr(database, 'pool') and database.pool else "not available"
    }

# Test endpoint for claim form
@app.get("/test-claim/{session_id}")
async def test_claim_form(session_id: str):
    """Test endpoint to see current claim form"""
    return await claim_page(session_id, None)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

# ===========================================================================
# Leaderboard API v1
#
#   Public            GET  /api/v1/games, /api/v1/games/{game}/leaderboard
#   Game token        POST /api/v1/scores            (a game's server posts a finished game)
#   Admin token or    GET/PUT /api/v1/admin/games, .../events; GET /api/v1/admin/scores,
#   console cookie   export.csv; PATCH/DELETE /api/v1/admin/scores/{id}
#   Console only     /admin/api/tokens               (create / list / revoke tokens)
#
# See /guide for how to add a leaderboard to a game, and /docs for the full schema.
# ===========================================================================


@app.get("/api/v1/games", tags=["public"])
async def v1_list_games():
    """Every game with a leaderboard, and how its scores rank and display"""
    return {"games": [_game_public(g) for g in await database.list_games()]}


@app.get("/api/v1/games/{game}/leaderboard", tags=["public"])
async def v1_leaderboard(game: str, limit: int = 10, source: str = "all", event: str = "all"):
    """One game's leaderboard (claimed scores, best first), optionally one event's board"""
    return await _build_leaderboard(game, max(1, min(limit, 500)), source, event)


@app.post("/api/v1/scores", tags=["game server"])
async def v1_submit_score(score: ScoreV1, request: Request):
    """Record a finished game. Call this from the game's SERVER with a game token (Authorization: Bearer lbk_...),
    never from a browser: whoever can call it can set any score. Returns the player's claim and leaderboard links."""
    tok = await _token_from_request(request)
    if not tok:
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    if tok["kind"] != "game" or tok["game"] != score.game:
        raise HTTPException(status_code=403, detail="This token cannot submit scores for that game")

    if not await database.get_game(score.game):
        raise HTTPException(status_code=404, detail="Unknown game")

    try:
        await database.insert_score(
            score.game, score.session_id, score.score, score.secondary,
            score.details, score.source, score.event,
        )
    except asyncpg.UniqueViolationError:
        # A retry of the same post is safe to repeat; report it rather than store a second score
        raise HTTPException(status_code=409, detail="session_id already recorded")

    origin = _origin(request)
    event_q = f"&event={score.event}" if score.event else ""
    logger.info(f"v1 score: game={score.game} session={score.session_id} score={score.score} token='{tok['name']}'")
    return {
        "success": True,
        "session_id": score.session_id,
        "claim_url": f"{origin}/claim/{score.session_id}",
        "leaderboard_url": f"{origin}/leaderboard?game={score.game}{event_q}",
    }


@app.get("/api/v1/admin/games", dependencies=[Depends(require_admin)], tags=["admin"])
@app.get("/admin/api/games", dependencies=[Depends(require_admin)], include_in_schema=False)
async def admin_list_games():
    games = await database.list_games()
    return {"games": [{**_game_public(g), "score_count": g["score_count"]} for g in games]}


@app.put("/api/v1/admin/games/{slug}", dependencies=[Depends(require_admin)], tags=["admin"])
@app.put("/admin/api/games/{slug}", dependencies=[Depends(require_admin)], include_in_schema=False)
async def admin_upsert_game(slug: str, game: GameUpsert):
    """Create a game or edit how it ranks and displays. Changing sort order re-ranks existing scores."""
    if not EVENT_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug must be lowercase letters, numbers and hyphens")
    if game.secondary_label and not game.secondary_format:
        game.secondary_format = "int"
    if not game.secondary_label:
        game.secondary_format = None
    await database.upsert_game(
        slug, game.name, game.score_label, game.score_format, game.sort_order,
        game.secondary_label, game.secondary_format, game.highscore_text,
        # Only touch the URLs when the caller sent them, so older scripts can't wipe them
        play_url=game.play_url if "play_url" in game.model_fields_set else ...,
        cabinet_url=game.cabinet_url if "cabinet_url" in game.model_fields_set else ...,
    )
    logger.info(f"Admin saved game {slug}")
    return {"success": True}


@app.get("/admin/api/tokens", dependencies=[Depends(require_admin_cookie)], include_in_schema=False)
async def admin_list_tokens():
    tokens = await database.list_tokens()
    return {"tokens": [
        {"id": t["id"], "name": t["name"], "kind": t["kind"], "game": t["game"],
         "prefix": t["token_prefix"],
         "created_at": t["created_at"].isoformat() if t["created_at"] else None,
         "last_used_at": t["last_used_at"].isoformat() if t["last_used_at"] else None,
         "revoked": t["revoked_at"] is not None}
        for t in tokens
    ]}


@app.post("/admin/api/tokens", dependencies=[Depends(require_admin_cookie)], include_in_schema=False)
async def admin_create_token(body: TokenCreate):
    """Mint a token. The secret is returned ONCE here and only its hash is kept."""
    if body.kind == "game" and not await database.get_game(body.game):
        raise HTTPException(status_code=404, detail="Unknown game: create it first")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = await database.create_token(
        body.name, body.kind, body.game, token[:12], _hash_token(token))
    logger.info(f"Admin created {body.kind} token '{body.name}' (id {token_id})")
    return {"id": token_id, "name": body.name, "kind": body.kind, "game": body.game, "token": token,
            "notice": "Copy this now: it cannot be shown again."}


@app.delete("/admin/api/tokens/{token_id}", dependencies=[Depends(require_admin_cookie)], include_in_schema=False)
async def admin_revoke_token(token_id: int):
    if not await database.revoke_token(token_id):
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    logger.info(f"Admin revoked token {token_id}")
    return {"success": True}


@app.get("/guide", response_class=HTMLResponse, include_in_schema=False)
async def guide_page():
    """How to add a leaderboard to a game, rendered from guide.md"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail="Guide not found")
    try:
        import markdown
        body = markdown.markdown(text, extensions=["fenced_code", "tables"])
    except Exception:
        body = f"<pre>{html.escape(text)}</pre>"
    return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Add a leaderboard to your game</title>
{AQUARIUM_HEAD}
<style>
body{{font-size:var(--aq-fs-large);line-height:1.5;padding:var(--aq-space-6) var(--aq-space-5)}}
main{{max-width:820px;margin:0 auto}}
h1{{border-bottom:1px solid var(--aq-border-muted);padding-bottom:var(--aq-space-4)}}
h2{{margin-top:var(--aq-space-7)}}
code{{background:var(--aq-bg-raised);padding:var(--aq-space-1) var(--aq-space-2);border-radius:var(--aq-radius);font-size:var(--aq-fs-default)}}
pre{{background:var(--aq-bg-layer);border:1px solid var(--aq-border-muted);border-radius:var(--aq-radius-lg);padding:var(--aq-space-5);overflow-x:auto}}
pre code{{background:none;padding:0}}
th,td{{border:1px solid var(--aq-border-muted);padding:var(--aq-space-3) var(--aq-space-4);text-align:left}}
td{{border-bottom:1px solid var(--aq-border-muted)}}
</style></head><body><main>{body}</main></body></html>""")
