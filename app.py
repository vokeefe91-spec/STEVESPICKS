"""Steve's Picks: one-file server.

Serves the app page and a small JSON API:
  /api/scoreboard?week=N&seasontype=2   games for a week (ESPN, free, no key)
  /api/game?id=EVENT                     details for one game (ESPN summary)
  /api/standings                         league standings (ESPN)
  /api/odds[?refresh=1]                  best lines across sportsbooks (The Odds API, needs ODDS_API_KEY)
  /api/picks  GET / POST                 paper picks, graded automatically when games finish
  /api/picks/delete?id=ID  POST          remove a pick
Settings (environment variables):
  PORT            web port (Railway sets this)
  ODDS_API_KEY    your odds key (The Odds API)
  DATA_DIR        folder for saved picks (use a Railway volume, e.g. /data)
  ODDS_TTL        seconds to reuse odds before asking again (default 3600 = 1 hour, saves credits)
  DEMO=1          sample games and odds, no internet needed (for testing)
"""
import json, os, time, threading, urllib.request, urllib.parse, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", "8093"))
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
_keyfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odds_key.txt")
if not ODDS_API_KEY and os.path.exists(_keyfile) and "paste-your" not in open(_keyfile).read():  # for testing on your own computer; never upload this file
    ODDS_API_KEY = open(_keyfile).read().strip()
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
ODDS_TTL = int(os.environ.get("ODDS_TTL", "3600"))
ODDS_MIN_REFRESH = 600  # a manual refresh is allowed at most every 10 minutes
DEMO = os.environ.get("DEMO", "") == "1"
HERE = os.path.dirname(os.path.abspath(__file__))
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"

os.makedirs(DATA_DIR, exist_ok=True)
PICKS_FILE = os.path.join(DATA_DIR, "picks.json")
LOCK = threading.Lock()
CACHE = {}


def fetch_json(url, ttl):
    now = time.time()
    hit = CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (StevesPicks)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
        headers = dict(r.headers)
    CACHE[url] = (now, data, headers)
    return data


# ---------- ESPN: games ----------
def team_info(c):
    t = c.get("team", {})
    rec = ""
    for r in c.get("records", []) or []:
        if r.get("type") in ("total", None) or r.get("name") == "overall":
            rec = r.get("summary", "")
            break
    if not rec and c.get("records"):
        rec = c["records"][0].get("summary", "")
    return {
        "id": t.get("id"), "name": t.get("displayName", ""), "short": t.get("shortDisplayName", t.get("name", "")),
        "abbr": t.get("abbreviation", ""), "logo": t.get("logo", ""), "color": t.get("color", ""),
        "score": c.get("score", ""), "record": rec, "winner": bool(c.get("winner")),
    }


def simplify_event(e):
    comp = (e.get("competitions") or [{}])[0]
    cs = comp.get("competitors", [])
    home = next((c for c in cs if c.get("homeAway") == "home"), cs[0] if cs else {})
    away = next((c for c in cs if c.get("homeAway") == "away"), cs[1] if len(cs) > 1 else {})
    st = (e.get("status") or comp.get("status") or {}).get("type", {})
    sit = comp.get("situation") or {}
    tv = ", ".join(n for b in comp.get("broadcasts", []) or [] for n in (b.get("names") or []))
    return {
        "id": e.get("id"), "date": e.get("date"), "name": e.get("shortName", ""),
        "state": st.get("state", "pre"), "detail": st.get("shortDetail", ""), "completed": bool(st.get("completed")),
        "home": team_info(home), "away": team_info(away), "tv": tv,
        "venue": (comp.get("venue") or {}).get("fullName", ""),
        "situation": {
            "down": sit.get("downDistanceText", ""), "possession": sit.get("possession", ""),
            "redzone": bool(sit.get("isRedZone")), "last": (sit.get("lastPlay") or {}).get("text", ""),
        } if sit else None,
    }


def scoreboard(week=None, seasontype=None):
    if DEMO:
        return demo_scoreboard()
    q = {}
    if week:
        q["week"] = week
    if seasontype:
        q["seasontype"] = seasontype
    url = ESPN + "/scoreboard" + ("?" + urllib.parse.urlencode(q) if q else "")
    data = fetch_json(url, 20)
    games = [simplify_event(e) for e in data.get("events", [])]
    games.sort(key=lambda g: g["date"] or "")
    wk = data.get("week", {}) or {}
    season = data.get("season", {}) or {}
    return {"week": wk.get("number"), "seasontype": season.get("type"), "year": season.get("year"), "games": games}


def game_detail(eid):
    if DEMO:
        return demo_detail(eid)
    d = fetch_json(ESPN + "/summary?" + urllib.parse.urlencode({"event": eid}), 20)
    out = {"id": eid, "teams": [], "leaders": [], "winprob": None, "predictor": None, "status": None, "scores": {}}
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    st = (comp.get("status") or {}).get("type", {})
    out["status"] = {"state": st.get("state"), "completed": bool(st.get("completed")), "detail": st.get("shortDetail", "")}
    for c in comp.get("competitors", []):
        out["scores"][(c.get("team") or {}).get("displayName", "")] = c.get("score")
    for t in (d.get("boxscore") or {}).get("teams", []) or []:
        out["teams"].append({
            "name": (t.get("team") or {}).get("displayName", ""),
            "stats": [{"label": s.get("label", s.get("name", "")), "value": s.get("displayValue", "")} for s in t.get("statistics", []) or []],
        })
    for t in d.get("leaders", []) or []:
        cats = []
        for cat in t.get("leaders", []) or []:
            top = (cat.get("leaders") or [{}])[0]
            ath = top.get("athlete") or {}
            if ath:
                cats.append({"cat": cat.get("displayName", ""), "player": ath.get("displayName", ""), "line": top.get("displayValue", "")})
        out["leaders"].append({"team": (t.get("team") or {}).get("displayName", ""), "cats": cats})
    wp = d.get("winprobability") or []
    if wp:
        out["winprob"] = round(float(wp[-1].get("homeWinPercentage", 0.5)) * 100)
    pr = d.get("predictor") or {}
    try:
        out["predictor"] = round(float((pr.get("homeTeam") or {}).get("gameProjection")))
    except (TypeError, ValueError):
        pass
    return out


def standings():
    if DEMO:
        return demo_standings()
    d = fetch_json(ESPN_STANDINGS, 1800)
    confs = []
    for conf in d.get("children", []) or []:
        rows = []
        entries = (conf.get("standings") or {}).get("entries", []) or []
        for en in entries:
            stats = {s.get("name"): s.get("displayValue") for s in en.get("stats", []) or []}
            t = en.get("team") or {}
            logos = t.get("logos") or [{}]
            rows.append({
                "team": t.get("displayName", ""), "abbr": t.get("abbreviation", ""), "logo": logos[0].get("href", ""),
                "w": stats.get("wins", ""), "l": stats.get("losses", ""), "t": stats.get("ties", ""),
                "pct": stats.get("winPercent", ""), "pf": stats.get("pointsFor", ""), "pa": stats.get("pointsAgainst", ""),
                "diff": stats.get("differential", stats.get("pointDifferential", "")), "streak": stats.get("streak", ""),
            })

        def key(r):
            try:
                return -float(r["pct"])
            except (TypeError, ValueError):
                return 0
        rows.sort(key=key)
        confs.append({"name": conf.get("name", conf.get("abbreviation", "")), "rows": rows})
    return {"conferences": confs}


# ---------- Odds: best line across books ----------
def to_dec(a):
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def mode(vals):
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: (counts[k], -abs(k))) if counts else None


def process_odds(raw):
    games = []
    for ev in raw:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        g = {"id": ev.get("id"), "commence": ev.get("commence_time"), "home": home, "away": away, "books": [], "best": {}}
        lines = {"h2h": {}, "spreads": {}, "totals": {}}
        for b in ev.get("bookmakers", []) or []:
            row = {"book": b.get("title", b.get("key", "")), "h2h": {}, "spreads": {}, "totals": {}}
            for m in b.get("markets", []) or []:
                mk = m.get("key")
                if mk not in lines:
                    continue
                for o in m.get("outcomes", []) or []:
                    name, price, point = o.get("name"), o.get("price"), o.get("point")
                    if price is None:
                        continue
                    lines[mk].setdefault(name, []).append({"book": row["book"], "price": price, "point": point})
                    row[mk][name] = {"price": price, "point": point}
            g["books"].append(row)
        best = {}
        for mk, outs in lines.items():
            best[mk] = {}
            # consensus point per side (spreads/totals), best price at that point
            for name, offers in outs.items():
                pt = mode([o["point"] for o in offers if o["point"] is not None]) if mk != "h2h" else None
                pool = [o for o in offers if mk == "h2h" or o["point"] == pt]
                if not pool:
                    continue
                top = max(pool, key=lambda o: to_dec(o["price"]))
                avg_imp = sum(1 / to_dec(o["price"]) for o in pool) / len(pool)
                best[mk][name] = {"price": top["price"], "book": top["book"], "point": pt, "imp": avg_imp, "books": len(pool)}
            tot = sum(v["imp"] for v in best[mk].values())
            for v in best[mk].values():
                v["prob"] = round(v["imp"] / tot, 4) if tot else None
                del v["imp"]
        g["best"] = best
        games.append(g)
    return games


ODDS_STATE = {"at": 0, "games": [], "remaining": None, "used": None, "error": None}


def odds(refresh=False):
    if DEMO:
        return {"games": demo_odds(), "updated": time.time(), "remaining": "demo", "error": None, "demo": True}
    now = time.time()
    age = now - ODDS_STATE["at"]
    want = age > ODDS_TTL or (refresh and age > ODDS_MIN_REFRESH)
    if not ODDS_API_KEY:
        return {"games": [], "updated": None, "remaining": None, "error": "No odds key set. Add ODDS_API_KEY in Railway Variables."}
    if want:
        q = urllib.parse.urlencode({"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h,spreads,totals", "oddsFormat": "american"})
        try:
            req = urllib.request.Request(ODDS_URL + "?" + q, headers={"User-Agent": "Mozilla/5.0 (StevesPicks)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = json.loads(r.read().decode("utf-8"))
                ODDS_STATE["remaining"] = r.headers.get("x-requests-remaining")
                ODDS_STATE["used"] = r.headers.get("x-requests-used")
            ODDS_STATE.update(at=now, games=process_odds(raw), error=None)
        except Exception as ex:  # keep showing the last good odds
            ODDS_STATE["error"] = "Couldn't load odds right now (" + str(ex)[:80] + ")."
    return {"games": ODDS_STATE["games"], "updated": ODDS_STATE["at"] or None, "remaining": ODDS_STATE["remaining"],
            "error": ODDS_STATE["error"], "next_refresh_in": max(0, int(ODDS_MIN_REFRESH - (now - ODDS_STATE["at"])))}


# ---------- Paper picks ----------
def load_picks():
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"start": 1000, "picks": []}


def save_picks(data):
    tmp = PICKS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PICKS_FILE)


def final_scores(gid):
    """Return {team name: score} if the game is final, else None."""
    try:
        d = game_detail(gid)
    except Exception:
        return None
    if not (d.get("status") or {}).get("completed"):
        return None
    try:
        return {k: float(v) for k, v in d["scores"].items()}
    except (TypeError, ValueError):
        return None


def grade_leg(leg, scores):
    home, away = leg["home"], leg["away"]
    hs, as_ = scores.get(home), scores.get(away)
    if hs is None or as_ is None:
        return None
    m, pick = leg["market"], leg["pick"]
    if m == "h2h":
        mine, theirs = (hs, as_) if pick == home else (as_, hs)
        return "win" if mine > theirs else "push" if mine == theirs else "loss"
    if m == "spreads":
        mine, theirs = (hs, as_) if pick == home else (as_, hs)
        v = mine + float(leg["point"]) - theirs
        return "win" if v > 0 else "push" if v == 0 else "loss"
    if m == "totals":
        tot, pt = hs + as_, float(leg["point"])
        if tot == pt:
            return "push"
        return "win" if (tot > pt) == (pick == "Over") else "loss"
    return None


def settle(p):
    res = [l.get("result") for l in p["legs"]]
    if any(r == "loss" for r in res):
        p["status"], p["profit"] = "loss", -p["stake"]
    elif all(r in ("win", "push") for r in res):
        live = [l for l in p["legs"] if l["result"] == "win"]
        if not live:
            p["status"], p["profit"] = "push", 0
        else:
            dec = 1
            for l in live:
                dec *= to_dec(l["price"])
            p["status"], p["profit"] = "win", round(p["stake"] * (dec - 1), 2)


def grade_all():
    with LOCK:
        data = load_picks()
        changed = False
        cache = {}
        for p in data["picks"]:
            if p.get("status") != "pending":
                continue
            for l in p["legs"]:
                if l.get("result"):
                    continue
                if l["gameId"] not in cache:
                    cache[l["gameId"]] = final_scores(l["gameId"])
                sc = cache[l["gameId"]]
                if sc:
                    r = grade_leg(l, sc)
                    if r:
                        l["result"] = r
                        l["final"] = f'{l["away"]} {int(sc.get(l["away"], 0))}, {l["home"]} {int(sc.get(l["home"], 0))}'
                        changed = True
            settle(p)
        if changed:
            save_picks(data)
        return data


def add_pick(body):
    legs = body.get("legs") or []
    stake = float(body.get("stake") or 0)
    if not legs or stake <= 0:
        raise ValueError("A pick needs at least one leg and a stake above 0.")
    clean = []
    for l in legs:
        clean.append({k: l.get(k) for k in ("gameId", "home", "away", "market", "pick", "point", "price", "book", "label", "kickoff")})
        clean[-1]["result"] = None
    dec = 1
    for l in clean:
        dec *= to_dec(l["price"])
    p = {"id": uuid.uuid4().hex[:10], "created": time.time(), "stake": stake, "legs": clean,
         "decimal": round(dec, 3), "toWin": round(stake * (dec - 1), 2), "status": "pending", "profit": 0,
         "note": (body.get("note") or "")[:120]}
    with LOCK:
        data = load_picks()
        data["picks"].insert(0, p)
        save_picks(data)
    return p


def delete_pick(pid):
    with LOCK:
        data = load_picks()
        data["picks"] = [p for p in data["picks"] if p["id"] != pid]
        save_picks(data)


# ---------- Demo data (DEMO=1) ----------
def _iso(hours):
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() + hours * 3600))

DEMO_TEAMS = {
    "BUF": ("Buffalo Bills", "Bills", "7-2"), "KC": ("Kansas City Chiefs", "Chiefs", "6-3"),
    "PHI": ("Philadelphia Eagles", "Eagles", "7-2"), "DAL": ("Dallas Cowboys", "Cowboys", "4-5"),
    "NYG": ("New York Giants", "Giants", "3-6"), "NYJ": ("New York Jets", "Jets", "3-6"),
    "DET": ("Detroit Lions", "Lions", "7-2"), "GB": ("Green Bay Packers", "Packers", "6-3"),
}

def _team(ab, score="", winner=False):
    n, s, r = DEMO_TEAMS[ab]
    return {"id": ab, "name": n, "short": s, "abbr": ab, "logo": "", "color": "", "score": score, "record": r, "winner": winner}

def demo_scoreboard():
    return {"week": 10, "seasontype": 2, "year": 2026, "games": [
        {"id": "d1", "date": _iso(-26), "name": "DAL @ PHI", "state": "post", "detail": "Final", "completed": True,
         "home": _team("PHI", "27", True), "away": _team("DAL", "17"), "tv": "FOX", "venue": "Lincoln Financial Field", "situation": None},
        {"id": "d2", "date": _iso(-1.5), "name": "GB @ DET", "state": "in", "detail": "10:42 - 3rd", "completed": False,
         "home": _team("DET", "21"), "away": _team("GB", "17"), "tv": "CBS", "venue": "Ford Field",
         "situation": {"down": "2nd & 7 at DET 34", "possession": "GB", "redzone": False, "last": "J. Love pass to R. Doubs for 9 yards"}},
        {"id": "d3", "date": _iso(27), "name": "KC @ BUF", "state": "pre", "detail": "Sun 4:25 PM", "completed": False,
         "home": _team("BUF"), "away": _team("KC"), "tv": "CBS", "venue": "Highmark Stadium", "situation": None},
        {"id": "d4", "date": _iso(31), "name": "NYJ @ NYG", "state": "pre", "detail": "Sun 8:20 PM", "completed": False,
         "home": _team("NYG"), "away": _team("NYJ"), "tv": "NBC", "venue": "MetLife Stadium", "situation": None},
    ]}

def demo_detail(eid):
    g = next((x for x in demo_scoreboard()["games"] if x["id"] == eid), None)
    if not g:
        return {"id": eid, "teams": [], "leaders": [], "status": None, "scores": {}}
    stats = lambda a, b, c, d: [{"label": "Total Yards", "value": a}, {"label": "Passing", "value": b}, {"label": "Rushing", "value": c}, {"label": "Turnovers", "value": d}]
    return {"id": eid, "status": {"state": g["state"], "completed": g["completed"], "detail": g["detail"]},
            "scores": {g["home"]["name"]: g["home"]["score"], g["away"]["name"]: g["away"]["score"]},
            "teams": [] if g["state"] == "pre" else [{"name": g["away"]["name"], "stats": stats("312", "221", "91", "1")}, {"name": g["home"]["name"], "stats": stats("358", "240", "118", "0")}],
            "leaders": [] if g["state"] == "pre" else [{"team": g["home"]["name"], "cats": [{"cat": "Passing", "player": "Starting QB", "line": "18/26, 240 YDS, 2 TD"}]}],
            "winprob": 64 if g["state"] == "in" else None, "predictor": 58 if g["state"] == "pre" else None}

def demo_standings():
    rows = lambda teams: [{"team": DEMO_TEAMS[t][0], "abbr": t, "logo": "", "w": DEMO_TEAMS[t][2].split("-")[0], "l": DEMO_TEAMS[t][2].split("-")[1], "t": "0",
                           "pct": "", "pf": "", "pa": "", "diff": "", "streak": ""} for t in teams]
    return {"conferences": [{"name": "American Football Conference", "rows": rows(["BUF", "KC", "NYJ"])},
                            {"name": "National Football Conference", "rows": rows(["PHI", "DET", "GB", "DAL", "NYG"])}]}

def demo_odds():
    def ev(home, away, hml, aml, hsp, total, t):
        books = []
        for i, b in enumerate(["DraftKings", "FanDuel", "BetMGM", "Caesars"]):
            j = int((i - 1.5) * 4)
            books.append({"title": b, "markets": [
                {"key": "h2h", "outcomes": [{"name": home, "price": hml + j}, {"name": away, "price": aml - j}]},
                {"key": "spreads", "outcomes": [{"name": home, "price": -110 + j, "point": hsp}, {"name": away, "price": -110 - j, "point": -hsp}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": -110 + j, "point": total}, {"name": "Under", "price": -110 - j, "point": total}]}]})
        return {"id": home[:3] + away[:3], "commence_time": _iso(t), "home_team": home, "away_team": away, "bookmakers": books}
    return process_odds([ev("Buffalo Bills", "Kansas City Chiefs", -135, 115, -2.5, 47.5, 27),
                         ev("New York Giants", "New York Jets", 105, -125, 1.5, 38.5, 31),
                         ev("Detroit Lions", "Green Bay Packers", -160, 135, -3.5, 49.5, -1.5)])


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path in ("/icon.png", "/manifest.json"):
                p = os.path.join(HERE, u.path.lstrip("/"))
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        return self.send(200, f.read(), "image/png" if p.endswith(".png") else "application/manifest+json")
            if u.path == "/api/scoreboard":
                return self.send(200, scoreboard(q.get("week"), q.get("seasontype")))
            if u.path == "/api/game":
                return self.send(200, game_detail(q.get("id", "")))
            if u.path == "/api/standings":
                return self.send(200, standings())
            if u.path == "/api/odds":
                return self.send(200, odds(q.get("refresh") == "1"))
            if u.path == "/api/picks":
                return self.send(200, grade_all())
            if u.path == "/health":
                return self.send(200, {"ok": True, "demo": DEMO, "oddsKey": bool(ODDS_API_KEY)})
            self.send(404, {"error": "Not found"})
        except Exception as ex:
            self.send(502, {"error": "Couldn't load data: " + str(ex)[:120]})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/picks":
                return self.send(200, add_pick(body))
            if u.path == "/api/picks/delete":
                delete_pick(q.get("id", ""))
                return self.send(200, {"ok": True})
            self.send(404, {"error": "Not found"})
        except ValueError as ex:
            self.send(400, {"error": str(ex)})
        except Exception as ex:
            self.send(500, {"error": str(ex)[:120]})


if __name__ == "__main__":
    print(f"Steve's Picks running on http://localhost:{PORT}  (demo={'on' if DEMO else 'off'}, odds key={'set' if ODDS_API_KEY else 'missing'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
