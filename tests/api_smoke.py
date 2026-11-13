"""End-to-end checks for the leaderboard API, run against a live instance (stdlib only).

    ADMIN_KEY=... INGEST_SECRET=... python3 tests/api_smoke.py http://localhost:8000

Creates a throwaway game, so run it against a local or staging instance, not production.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "adm")
INGEST = os.environ.get("INGEST_SECRET", "s3")

passed = failed = 0
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


def call(method, path, body=None, headers=None, form=None, jar=True):
    data = None
    hdrs = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        resp = (opener if jar else urllib.request.build_opener()).open(req, timeout=20)
        raw = resp.read()
        status, ctype = resp.status, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, status, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    parsed = None
    if "json" in ctype:
        try:
            parsed = json.loads(raw)
        except ValueError:
            pass
    return status, parsed, raw


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


print("== Disk Invaders keeps working (legacy ingest, claim, board)")
st, _, _ = call("POST", "/api/scores", {"session_id": "smoke-di-1", "final_bill": 123, "total_savings": 8000,
                "timestamp": "2026-01-01T00:00:00Z", "source": "web"}, {"X-Ingest-Secret": INGEST}, jar=False)
check("legacy POST /api/scores with the ingest secret", st == 200, st)
st, _, _ = call("POST", "/api/scores", {"session_id": "smoke-di-x", "final_bill": 1, "total_savings": 1,
                "timestamp": "2026-01-01T00:00:00Z"}, jar=False)
check("legacy ingest still refuses a missing secret", st == 403, st)
st, body, _ = call("POST", "/api/claim/smoke-di-1", {"email": "di@example.com", "nickname": "SmokeDI"}, jar=False)
check("claim a Disk Invaders score", st == 200 and body["success"], (st, body))
st, body, _ = call("GET", "/api/leaderboard?limit=50", jar=False)
row = next((e for e in body["leaderboard"] if e["nickname"] == "SmokeDI"), None) if st == 200 else None
check("legacy leaderboard lists it with original field names",
      row and row["total_savings"] == "$8.0k" and row["final_bill"] == "$123", row)
check("game config is returned with the board", body["game"]["slug"] == "disk-invaders" and body["game"]["score_label"] == "Saved")

print("== games and tokens (console)")
st, _, _ = call("POST", "/admin/login", form={"password": ADMIN_KEY})
check("console login", st == 200, st)
st, _, _ = call("PUT", "/admin/api/games/smoke-speedrun", {
    "name": "Smoke Speedrun", "score_label": "Time", "score_format": "time_ms", "sort_order": "asc",
    "secondary_label": "Level", "secondary_format": "int", "highscore_text": "Fast! {score}"})
check("create a lower-wins time game", st == 200, st)
st, body, _ = call("GET", "/api/v1/games", jar=False)
slugs = [g["slug"] for g in body["games"]]
check("public games list shows both games", "disk-invaders" in slugs and "smoke-speedrun" in slugs, slugs)

st, body, _ = call("POST", "/admin/api/tokens", {"name": "smoke game token", "kind": "game", "game": "smoke-speedrun"})
game_token = body["token"] if st == 200 else ""
check("create a game token (secret returned once)", st == 200 and game_token.startswith("lbk_"), (st, body))
st, body, _ = call("POST", "/admin/api/tokens", {"name": "smoke admin token", "kind": "admin"})
admin_token = body["token"] if st == 200 else ""
check("create an admin token", st == 200 and admin_token.startswith("lbk_"))
st, _, _ = call("POST", "/admin/api/tokens", {"name": "nope", "kind": "game", "game": "no-such-game"})
check("a game token needs an existing game", st == 404, st)
st, body, _ = call("GET", "/admin/api/tokens")
listed = body["tokens"] if st == 200 else []
check("token list never contains a secret", all("token" not in t and "token_hash" not in t for t in listed) and len(listed) >= 2)
check("token list shows a prefix", any(t["prefix"] == game_token[:12] for t in listed))

print("== submitting scores (game token)")
def post(token, payload):
    return call("POST", "/api/v1/scores", payload, bearer(token) if token else None, jar=False)
good = {"game": "smoke-speedrun", "session_id": "smoke-sr-0001", "score": 92345, "secondary": 7,
        "details": {"level": 7}}
st, body, _ = post(game_token, good)
check("valid post returns claim and leaderboard links", st == 200 and "/claim/smoke-sr-0001" in body["claim_url"]
      and "game=smoke-speedrun" in body["leaderboard_url"], (st, body))
st, _, _ = post(game_token, good)
check("same session_id again is a 409, nothing stored twice", st == 409, st)
st, _, _ = post(None, {**good, "session_id": "smoke-sr-0002"})
check("no token is a 401", st == 401, st)
st, _, _ = post("lbk_notarealtoken", {**good, "session_id": "smoke-sr-0003"})
check("unknown token is a 401", st == 401, st)
st, _, _ = post(admin_token, {**good, "session_id": "smoke-sr-0004"})
check("an admin token cannot submit scores", st == 403, st)
st, _, _ = post(game_token, {**good, "game": "disk-invaders", "session_id": "smoke-sr-0005"})
check("a game token cannot write another game's board", st == 403, st)
st, _, _ = post(game_token, {**good, "session_id": "bad id!"})
check("invalid session_id is a 422", st == 422, st)
st, _, _ = post(game_token, {**good, "session_id": "smoke-sr-0006", "score": -5})
check("negative score is a 422", st == 422, st)
st, _, _ = post(game_token, {**good, "session_id": "smoke-sr-0007", "details": {"x": "y" * 3000}})
check("oversized details is a 422", st == 422, st)
for sid, ms, lvl in (("smoke-sr-0010", 61000, 9), ("smoke-sr-0011", 120500, 3)):
    post(game_token, {"game": "smoke-speedrun", "session_id": sid, "score": ms, "secondary": lvl})

print("== claiming and ranking a lower-wins game")
for sid, nick in (("smoke-sr-0001", "Mid"), ("smoke-sr-0010", "Fast"), ("smoke-sr-0011", "Slow")):
    call("POST", f"/api/claim/{sid}", {"email": f"{nick.lower()}@example.com", "nickname": nick}, jar=False)
st, body, _ = call("GET", "/api/v1/games/smoke-speedrun/leaderboard", jar=False)
order = [e["nickname"] for e in body["leaderboard"]]
check("lowest time ranks first", order == ["Fast", "Mid", "Slow"], order)
check("times are formatted by the game", body["leaderboard"][0]["score_display"] == "1:01.000", body["leaderboard"][0])
check("second stat and labels come through", body["leaderboard"][0]["secondary_display"] == "9"
      and body["game"]["secondary_label"] == "Level")
check("details are returned", body["leaderboard"][1].get("details") == {"level": 7})
st, body, _ = call("GET", "/api/check-high-score/smoke-sr-0010", jar=False)
check("rank respects sort order and reports the game", body["rank"] == 1 and body["game"] == "smoke-speedrun", body)
st, _, raw = call("GET", "/claim/smoke-sr-0010", jar=False)
check("claim page uses the game's labels", b"Time:" in raw and b"Level:" in raw and b"Total Saved" not in raw)
check("claim page has a warned way back to the game",
      b'id="backWithoutClaim"' in raw and b"Back to game without claiming score" in raw and b"can't win the event" in raw)
st, body, _ = call("GET", "/api/leaderboard?limit=50", jar=False)
check("the Disk Invaders board is unaffected by the new game",
      all(e["nickname"] not in ("Fast", "Mid", "Slow") for e in body["leaderboard"]))

print("== management API (admin token)")
st, body, _ = call("GET", "/api/v1/admin/scores?game=smoke-speedrun", headers=bearer(admin_token), jar=False)
check("admin token lists one game's scores", st == 200 and body["total"] >= 3 and all(s["game"] == "smoke-speedrun" for s in body["scores"]), st)
st, body, _ = call("GET", "/api/v1/admin/scores?game=smoke-speedrun&limit=1&offset=1", headers=bearer(admin_token), jar=False)
check("paging works", st == 200 and len(body["scores"]) == 1)
st, _, raw = call("GET", "/api/v1/admin/export.csv?game=smoke-speedrun", headers=bearer(admin_token), jar=False)
check("CSV export is filtered by game", st == 200 and b"smoke-speedrun" in raw and b"SmokeDI" not in raw and b"di@example.com" not in raw)
check("CSV export has no claimed column", b"claimed," not in raw.split(b"\n")[0].replace(b"claimed_at", b""))
st, _, _ = call("PUT", "/api/v1/admin/games/smoke-speedrun", {"name": "Smoke Speedrun 2", "score_label": "Time",
                "score_format": "time_ms", "sort_order": "asc"}, bearer(admin_token), jar=False)
check("admin token can edit a game", st == 200, st)
st, _, _ = call("GET", "/api/v1/admin/scores", headers=bearer(game_token), jar=False)
check("a game token cannot use the management API", st == 401, st)
st, _, _ = call("GET", "/api/v1/admin/scores", jar=False)
check("management API refuses anonymous callers", st == 401, st)
st, _, _ = call("POST", "/admin/api/tokens", {"name": "escalate", "kind": "admin"}, bearer(admin_token), jar=False)
check("an admin token cannot mint tokens", st == 401, st)
st, _, _ = call("DELETE", "/api/v1/admin/scores/smoke-sr-0011", headers=bearer(admin_token), jar=False)
check("admin token can delete a score", st == 200, st)

print("== events with several games")
st, _, _ = call("PUT", "/admin/api/games/smoke-speedrun", {
    "name": "Smoke Speedrun", "score_label": "Time", "score_format": "time_ms", "sort_order": "asc",
    "secondary_label": "Level", "secondary_format": "int", "highscore_text": "Fast! {score}",
    "play_url": "https://speedrun.example/play?mode=arcade", "cabinet_url": "https://speedrun.example/cab/{event}"})
check("a game can have play and cabinet URLs", st == 200, st)
st, _, _ = call("PUT", "/admin/api/games/smoke-speedrun", {"name": "Smoke Speedrun", "score_label": "Time",
    "score_format": "time_ms", "sort_order": "asc", "secondary_label": "Level", "secondary_format": "int",
    "highscore_text": "Fast! {score}"})
st, body, _ = call("GET", "/api/v1/games", jar=False)
sr = next(g for g in body["games"] if g["slug"] == "smoke-speedrun")
check("saving a game without URLs keeps them", sr["play_url"] == "https://speedrun.example/play?mode=arcade"
      and sr["cabinet_url"] == "https://speedrun.example/cab/{event}", sr)
st, _, _ = call("PUT", "/admin/api/games/smoke-speedrun", {**{k: sr[k] for k in ("name", "score_label",
    "score_format", "sort_order", "secondary_label", "secondary_format", "highscore_text")}, "play_url": "javascript:alert(1)"})
check("a non-http play URL is a 422", st == 422, st)
di = next(g for g in body["games"] if g["slug"] == "disk-invaders")
check("Disk Invaders gets its inkless.dev play URL", (di["play_url"] or "").endswith("/e/{event}"), di)

st, body, _ = call("PUT", "/admin/api/events/smoke-conf", {"name": "Smoke Conf", "games": ["disk-invaders", "smoke-speedrun"]})
check("create an event with two games", st == 200, (st, body))
st, body, _ = call("PUT", "/admin/api/events/smoke-bad", {"name": "Bad", "games": ["no-such-game"]})
check("an event with an unknown game is a 404", st == 404 and "no-such-game" in body.get("detail", ""), (st, body))
st, body, _ = call("GET", "/admin/api/events")
ev = next((e for e in body["events"] if e["slug"] == "smoke-conf"), None)
boards = {b["game"]: b["links"] for b in (ev or {}).get("boards", [])}
check("the event lists both games, one board each", ev and ev["games"] == ["disk-invaders", "smoke-speedrun"]
      and set(boards) == {"disk-invaders", "smoke-speedrun"}, ev)
check("play link appends ?event= when there is no {event}",
      boards.get("smoke-speedrun", {}).get("game") == "https://speedrun.example/play?mode=arcade&event=smoke-conf", boards)
check("cabinet link fills {event}", boards.get("smoke-speedrun", {}).get("cabinet") == "https://speedrun.example/cab/smoke-conf")
check("Disk Invaders play link fills {event}", (boards.get("disk-invaders", {}).get("game") or "").endswith("/e/smoke-conf"))
check("each board links to its own game's event board",
      "game=smoke-speedrun&event=smoke-conf" in boards.get("smoke-speedrun", {}).get("leaderboard", ""))
check("a game with no cabinet URL has no cabinet link", boards.get("disk-invaders", {}).get("cabinet") is None)
check("starter events were given Disk Invaders",
      all(e["games"] == ["disk-invaders"] for e in body["events"] if e["slug"] in ("ato-2026", "pgconf-2026")))

st, _, raw = call("GET", "/admin/api/events/smoke-conf/qr?game=smoke-speedrun&kind=game&format=png")
check("QR code for one game's play link", st == 200 and raw[:4] == b"\x89PNG", st)
st, _, raw = call("GET", "/admin/api/events/smoke-conf/qr?game=smoke-speedrun&kind=cabinet&format=svg")
check("QR code for a cabinet link, as SVG", st == 200 and b"<svg" in raw, st)
st, _, _ = call("GET", "/admin/api/events/smoke-conf/qr?kind=game")
check("a multi-game event needs ?game= for a QR code", st == 400, st)
st, _, _ = call("GET", "/admin/api/events/smoke-conf/qr?game=disk-invaders&kind=cabinet")
check("no QR code for a link the game doesn't have", st == 404, st)
st, _, _ = call("GET", "/admin/api/events/ato-2026/qr?kind=leaderboard")
check("a one-game event's QR code needs no ?game=", st == 200, st)
st, _, _ = call("GET", "/admin/api/events/smoke-conf/qr?game=disk-invaders&kind=game", jar=False)
check("QR codes need the admin console", st in (401, 403), st)

st, _, _ = call("PUT", "/admin/api/events/smoke-solo", {"name": "Smoke Solo", "games": ["disk-invaders"]})
post(game_token, {"game": "smoke-speedrun", "session_id": "smoke-ev-0001", "score": 50000, "event": "smoke-conf"})
post(game_token, {"game": "smoke-speedrun", "session_id": "smoke-ev-0002", "score": 40000, "event": "smoke-solo"})
for sid, nick in (("smoke-ev-0001", "AtConf"), ("smoke-ev-0002", "WrongEvent")):
    call("POST", f"/api/claim/{sid}", {"email": f"{nick.lower()}@example.com", "nickname": nick}, jar=False)
st, body, _ = call("GET", "/api/v1/games/smoke-speedrun/leaderboard?event=smoke-conf", jar=False)
check("a score from one of the event's games goes on its board",
      [e["nickname"] for e in body["leaderboard"]] == ["AtConf"], body.get("leaderboard"))
st, body, _ = call("GET", "/api/v1/games/smoke-speedrun/leaderboard?event=smoke-solo", jar=False)
check("a game not in the event is saved without the event", body["leaderboard"] == [], body.get("leaderboard"))
st, body, _ = call("GET", "/api/v1/games/smoke-speedrun/leaderboard?event=none", jar=False)
check("... and lands on the no-event board", "WrongEvent" in [e["nickname"] for e in body["leaderboard"]])

st, _, _ = call("PUT", "/admin/api/events/smoke-conf", {"name": "Smoke Conf (renamed)"})
st, body, _ = call("GET", "/api/events", jar=False)
ev = next(e for e in body["events"] if e["slug"] == "smoke-conf")
check("editing an event without games keeps them", ev["name"] == "Smoke Conf (renamed)"
      and ev["games"] == ["disk-invaders", "smoke-speedrun"], ev)
st, _, _ = call("PUT", "/admin/api/events/smoke-conf", {"name": "Smoke Conf", "games": ["smoke-speedrun"]})
st, body, _ = call("GET", "/api/events", jar=False)
check("the public events list shows each event's games, and games can be removed",
      next(e for e in body["events"] if e["slug"] == "smoke-conf")["games"] == ["smoke-speedrun"])

print("== revoking")
tid = next(t["id"] for t in listed if t["prefix"] == game_token[:12])
st, _, _ = call("DELETE", f"/admin/api/tokens/{tid}")
check("revoke a token", st == 200, st)
st, _, _ = post(game_token, {**good, "session_id": "smoke-sr-0099"})
check("a revoked token stops working at once", st == 401, st)
st, _, _ = call("DELETE", f"/admin/api/tokens/{tid}")
check("revoking twice is a 404", st == 404, st)

print("== docs")
st, _, raw = call("GET", "/guide", jar=False)
check("/guide renders", st == 200 and b"Add a leaderboard to your game" in raw and b"<pre>" in raw)
st, _, _ = call("GET", "/docs", jar=False)
check("/docs (OpenAPI) is up", st == 200, st)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
