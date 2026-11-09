"""Steve's Picks — NFL paper betting on real Kalshi prices.

Stdlib only (no pip installs). Run:  python server.py   then open http://localhost:8080
Env vars:
  PORT       port to listen on (Railway sets this)            default 8080
  DATA_DIR   folder for bets.json (mount a Railway volume here) default ./data
  APP_PIN    optional PIN; if set, the app asks for it before placing bets
  START_BANK starting paper bankroll in dollars                default 1000
  MOCK=1     use built-in sample data (no internet needed) for testing
"""
import json, os, re, threading, time, uuid, math, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
APP_PIN = os.environ.get("APP_PIN", "").strip()
START_BANK = float(os.environ.get("START_BANK", "5000"))
MOCK = os.environ.get("MOCK", "") == "1"
HERE = os.path.dirname(os.path.abspath(__file__))

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
UA = {"User-Agent": "StevesPicks/1.0", "Accept": "application/json"}

# Kalshi and ESPN don't always use the same team codes
ALIASES = {"WSH": ["WSH", "WAS"], "JAX": ["JAX", "JAC"], "LAR": ["LAR", "LA"], "LV": ["LV", "LVR", "OAK"],
           "LAC": ["LAC", "SD"], "KC": ["KC", "KAN"], "GB": ["GB", "GNB"], "NE": ["NE", "NWE"],
           "NO": ["NO", "NOR"], "SF": ["SF", "SFO"], "TB": ["TB", "TAM"]}
FALLBACK_SERIES = ["KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLANYTD", "KXNFLPASSYDS",
                   "KXNFLRSHYDS", "KXNFLRECYDS", "KXNFLREC", "KXNFLPASSTDS", "KXNFL1HWINNER"]

# ---------------------------------------------------------------- http + cache
_cache, _cache_lock = {}, threading.Lock()

def get_json(url, ttl):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    if MOCK:
        import mockdata
        data = mockdata.fetch(url)
    else:
        data = None
        for attempt in range(3):
            if url.startswith(KALSHI):
                kalshi_pace()
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 2:
                    raise
                wait = float(e.headers.get("Retry-After") or 0) or (2 + attempt * 3)
                _pace["until"] = time.time() + wait   # everyone backs off together
                time.sleep(wait)
    with _cache_lock:
        _cache[url] = (now, data)
    return data

STATUS = {"espn": {"ok": None, "msg": "not checked yet", "at": 0},
          "kalshi": {"ok": None, "msg": "loading prices…", "at": 0}}

def mark(src, ok, msg):
    prev = STATUS[src].get("ok")
    STATUS[src] = {"ok": ok, "msg": msg, "at": time.time()}
    if prev != ok:
        print("[%s] %s: %s" % (time.strftime("%H:%M:%S"), src.upper(), ("OK - " if ok else "PROBLEM - ") + msg), flush=True)

_pace = {"last": 0.0, "until": 0.0}
_pace_lock = threading.Lock()

def kalshi_pace(gap=0.25):
    """At most ~4 Kalshi requests a second, and pause after Kalshi says slow down."""
    with _pace_lock:
        now = time.time()
        t = max(now, _pace["last"] + gap, _pace["until"])
        _pace["last"] = t
    if t > now:
        time.sleep(t - now)

_last_err = {"msg": "", "at": 0}

def safe(fn, default):
    try:
        return fn()
    except Exception as e:
        msg = str(e)
        if msg != _last_err["msg"] or time.time() - _last_err["at"] > 120:   # don't flood the window
            print("fetch error:", msg, flush=True)
            _last_err.update(msg=msg, at=time.time())
        return default

def money(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------- Kalshi
def nfl_series():
    def load():
        d = get_json(KALSHI + "/series?category=Sports", 3600)
        out = [{"ticker": s["ticker"], "title": s.get("title") or s["ticker"]}
               for s in d.get("series", []) if s.get("ticker", "").startswith("KXNFL")]
        return out or [{"ticker": t, "title": t} for t in FALLBACK_SERIES]
    return safe(load, [{"ticker": t, "title": t} for t in FALLBACK_SERIES])

def series_markets(series_ticker):
    out, cursor = [], ""
    for _ in range(5):
        q = {"series_ticker": series_ticker, "status": "open", "limit": "1000"}
        if cursor:
            q["cursor"] = cursor
        d = get_json(KALSHI + "/markets?" + urllib.parse.urlencode(q), 25)
        out += d.get("markets", [])
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    return out

GAME_KEY = re.compile(r"-(\d{2}[A-Z]{3}\d{2})([A-Z]{4,8})")

def game_key(event_ticker):
    m = GAME_KEY.search(event_ticker or "")
    return (m.group(1), m.group(2)) if m else None

def clean_market(m, series_title):
    yb, ya = money(m.get("yes_bid_dollars")), money(m.get("yes_ask_dollars"))
    nb, na = money(m.get("no_bid_dollars")), money(m.get("no_ask_dollars"))
    last = money(m.get("last_price_dollars"))
    # fill gaps the way the order book implies: No ask = 1 - Yes bid, etc.
    if na in (None, 0) and yb:
        na = round(1 - yb, 4)
    if ya in (None, 0) and nb:
        ya = round(1 - nb, 4)
    if yb in (None,) and na:
        yb = round(1 - na, 4)
    if nb in (None,) and ya:
        nb = round(1 - ya, 4)
    chance = None
    if yb and ya:
        chance = (yb + ya) / 2
    elif last:
        chance = last
    elif ya:
        chance = ya
    return {"ticker": m.get("ticker"), "event": m.get("event_ticker"), "series": series_title,
            "label": (m.get("yes_sub_title") or m.get("title") or m.get("ticker") or "").strip(),
            "no_label": m.get("no_sub_title") or "",
            "rules": m.get("rules_primary") or "",
            "full": m.get("title") or "",
            "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na, "last": last,
            "chance": round(chance * 100) if chance is not None else None,
            "volume": money(m.get("volume_fp")) or 0, "close": m.get("close_time"),
            "tradable": bool((ya and 0 < ya < 1) or (na and 0 < na < 1)),
            "status": m.get("status"), "result": m.get("result") or ""}

CORE = ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL")
_series_data = {}   # ticker -> (fetched_at, markets)

def all_nfl_markets():
    """Returns {(date, teams): [market, ...]} for every open NFL market on Kalshi.
    Winner/spread/total refresh every loop; props every 3 minutes (Kalshi rate-limits heavy use)."""
    series = [x for x in nfl_series() if not re.search(r"protected|precise|escalator|margin", x.get("title") or "", re.I)]
    now = time.time()
    due = [x for x in series if x["ticker"] in CORE or now - _series_data.get(x["ticker"], (0, None))[0] > 180]
    with ThreadPoolExecutor(max_workers=2) as ex:
        for x, mk in ex.map(lambda x: (x, safe(lambda: series_markets(x["ticker"]), None)), due):
            if mk is not None:
                _series_data[x["ticker"]] = (time.time(), mk)
    results = [(x, _series_data.get(x["ticker"], (0, []))[1]) for x in series]
    groups = {}
    for s, mkts in results:
        for m in mkts:
            k = game_key(m.get("event_ticker"))
            if not k:
                continue
            title = re.sub(r"^Pro Football\s+", "", (s["title"] or "").replace("  ", " ")).strip()
            cm = clean_market(m, title[:1].upper() + title[1:] if title else s["ticker"])
            cm["series_ticker"] = s["ticker"]
            if s["ticker"] == "KXNFLGAME":
                cm["series"] = "Winner"
            groups.setdefault(k, []).append(cm)
    return groups

_snapshot = {"groups": {}, "at": 0}

LAST_VISIT = {"t": time.time()}
IDLE_AFTER = 300  # seconds with nobody using the app before background checks pause (lets Railway sleep)

def idle():
    return time.time() - LAST_VISIT["t"] > IDLE_AFTER

def kalshi_loop():
    while True:
        if idle():
            time.sleep(5)
            continue
        try:
            g = all_nfl_markets()
            n = sum(len(v) for v in g.values())
            if n:
                _snapshot.update(groups=g, at=time.time())
                mark("kalshi", True, "%d NFL markets across %d games" % (n, len(g)))
            else:
                mark("kalshi", False, "Kalshi answered but listed no open NFL markets (or it's blocked on this network)")
        except Exception as e:
            mark("kalshi", False, "can't reach Kalshi: %s" % e)
        time.sleep(30)

def snapshot():
    return _snapshot["groups"]

def markets_by_ticker(tickers):
    out = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        d = safe(lambda: get_json(KALSHI + "/markets?" + urllib.parse.urlencode({"tickers": ",".join(chunk), "limit": "1000"}), 20), {})
        for m in d.get("markets", []):
            out[m["ticker"]] = clean_market(m, "")
    return out

def codes(abbr):
    return ALIASES.get(abbr, [abbr])

def match_key(groups, away, home):
    want = set()
    for a in codes(away):
        for h in codes(home):
            want.add(a + h)
            want.add(h + a)
    for k in groups:
        if k[1] in want:
            return k
    return None

def order_markets(mkts):
    def rank(m):
        st = m.get("series_ticker", "")
        if st == "KXNFLGAME": return 0
        if "SPREAD" in st: return 1
        if "TOTAL" in st: return 2
        return 3
    return sorted(mkts, key=lambda m: (rank(m), m["series"], -(m["chance"] or 0)))

# ---------------------------------------------------------------- ESPN
def espn_scoreboard(week=None, stype=None, year=None):
    url = ESPN + "/scoreboard"
    if week:
        q = {"week": week, "seasontype": stype or 2}
        if year:
            q["dates"] = year
        url += "?" + urllib.parse.urlencode(q)
    return get_json(url, 10 if not week else 30)

def season_weeks(sb):
    """Week list from ESPN's calendar: [{'key':'2-10','label':'Week 10','dates':'Nov 12-18'}]"""
    out = []
    for lg in sb.get("leagues") or []:
        for part in lg.get("calendar") or []:
            if not isinstance(part, dict):
                continue
            st = str(part.get("value") or "")
            if st not in ("2", "3"):
                continue
            for e in part.get("entries") or []:
                out.append({"key": "%s-%s" % (st, e.get("value")), "label": e.get("label") or ("Week " + str(e.get("value"))),
                            "dates": e.get("detail") or "", "start": e.get("startDate")})
    return out

_weeks_cache = {"list": [], "year": None}

def team_info(c):
    t = c.get("team", {})
    rec = (c.get("records") or [{}])[0].get("summary", "")
    return {"abbr": t.get("abbreviation"), "name": t.get("displayName"), "short": t.get("shortDisplayName") or t.get("name"),
            "logo": t.get("logo") or ((t.get("logos") or [{}])[0] or {}).get("href"), "color": t.get("color"), "score": c.get("score"), "record": rec,
            "home": c.get("homeAway") == "home"}

def parse_event(ev):
    comp = (ev.get("competitions") or [{}])[0]
    teams = [team_info(c) for c in comp.get("competitors", [])]
    home = next((t for t in teams if t["home"]), teams[0] if teams else {})
    away = next((t for t in teams if not t["home"]), teams[1] if len(teams) > 1 else {})
    st = comp.get("status") or ev.get("status") or {}
    stype = st.get("type", {})
    sit = comp.get("situation") or {}
    bc = comp.get("broadcasts") or []
    tv = ", ".join(n for b in bc for n in b.get("names", []))
    return {"id": ev.get("id"), "name": ev.get("name"), "date": ev.get("date"),
            "state": stype.get("state"), "detail": stype.get("shortDetail") or stype.get("detail"),
            "clock": st.get("displayClock"), "period": st.get("period"),
            "home": home, "away": away, "venue": (comp.get("venue") or {}).get("fullName"), "tv": tv,
            "situation": {"down": sit.get("shortDownDistanceText") or sit.get("downDistanceText"),
                          "possession": sit.get("possession"), "lastPlay": (sit.get("lastPlay") or {}).get("text"),
                          "redzone": sit.get("isRedZone")} if sit else None}

def games(week=None):
    stype, wnum = None, None
    if week and "-" in str(week):
        stype, wnum = str(week).split("-", 1)
    elif week:
        wnum = week
    try:
        if not _weeks_cache["list"]:
            cur = espn_scoreboard()
            _weeks_cache.update(list=season_weeks(cur), year=(cur.get("season") or {}).get("year"))
        sb = espn_scoreboard(wnum, stype, _weeks_cache["year"]) if wnum else espn_scoreboard()
        mark("espn", True, "%d games on the schedule" % len(sb.get("events", [])))
    except Exception as e:
        sb = {}
        mark("espn", False, "can't reach ESPN: %s" % e)
    evs = [parse_event(e) for e in sb.get("events", [])]
    groups = snapshot()
    for g in evs:
        k = match_key(groups, g["away"].get("abbr", ""), g["home"].get("abbr", ""))
        mk = groups.get(k, []) if k else []
        g["market_count"] = len(mk)
        win = [m for m in mk if m.get("series_ticker") == "KXNFLGAME"]
        for m in win:
            suf = m["ticker"].rsplit("-", 1)[-1]
            for side in ("away", "home"):
                if suf in codes(g[side].get("abbr", "")):
                    m["team"] = g[side]["abbr"]
                    m["label"] = g[side].get("short") or m["label"]
        g["winner"] = sorted([m for m in win if m.get("team")], key=lambda m: m["team"] != g["away"].get("abbr"))
        # the spread and total lines closest to a coin flip, for the weekly pick sheet
        def near50(ms):
            ms = [m for m in ms if m.get("chance") is not None]
            return sorted(ms, key=lambda m: abs(m["chance"] - 50))
        g["spreads"] = near50([m for m in mk if "SPREAD" in m.get("series_ticker", "")])[:2]
        g["totals"] = near50([m for m in mk if "TOTAL" in m.get("series_ticker", "")])[:1]
    order = {"in": 0, "pre": 1, "post": 2}
    evs.sort(key=lambda g: (order.get(g["state"], 3), g["date"] or ""))
    wk = (sb.get("week") or {}).get("number")
    st = (sb.get("season") or {}).get("type") or 2
    if not week and season_weeks(sb):
        _weeks_cache.update(list=season_weeks(sb), year=(sb.get("season") or {}).get("year"))
    return {"week": wk, "weekKey": "%s-%s" % (st, wk), "season": (sb.get("season") or {}).get("year"), "games": evs,
            "weeks": _weeks_cache["list"], "updated": time.time(), "status": STATUS}

def label_repeats(mk):
    """If one market group shows the same label twice (e.g. Race to Points: Buffalo, Buffalo), add the number that tells them apart."""
    seen = {}
    for m in mk:
        seen.setdefault((m.get("series_ticker"), m["label"]), []).append(m)
    for (_, lab), ms in seen.items():
        if len(ms) < 2:
            continue
        for m in ms:
            num = re.findall(r"\d+(?:\.\d+)?", m.get("full") or "") or re.findall(r"\d+", m["ticker"].rsplit("-", 1)[-1])
            if num:
                m["label"] = "%s · %s" % (lab, (m.get("full") or "").strip() or num[-1])

def tag_teams(mk, g):
    """Kalshi player tickers start with the team code, e.g. ...-BUFJALLEN17-1 -> Bills."""
    sides = []
    for side in ("away", "home"):
        ab = (g.get(side) or {}).get("abbr") or ""
        for c in codes(ab):
            sides.append((c, ab))
    sides.sort(key=lambda x: -len(x[0]))
    for m in mk:
        parts = (m.get("ticker") or "").split("-")
        if len(parts) < 3 or m.get("series_ticker") == "KXNFLGAME":
            continue
        seg = parts[2]
        for c, ab in sides:
            if seg.startswith(c) and re.match(r"^[A-Z]+\d+$", seg[len(c):] or ""):
                m["pteam"] = ab
                break

def game_detail(eid):
    s = safe(lambda: get_json(ESPN + "/summary?" + urllib.parse.urlencode({"event": eid}), 8), {})
    hdr = s.get("header", {})
    comp = (hdr.get("competitions") or [{}])[0]
    ev = {"id": eid, "competitions": [comp], "date": comp.get("date"), "name": "", "status": comp.get("status")}
    # summary header lacks situation; borrow it from the scoreboard when live
    g = parse_event(ev)
    sb = safe(lambda: espn_scoreboard(), {})
    for e in sb.get("events", []):
        if e.get("id") == str(eid):
            g = parse_event(e)
    # play by play: most recent drives first
    drives = []
    dsrc = (s.get("drives") or {})
    cur = dsrc.get("current")
    alld = (dsrc.get("previous") or [])[:]
    if cur and (not alld or cur.get("id") != alld[-1].get("id")):
        alld.append(cur)
    for d in reversed(alld[-12:]):
        drives.append({"team": (d.get("team") or {}).get("abbreviation"), "desc": d.get("description"),
                       "result": d.get("displayResult") or d.get("result"),
                       "plays": [{"text": p.get("text"), "clock": (p.get("clock") or {}).get("displayValue"),
                                  "period": (p.get("period") or {}).get("number"),
                                  "score": p.get("scoringPlay"), "down": (p.get("start") or {}).get("downDistanceText")}
                                 for p in reversed(d.get("plays") or [])]})
    leaders = []
    for team in s.get("leaders", []) or []:
        for cat in team.get("leaders", []) or []:
            top = (cat.get("leaders") or [{}])[0]
            ath = top.get("athlete") or {}
            leaders.append({"team": (team.get("team") or {}).get("abbreviation"), "cat": cat.get("displayName"),
                            "name": ath.get("displayName"), "line": top.get("displayValue")})
    scoring = [{"text": p.get("text"), "team": (p.get("team") or {}).get("abbreviation"),
                "period": (p.get("period") or {}).get("number"), "clock": (p.get("clock") or {}).get("displayValue"),
                "away": p.get("awayScore"), "home": p.get("homeScore")} for p in (s.get("scoringPlays") or [])]
    lines = []
    for c in comp.get("competitors", []) or []:
        lines.append({"abbr": (c.get("team") or {}).get("abbreviation"),
                      "q": [l.get("displayValue") for l in (c.get("linescores") or [])]})
    groups = snapshot()
    k = match_key(groups, g["away"].get("abbr", ""), g["home"].get("abbr", ""))
    mk = order_markets(groups.get(k, [])) if k else []
    tag_teams(mk, g)
    label_repeats(mk)
    return {"game": g, "drives": drives, "leaders": leaders, "scoring": scoring, "linescores": lines,
            "markets": mk, "updated": time.time()}

# ---------------------------------------------------------------- recommended picks
def payout_for(price, stake=10.0):
    c = stake / price
    while c > 0 and c * price + kalshi_fee(c, price) > stake + 1e-9:
        c -= 0.01
    return round(c, 2)

def recommendations():
    """Picks built only from games that have a live, tradable Kalshi price.
    Chance = Kalshi's own price, so these lean on the market, not a secret edge."""
    data = games()
    legs = []
    for g in data["games"]:
        if g["state"] == "post":
            continue
        for m in g.get("winner", []):
            if m.get("tradable") and m.get("yes_ask") and m.get("chance") is not None:
                opp = next((x for x in g["winner"] if x is not m), None)
                legs.append({"ticker": m["ticker"], "side": "yes", "label": m["label"] + " to win",
                             "game": "%s @ %s" % (g["away"]["abbr"], g["home"]["abbr"]), "series": "Moneyline",
                             "price": m["yes_ask"], "chance": m["chance"], "state": g["state"], "date": g["date"],
                             "opp": opp["label"] if opp else ""})
    legs.sort(key=lambda l: -l["chance"])
    by_game = {}
    for l in legs:
        by_game.setdefault(l["game"], l)  # favorite in each game
    favs = sorted(by_game.values(), key=lambda l: -l["chance"])

    def combo(ls, name, why):
        p = 1.0
        for l in ls:
            p *= l["price"]
        p = round(p, 3)
        ch = 1.0
        for l in ls:
            ch *= l["chance"] / 100
        return {"name": name, "why": why, "legs": ls, "chance": round(ch * 100), "price": p,
                "pays10": payout_for(p) if 0 < p < 1 else 0}

    out = []
    safe_singles = [l for l in favs if 58 <= l["chance"] <= 90][:5]
    for l in safe_singles:
        out.append(combo([l], "Single", "Strong favorite at %d%% that still pays something" % l["chance"]))
    # doubles that land near a coin flip (about 2x your money)
    pairs = []
    for i in range(len(favs)):
        for j in range(i + 1, len(favs)):
            p = favs[i]["price"] * favs[j]["price"]
            if 0.40 <= p <= 0.56:
                pairs.append((abs(p - 0.5), favs[i], favs[j]))
    for _, a, b in sorted(pairs, key=lambda x: x[0])[:3]:
        c = combo([a, b], "Double", "")
        c["why"] = "Two favorites together, about %.1fx your money" % (c["pays10"] / 10)
        out.append(c)
    top3 = [l for l in favs if l["chance"] >= 65][:3]
    if len(top3) == 3:
        out.append(combo(top3, "3-leg combo", "The three biggest favorites on the board"))
    return {"week": data["week"], "picks": out, "updated": time.time()}

# ---------------------------------------------------------------- paper bets
_store_lock = threading.Lock()
BETS_FILE = os.path.join(DATA_DIR, "bets.json")

def kalshi_fee(contracts, price):
    """Kalshi taker fee: round up to the cent of 0.07 * C * P * (1-P)."""
    if not price or price <= 0 or price >= 1:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100

def load_store():
    try:
        with open(BETS_FILE) as f:
            st = json.load(f)
        # accounts that were never used get the new starting balance
        if not st.get("bets") and st.get("start") != START_BANK and not st.get("deposits"):
            st.update(bank=START_BANK, start=START_BANK)
        return st
    except Exception:
        return {"bank": START_BANK, "start": START_BANK, "bets": []}

def save_store(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = BETS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, BETS_FILE)

def leg_price(m, side):
    p = m.get("yes_ask") if side == "yes" else m.get("no_ask")
    if not p:
        p = m.get("last") if side == "yes" else (1 - m["last"] if m.get("last") else None)
    return p

# ---------------------------------------------------------------- slip check
def leg_info(ticker):
    """Break a Kalshi ticker into what it bets on: series, game, team/number."""
    parts = (ticker or "").split("-")
    series = parts[0] if parts else ""
    k = game_key(ticker)
    suf = parts[-1] if len(parts) >= 3 else ""
    m = re.match(r"^([A-Z]+)?(\d+(?:\.\d+)?)?$", suf)
    team = (m.group(1) if m else None) or None
    num = float(m.group(2)) if m and m.group(2) else None
    kind = "win" if series == "KXNFLGAME" else "spread" if "SPREAD" in series else "total" if "TOTAL" in series else "prop"
    return {"series": series, "game": k, "team": team, "num": num, "kind": kind}

def same_team(a, b):
    if not a or not b:
        return False
    for canon, al in ALIASES.items():
        if a in al and b in al:
            return True
    return a == b

def check_slip(legs):
    """Plain-rule check of a slip. Returns warnings, worst first."""
    out = []
    def add(level, text):
        out.append({"level": level, "text": text})
    infos = [dict(l, **leg_info(l["ticker"])) for l in legs]
    # 1) legs inside the slip that fight each other
    for i in range(len(infos)):
        for j in range(i + 1, len(infos)):
            a, b = infos[i], infos[j]
            if a["ticker"] == b["ticker"]:
                if a["side"] != b["side"]:
                    add("stop", "%s is in here as both Yes and No. One of them always loses." % a["label"])
                continue
            if not a["game"] or a["game"] != b["game"]:
                continue
            if a["kind"] == b["kind"] == "win":
                ya = a["side"] == "yes"; yb = b["side"] == "yes"
                if ya and yb:
                    add("stop", "You picked both teams to win the same game. This combo can't hit.")
                elif ya != yb:
                    add("warn", "%s and %s are the same pick written two ways. It doesn't add anything." % (a["label"], b["label"]))
            if {a["kind"], b["kind"]} == {"win", "spread"}:
                w, sp = (a, b) if a["kind"] == "win" else (b, a)
                w_team = w["team"]; sp_team = sp["team"]
                if w["side"] == "yes" and sp["side"] == "yes" and sp_team and not same_team(w_team, sp_team):
                    add("stop", "You have %s to win but the other team covering the spread. Those can't both happen." % w["label"])
                if w["side"] == "yes" and sp["side"] == "no" and same_team(w_team, sp_team):
                    add("warn", "%s to win but NOT cover only hits if they win by a small margin. That's a narrow window." % w["label"])
            if a["kind"] == b["kind"] == "total" and a["num"] and b["num"]:
                over = [x for x in (a, b) if x["side"] == "yes"]
                under = [x for x in (a, b) if x["side"] == "no"]
                if over and under and over[0]["num"] >= under[0]["num"]:
                    add("stop", "Your Over and Under on the same game can't both hit.")
    # 2) slip vs bets already open
    st = load_store()
    for b in st["bets"]:
        if b["status"] != "open":
            continue
        for ol in b["legs"]:
            oi = leg_info(ol["ticker"])
            for l in infos:
                if l["ticker"] == ol["ticker"] and l["side"] != ol["side"]:
                    add("warn", "This goes against a bet you already have open (%s %s). If one wins, the other loses." % (ol["side"].upper(), ol["label"]))
                elif l["kind"] == oi["kind"] == "win" and l["game"] and l["game"] == oi["game"] and l["ticker"] != ol["ticker"] and l["side"] == ol["side"] == "yes":
                    add("warn", "You already have an open bet on the other team in this game (%s). You'd be betting against yourself." % ol["label"])
                elif l["ticker"] == ol["ticker"] and l["side"] == ol["side"]:
                    add("info", "You already have this pick open (%s). This doubles your risk on it." % ol["label"])
    # 3) how likely the slip is
    p = 1.0
    for l in legs:
        p *= max(min(float(l.get("price") or 0), 0.999), 0.001)
    if len(legs) > 1:
        games = [x["game"] for x in infos if x["game"]]
        if len(games) != len(set(games)):
            add("info", "Some legs are from the same game. They tend to move together, so Kalshi's real combo price can be different.")
        if p < 0.05:
            add("warn", "This combo hits about 1 time in %d. Fun for a few bucks, but expect it to lose." % round(1 / p))
        elif p < 0.15:
            add("info", "About a 1 in %d shot (%d%%). Keep the stake small." % (round(1 / p), round(p * 100)))
        if len(legs) >= 6:
            add("warn", "%d legs is a lot. Every extra leg is another way to lose; 3-4 strong legs usually hold up better." % len(legs))
        weak = [x for x in legs if (x.get("price") or 1) < 0.35]
        if weak and len(legs) > 1:
            add("info", "%s is a long shot by itself (%d%%) and drags the whole combo down." % (weak[0]["label"], round(weak[0]["price"] * 100)))
    else:
        pr = float(legs[0].get("price") or 0)
        if pr >= 0.9:
            add("info", "Big favorite: you risk about $%.0f to win $1. One upset wipes out a lot of small wins." % (pr / (1 - pr)))
    order = {"stop": 0, "warn": 1, "info": 2}
    out.sort(key=lambda w: order[w["level"]])
    seen, uniq = set(), []
    for w in out:
        if w["text"] not in seen:
            seen.add(w["text"]); uniq.append(w)
    verdict = "bad" if any(w["level"] == "stop" for w in uniq) else "risky" if any(w["level"] == "warn" for w in uniq) else "ok"
    return {"verdict": verdict, "chance": round(p * 100, 1), "warnings": uniq}

def place_bet(body):
    legs_in = body.get("legs") or []
    if not legs_in or len(legs_in) > 10:
        raise ValueError("Pick between 1 and 10 legs")
    live = markets_by_ticker([l["ticker"] for l in legs_in])
    legs, prob = [], 1.0
    for l in legs_in:
        m = live.get(l["ticker"])
        side = "no" if l.get("side") == "no" else "yes"
        if not m:
            raise ValueError("That market isn't available right now")
        if m.get("status") not in (None, "active", "open"):
            raise ValueError("Market closed: " + m["label"])
        p = leg_price(m, side)
        if not p or p <= 0 or p >= 1:
            raise ValueError("No price on " + m["label"])
        prob *= p
        legs.append({"ticker": m["ticker"], "side": side, "label": l.get("label") or m["label"],
                     "game": l.get("game") or "", "series": l.get("series") or "", "price": round(p, 4), "result": None})
    combo = len(legs) > 1
    price = round(prob, 3) if combo else legs[0]["price"]
    price = min(max(price, 0.001), 0.999)
    mode = body.get("mode", "dollars")
    amt = float(body.get("amount") or 0)
    if amt <= 0:
        raise ValueError("Enter an amount")
    if mode == "contracts":
        contracts = round(amt, 2)
    else:
        # spend `amt` dollars including fee
        contracts = amt / price
        while contracts > 0 and contracts * price + kalshi_fee(contracts, price) > amt + 1e-9:
            contracts -= 0.01
        contracts = round(contracts, 2)
    if contracts <= 0:
        raise ValueError("Amount too small")
    fee = kalshi_fee(contracts, price)
    cost = round(contracts * price + fee, 2)
    with _store_lock:
        st = load_store()
        if cost > st["bank"] + 1e-9:
            raise ValueError("Not enough paper balance (${:.2f} left)".format(st["bank"]))
        bet = {"id": uuid.uuid4().hex[:10], "placed": time.time(), "combo": combo, "legs": legs,
               "week": body.get("week"),
               "price": price, "contracts": contracts, "fee": fee, "cost": cost,
               "payout": round(contracts, 2), "status": "open", "pnl": 0.0, "settled": None}
        st["bank"] = round(st["bank"] - cost, 2)
        st["bets"].insert(0, bet)
        save_store(st)
    return bet

def sell_bet(bid):
    """Cash out a single (non-combo) open bet at the current bid, like selling on Kalshi."""
    with _store_lock:
        st = load_store()
        b = next((x for x in st["bets"] if x["id"] == bid), None)
        if not b or b["status"] != "open":
            raise ValueError("Bet isn't open")
        if b["combo"]:
            raise ValueError("Combos can't be sold early")
        leg = b["legs"][0]
        m = markets_by_ticker([leg["ticker"]]).get(leg["ticker"])
        bid_p = (m or {}).get("yes_bid" if leg["side"] == "yes" else "no_bid")
        if not bid_p:
            raise ValueError("No buyers right now")
        fee = kalshi_fee(b["contracts"], bid_p)
        proceeds = round(b["contracts"] * bid_p - fee, 2)
        b.update(status="sold", settled=time.time(), pnl=round(proceeds - b["cost"], 2), sold_at=bid_p, proceeds=proceeds)
        st["bank"] = round(st["bank"] + proceeds, 2)
        save_store(st)
        return b

def settle_open():
    with _store_lock:
        st = load_store()
        open_bets = [b for b in st["bets"] if b["status"] == "open"]
    if not open_bets:
        return
    tick = {l["ticker"] for b in open_bets for l in b["legs"] if l["result"] is None}
    if not tick:
        return
    # settled markets drop out of status=open lists; ask for them directly, uncached
    with _cache_lock:
        for k in list(_cache):
            if "tickers=" in k:
                _cache.pop(k, None)
    live = markets_by_ticker(tick)
    with _store_lock:
        st = load_store()
        changed = False
        for b in st["bets"]:
            if b["status"] != "open":
                continue
            for l in b["legs"]:
                m = live.get(l["ticker"])
                if l["result"] is None and m and m.get("result") in ("yes", "no"):
                    l["result"] = "win" if m["result"] == l["side"] else "loss"
                    changed = True
            res = [l["result"] for l in b["legs"]]
            if "loss" in res:
                b.update(status="lost", settled=time.time(), pnl=-b["cost"]); changed = True
            elif all(r == "win" for r in res):
                b.update(status="won", settled=time.time(), pnl=round(b["payout"] - b["cost"], 2))
                st["bank"] = round(st["bank"] + b["payout"], 2); changed = True
        if changed:
            save_store(st)

def record():
    st = load_store()
    done = [b for b in st["bets"] if b["status"] in ("won", "lost", "sold")]
    w = sum(1 for b in done if b["status"] == "won" or (b["status"] == "sold" and b["pnl"] > 0))
    l = len(done) - w
    staked = sum(b["cost"] for b in done)
    pnl = round(sum(b["pnl"] for b in done), 2)
    open_risk = round(sum(b["cost"] for b in st["bets"] if b["status"] == "open"), 2)
    streak, kind = 0, None
    for b in sorted(done, key=lambda x: x["settled"] or 0, reverse=True):
        k = "W" if b["pnl"] > 0 else "L"
        if kind is None:
            kind = k
        if k != kind:
            break
        streak += 1
    def split(fn):
        ds = [b for b in done if fn(b)]
        ww = sum(1 for b in ds if b["pnl"] > 0)
        return {"w": ww, "l": len(ds) - ww, "pnl": round(sum(b["pnl"] for b in ds), 2)}
    return {"bank": st["bank"], "start": st["start"], "wins": w, "losses": l, "pnl": pnl,
            "roi": round(pnl / staked * 100, 1) if staked else 0, "open_risk": open_risk,
            "open": sum(1 for b in st["bets"] if b["status"] == "open"),
            "streak": (kind or "") + (str(streak) if streak else ""),
            "singles": split(lambda b: not b["combo"]), "combos": split(lambda b: b["combo"]),
            "types": {t: split(lambda b, t=t: bet_type(b) == t) for t in ("Moneyline", "Spread", "Total", "Prop", "Combo")},
            "weeks": sorted([dict(week=w, **split(lambda b, w=w: b.get("week") == w))
                             for w in {b.get("week") for b in done if b.get("week")}], key=lambda x: -int(x["week"])),
            "equity": equity_curve(st)}

def bet_type(b):
    if b["combo"]:
        return "Combo"
    st = b["legs"][0].get("series_ticker") or b["legs"][0]["ticker"]
    if st.startswith("KXNFLGAME"): return "Moneyline"
    if "SPREAD" in st: return "Spread"
    if "TOTAL" in st: return "Total"
    return "Prop"

def equity_curve(st):
    pts, bal = [], st["start"]
    for b in sorted([b for b in st["bets"] if b["settled"]], key=lambda x: x["settled"]):
        bal += b["pnl"]
        pts.append([b["settled"], round(bal, 2)])
    return pts

def settler():
    while True:
        if not idle():
            safe(settle_open, None)
        time.sleep(60)

# ---------------------------------------------------------------- phone app (logo, manifest, icons)
LOGO_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><path id="sh" d="M256 34 C330 70 392 78 446 74 L446 250 C446 360 370 430 256 478 C142 430 66 360 66 250 L66 74 C120 78 182 70 256 34 Z"/><path id="sp" d="M170.5 302.9C218.6 302.9 248.8 282.7 254.8 246.2C259.8 215.6 244.1 198.1 203.4 190.5L189.1 187.8C170.8 184.3 164.7 179.5 166.2 170.8C167.7 161.6 178.8 155.2 193.0 155.2C208.7 155.2 217.0 162.5 216.1 175.7H262.8C267.9 137.6 241.9 115.1 196.2 115.1C153.3 115.1 120.8 137.1 115.3 170.1C110.5 198.6 127.6 218.8 161.6 225.4L179.1 228.8C198.9 232.6 206.1 238.1 204.6 246.8C203.1 256.4 191.2 262.8 175.2 262.8C156.8 262.8 146.7 254.2 148.4 239.4H100.4C93.8 279.3 118.8 302.9 170.5 302.9Z M256.1 300.0H306.9L315.9 245.0H342.4C385.9 245.0 417.2 220.5 423.7 181.5C430.1 142.6 406.9 118.1 363.5 118.1H286.3ZM322.9 203.3 330.1 159.8H352.8C368.0 159.8 376.3 168.2 374.1 181.5C371.8 194.8 360.8 203.3 345.6 203.3Z"/><linearGradient id="bg1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a3038"/><stop offset="1" stop-color="#14171b"/></linearGradient><linearGradient id="sh1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#262c34"/><stop offset="1" stop-color="#101317"/></linearGradient><linearGradient id="chrome" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f4f7fa"/><stop offset=".48" stop-color="#c9d1da"/><stop offset=".52" stop-color="#9aa6b3"/><stop offset="1" stop-color="#e8edf2"/></linearGradient><linearGradient id="grn" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5ff0b3"/><stop offset="1" stop-color="#1fb87b"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="url(#bg1)"/><use href="#sh" fill="url(#grn)" transform="translate(256 256) scale(1.0) translate(-256 -256)"/><use href="#sh" fill="url(#sh1)" transform="translate(256 258) scale(.94) translate(-256 -256)"/><use href="#sh" fill="none" stroke="url(#chrome)" stroke-width="3" transform="translate(256 258) scale(.88) translate(-256 -256)"/><path d="M256.0,79.0 L259.2,87.5 L268.3,87.9 L261.1,93.6 L263.6,102.5 L256.0,97.4 L248.3,102.5 L250.8,93.6 L243.6,87.9 L252.7,87.5Z" fill="url(#grn)"/><path d="M210.0,92.0 L211.9,97.2 L217.6,97.5 L213.2,101.0 L214.7,106.4 L210.0,103.3 L205.3,106.4 L206.8,101.0 L202.3,97.5 L208.0,97.2Z" fill="#c9d1da"/><path d="M302.0,92.0 L303.9,97.2 L309.6,97.5 L305.2,101.0 L306.7,106.4 L302.0,103.3 L297.3,106.4 L298.8,101.0 L294.3,97.5 L300.0,97.2Z" fill="#c9d1da"/><g transform="translate(6 8)"><use href="#sp" fill="#0b0d10" opacity=".55"/></g><use href="#sp" fill="url(#grn)" transform="translate(3 4)"/><use href="#sp" fill="url(#chrome)" stroke="#0b0d10" stroke-width="5" paint-order="stroke"/><line x1="181.0" y1="345.0" x2="331.0" y2="345.0" stroke="#e8edf2" stroke-width="7" stroke-linecap="round"/><line x1="196.0" y1="332.0" x2="196.0" y2="358.0" stroke="#e8edf2" stroke-width="7" stroke-linecap="round"/><line x1="226.0" y1="332.0" x2="226.0" y2="358.0" stroke="#e8edf2" stroke-width="7" stroke-linecap="round"/><line x1="256.0" y1="332.0" x2="256.0" y2="358.0" stroke="#e8edf2" stroke-width="7" stroke-linecap="round"/><line x1="286.0" y1="332.0" x2="286.0" y2="358.0" stroke="#e8edf2" stroke-width="7" stroke-linecap="round"/><line x1="316.0" y1="332.0" x2="316.0" y2="358.0" stroke="#e8edf2" stroke-width="7" stroke-linecap="round"/><path d="M108 372 L404 372 L384 404 L404 436 L108 436 L128 404 Z" fill="url(#grn)"/><path d="M108 372 L404 372 L384 404 L404 436 L108 436 L128 404 Z" fill="none" stroke="#0b0d10" stroke-opacity=".35" stroke-width="2"/><path d="M141.7 415.3C147.6 415.3 151.0 412.7 151.0 408.1C151.0 404.6 148.9 402.7 143.9 401.8L142.1 401.5C139.9 401.0 138.9 400.5 138.9 399.5C138.9 398.4 140.1 397.6 141.8 397.6C143.6 397.6 144.8 398.5 145.0 400.0H150.6C150.4 395.5 147.1 392.8 141.7 392.8C136.4 392.8 132.8 395.6 132.8 399.7C132.8 403.0 135.0 405.2 139.3 406.0L141.4 406.4C143.9 406.9 145.0 407.5 145.0 408.6C145.0 409.7 143.7 410.5 141.8 410.5C139.6 410.5 138.2 409.5 138.1 407.7H132.3C132.3 412.6 135.7 415.3 141.7 415.3Z M154.3 398.3H160.6V415.0H166.7V398.3H172.9V393.1H154.3Z M176.4 415.0H193.3V409.8H182.5V406.5H192.5V401.6H182.5V398.3H193.3V393.1H176.4Z M203.3 415.0H211.4L218.5 393.1H211.9L209.5 401.6C208.7 404.1 208.1 406.7 207.4 409.2C206.7 406.7 206.0 404.1 205.2 401.6L202.7 393.1H196.0Z M221.5 415.0H238.4V409.8H227.6V406.5H237.5V401.6H227.6V398.3H238.4V393.1H221.5Z M243.3 402.1H247.4L247.9 393.1H242.8Z M261.8 415.3C267.7 415.3 271.1 412.7 271.1 408.1C271.1 404.6 268.9 402.7 263.9 401.8L262.2 401.5C259.9 401.0 258.9 400.5 258.9 399.5C258.9 398.4 260.1 397.6 261.8 397.6C263.6 397.6 264.9 398.5 265.0 400.0H270.6C270.5 395.5 267.2 392.8 261.7 392.8C256.4 392.8 252.8 395.6 252.8 399.7C252.8 403.0 255.1 405.2 259.3 406.0L261.5 406.4C263.9 406.9 265.0 407.5 265.0 408.6C265.0 409.7 263.8 410.5 261.8 410.5C259.6 410.5 258.2 409.5 258.1 407.7H252.4C252.4 412.6 255.7 415.3 261.8 415.3Z M282.3 415.0H288.4V408.4H291.5C296.8 408.4 300.0 405.4 300.0 400.7C300.0 396.1 296.8 393.1 291.5 393.1H282.3ZM288.4 403.3V398.1H291.1C292.9 398.1 294.1 399.1 294.1 400.7C294.1 402.3 292.9 403.3 291.1 403.3Z M309.6 393.1H303.5V415.0H309.6Z M323.8 415.3C329.4 415.3 333.3 411.9 333.9 406.7H327.8C327.4 408.8 326.0 410.0 323.9 410.0C320.9 410.0 319.3 407.9 319.3 404.1C319.3 400.2 320.9 398.0 323.9 398.0C326.0 398.0 327.4 399.3 327.8 401.4H333.9C333.3 396.2 329.4 392.8 323.8 392.8C317.8 392.8 313.2 396.8 313.2 404.1C313.2 411.3 317.8 415.3 323.8 415.3Z M337.4 415.0H343.5V409.9L346.2 406.6L351.2 415.0H358.1L350.1 402.5L357.7 393.1H350.7L346.3 398.7C345.4 400.0 344.5 401.2 343.5 402.4V393.1H337.4Z M370.2 415.3C376.2 415.3 379.6 412.7 379.6 408.1C379.6 404.6 377.4 402.7 372.4 401.8L370.7 401.5C368.4 401.0 367.4 400.5 367.4 399.5C367.4 398.4 368.6 397.6 370.3 397.6C372.1 397.6 373.4 398.5 373.5 400.0H379.1C378.9 395.5 375.7 392.8 370.2 392.8C364.9 392.8 361.3 395.6 361.3 399.7C361.3 403.0 363.6 405.2 367.8 406.0L370.0 406.4C372.4 406.9 373.5 407.5 373.5 408.6C373.5 409.7 372.2 410.5 370.3 410.5C368.1 410.5 366.7 409.5 366.6 407.7H360.9C360.9 412.6 364.2 415.3 370.2 415.3Z" fill="#0b2a1d"/></svg>'''
MANIFEST = {"name": "Steve's Picks", "short_name": "Steve's Picks", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#1b1f24", "theme_color": "#1b1f24",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/logo.svg", "sizes": "any", "type": "image/svg+xml"}]}
SW_JS = "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>self.clients.claim());self.addEventListener('fetch',e=>{});"
ICONS = {"icon-192.png", "icon-512.png", "apple-touch-icon.png"}
ICON_DIR = os.path.join(DATA_DIR, "icons")

def icon_bytes(name):
    try:
        with open(os.path.join(ICON_DIR, name), "rb") as f:
            return f.read()
    except Exception:
        return None

def save_icon(name, data):
    """The app draws its icons from the logo in the browser the first time it opens and sends them here to keep."""
    if name not in ICONS or not data.startswith(b"\x89PNG") or len(data) > 400000 or icon_bytes(name):
        return False
    os.makedirs(ICON_DIR, exist_ok=True)
    with open(os.path.join(ICON_DIR, name), "wb") as f:
        f.write(data)
    return True

# ---------------------------------------------------------------- web server
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        return not APP_PIN or self.headers.get("X-Pin", "") == APP_PIN

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/api/") or u.path == "/":
            LAST_VISIT["t"] = time.time()
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self.send(200, STATUS)
            if u.path == "/api/config":
                return self.send(200, {"pin": bool(APP_PIN), "mock": MOCK})
            if u.path == "/api/games":
                return self.send(200, games(q.get("week")))
            m = re.match(r"^/api/game/(\d+)$", u.path)
            if m:
                return self.send(200, game_detail(m.group(1)))
            if u.path == "/api/recs":
                return self.send(200, recommendations())
            if u.path == "/api/bets":
                safe(settle_open, None)
                st = load_store()
                return self.send(200, {"bank": st["bank"], "bets": st["bets"]})
            if u.path == "/api/record":
                return self.send(200, record())
            if u.path == "/logo.svg":
                return self.send(200, LOGO_SVG.encode(), "image/svg+xml")
            if u.path == "/manifest.webmanifest":
                return self.send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
            if u.path == "/sw.js":
                return self.send(200, SW_JS.encode(), "text/javascript")
            if u.path.lstrip("/") in ICONS:
                b = icon_bytes(u.path.lstrip("/"))
                return self.send(200, b, "image/png") if b else self.send(404, {"error": "not made yet"})
            if u.path == "/health":
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 500000)) if n else b""
        if u.path.startswith("/api/icon/"):
            return self.send(200, {"saved": save_icon(u.path.rsplit("/", 1)[-1], raw)})
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if u.path == "/api/check":
            try:
                return self.send(200, check_slip(body.get("legs") or []))
            except Exception as e:
                return self.send(200, {"verdict": "ok", "warnings": [], "error": str(e)})
        if not self.authed():
            return self.send(401, {"error": "Wrong PIN"})
        try:
            if u.path == "/api/bets":
                return self.send(200, place_bet(body))
            m = re.match(r"^/api/bets/(\w+)/sell$", u.path)
            if m:
                return self.send(200, sell_bet(m.group(1)))
            if u.path == "/api/check":
                return self.send(200, check_slip(body.get("legs") or []))
            if u.path == "/api/reset":
                amt = float(body.get("amount") or START_BANK)
                with _store_lock:
                    save_store({"bank": amt, "start": amt, "bets": []})
                return self.send(200, {"ok": True})
            if u.path == "/api/addfunds":
                amt = float(body.get("amount") or 0)
                if amt <= 0 or amt > 1000000:
                    raise ValueError("Pick an amount")
                with _store_lock:
                    st = load_store()
                    st["bank"] = round(st["bank"] + amt, 2)
                    st["start"] = round(st["start"] + amt, 2)  # added money isn't counted as profit
                    st["deposits"] = round(st.get("deposits", 0) + amt, 2)
                    save_store(st)
                return self.send(200, {"ok": True, "bank": st["bank"]})
            if u.path == "/api/pin":
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            self.send(500, {"error": str(e)})

if __name__ == "__main__":
    threading.Thread(target=settler, daemon=True).start()
    threading.Thread(target=kalshi_loop, daemon=True).start()
    print("Steve's Picks running on http://localhost:%d%s" % (PORT, "  (MOCK data)" if MOCK else ""), flush=True)
    print("Checking ESPN and Kalshi... status lines will appear below.", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
