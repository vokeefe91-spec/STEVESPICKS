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
START_BANK = float(os.environ.get("START_BANK", "1000"))
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
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
    with _cache_lock:
        _cache[url] = (now, data)
    return data

def safe(fn, default):
    try:
        return fn()
    except Exception as e:
        print("fetch error:", e)
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
        d = get_json(KALSHI + "/markets?" + urllib.parse.urlencode(q), 15)
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
            "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na, "last": last,
            "chance": round(chance * 100) if chance is not None else None,
            "volume": money(m.get("volume_fp")) or 0, "close": m.get("close_time"),
            "tradable": bool((ya and 0 < ya < 1) or (na and 0 < na < 1)),
            "status": m.get("status"), "result": m.get("result") or ""}

def all_nfl_markets():
    """Returns {(date, teams): [market, ...]} for every open NFL market on Kalshi."""
    series = nfl_series()
    groups = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda s: (s, safe(lambda: series_markets(s["ticker"]), [])), series))
    for s, mkts in results:
        for m in mkts:
            k = game_key(m.get("event_ticker"))
            if not k:
                continue
            cm = clean_market(m, s["title"])
            cm["series_ticker"] = s["ticker"]
            if s["ticker"] == "KXNFLGAME":
                cm["series"] = "Winner"
            groups.setdefault(k, []).append(cm)
    return groups

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
def espn_scoreboard(week=None):
    url = ESPN + "/scoreboard"
    if week:
        url += "?" + urllib.parse.urlencode({"week": week, "seasontype": 2})
    return get_json(url, 10)

def team_info(c):
    t = c.get("team", {})
    rec = (c.get("records") or [{}])[0].get("summary", "")
    return {"abbr": t.get("abbreviation"), "name": t.get("displayName"), "short": t.get("shortDisplayName") or t.get("name"),
            "logo": t.get("logo"), "color": t.get("color"), "score": c.get("score"), "record": rec,
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
    sb = safe(lambda: espn_scoreboard(week), {})
    evs = [parse_event(e) for e in sb.get("events", [])]
    groups = safe(all_nfl_markets, {})
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
    return {"week": wk, "season": (sb.get("season") or {}).get("year"), "games": evs, "updated": time.time()}

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
    groups = safe(all_nfl_markets, {})
    k = match_key(groups, g["away"].get("abbr", ""), g["home"].get("abbr", ""))
    mk = order_markets(groups.get(k, [])) if k else []
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
            return json.load(f)
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
        safe(settle_open, None)
        time.sleep(60)

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
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
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
            if u.path == "/health":
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if not self.authed():
            return self.send(401, {"error": "Wrong PIN"})
        try:
            if u.path == "/api/bets":
                return self.send(200, place_bet(body))
            m = re.match(r"^/api/bets/(\w+)/sell$", u.path)
            if m:
                return self.send(200, sell_bet(m.group(1)))
            if u.path == "/api/reset":
                with _store_lock:
                    save_store({"bank": START_BANK, "start": START_BANK, "bets": []})
                return self.send(200, {"ok": True})
            if u.path == "/api/pin":
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            self.send(500, {"error": str(e)})

if __name__ == "__main__":
    threading.Thread(target=settler, daemon=True).start()
    print("Steve's Picks running on http://localhost:%d%s" % (PORT, "  (MOCK data)" if MOCK else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
