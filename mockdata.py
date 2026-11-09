"""Sample data used only when MOCK=1, so the app can be tried without internet."""
import re, time, random
from urllib.parse import urlparse, parse_qs

TEAMS = {"BUF": ("Buffalo Bills", "Bills", "00338D"), "MIN": ("Minnesota Vikings", "Vikings", "4F2683"),
         "NYG": ("New York Giants", "Giants", "0B2265"), "WSH": ("Washington Commanders", "Commanders", "5A1414"),
         "DET": ("Detroit Lions", "Lions", "0076B6"), "NE": ("New England Patriots", "Patriots", "002244"),
         "KC": ("Kansas City Chiefs", "Chiefs", "E31837"), "ATL": ("Atlanta Falcons", "Falcons", "A71930")}
# (espn id, away, home, kalshi away code, kalshi home code, date, state)
GAMES = [("401", "BUF", "MIN", "BUF", "MIN", "2026-11-10T01:15Z", "in"),
         ("402", "WSH", "NYG", "WAS", "NYG", "2026-11-13T01:15Z", "pre"),
         ("403", "NE", "DET", "NE", "DET", "2026-11-15T14:30Z", "pre"),
         ("404", "KC", "ATL", "KC", "ATL", "2026-11-15T18:00Z", "pre")]
PROB = {"401": 0.62, "402": 0.465, "403": 0.418, "404": 0.623}
_t0 = time.time()

def comp(g):
    eid, a, h, _, _, date, state = g
    mins = int((time.time() - _t0) / 20)
    sa, sh = (17 + mins % 7, 13 + mins % 5) if state == "in" else ("0", "0")
    def c(abbr, home, score):
        n, s, col = TEAMS[abbr]
        return {"homeAway": "home" if home else "away", "score": str(score),
                "team": {"abbreviation": abbr, "displayName": n, "shortDisplayName": s, "color": col,
                         "logo": ""}, "records": [{"summary": "6-3"}],
                "linescores": [{"displayValue": "7"}, {"displayValue": "10"}, {"displayValue": "0"}] if state == "in" else []}
    st = {"in": {"state": "in", "shortDetail": "3rd 8:42"}, "pre": {"state": "pre", "shortDetail": "Thu 8:15 PM"}}[state]
    return {"competitors": [c(h, True, sh), c(a, False, sa)], "status": {"type": st, "displayClock": "8:42", "period": 3},
            "venue": {"fullName": "U.S. Bank Stadium"}, "broadcasts": [{"names": ["ESPN"]}], "date": date,
            "situation": {"shortDownDistanceText": "2nd & 7 at MIN 34", "possession": "BUF", "isRedZone": False,
                          "lastPlay": {"text": "J.Allen scrambles right for 9 yards"}} if state == "in" else None}

def scoreboard():
    return {"week": {"number": 10}, "season": {"year": 2026},
            "events": [{"id": g[0], "date": g[5], "name": "%s at %s" % (TEAMS[g[1]][0], TEAMS[g[2]][0]),
                        "competitions": [comp(g)]} for g in GAMES]}

def summary(eid):
    g = next(x for x in GAMES if x[0] == eid)
    plays = [{"text": t, "clock": {"displayValue": c}, "period": {"number": 3}, "scoringPlay": sc,
              "start": {"downDistanceText": d}} for t, c, sc, d in [
        ("J.Cook up the middle for 4 yards", "10:12", False, "1st & 10 at BUF 25"),
        ("J.Allen pass deep right to K.Shakir for 31 yards", "9:40", False, "2nd & 6 at BUF 29"),
        ("J.Allen scrambles right for 9 yards", "8:42", False, "1st & 10 at MIN 43")]]
    return {"header": {"competitions": [comp(g)]},
            "drives": {"previous": [{"id": "1", "team": {"abbreviation": "MIN"}, "description": "8 plays, 75 yards, 4:10",
                                     "displayResult": "Touchdown", "plays": plays[:1]}],
                       "current": {"id": "2", "team": {"abbreviation": "BUF"}, "description": "3 plays, 44 yards",
                                   "displayResult": "", "plays": plays}},
            "leaders": [{"team": {"abbreviation": "BUF"}, "leaders": [
                {"displayName": "Passing Yards", "leaders": [{"displayValue": "16/24, 211 YDS, 2 TD", "athlete": {"displayName": "Josh Allen"}}]},
                {"displayName": "Rushing Yards", "leaders": [{"displayValue": "6 CAR, 38 YDS", "athlete": {"displayName": "Josh Allen"}}]}]}],
            "scoringPlays": [{"text": "Josh Allen 5 yd run", "team": {"abbreviation": "BUF"}, "period": {"number": 1},
                              "clock": {"displayValue": "4:12"}, "awayScore": 7, "homeScore": 0}]}

def mk(ticker, event, yes, label, wobble=True):
    p = yes + (random.uniform(-0.01, 0.01) if wobble else 0)
    p = min(max(p, 0.02), 0.98)
    return {"ticker": ticker, "event_ticker": event, "status": "active", "result": "",
            "yes_sub_title": label, "no_sub_title": label,
            "yes_bid_dollars": "%.3f" % (p - 0.01), "yes_ask_dollars": "%.3f" % (p + 0.01),
            "no_bid_dollars": "%.3f" % (1 - p - 0.01), "no_ask_dollars": "%.3f" % (1 - p + 0.01),
            "last_price_dollars": "%.3f" % p, "volume_fp": "152340.00", "close_time": "2026-11-10T05:00:00Z"}

def series_markets(series):
    out = []
    for eid, a, h, ka, kh, date, state in GAMES:
        key = "26NOV%02d%s%s" % (int(date[8:10]), ka, kh)
        ev = "%s-%s" % (series, key)
        p = PROB[eid]
        an, hn = TEAMS[a][1], TEAMS[h][1]
        if series == "KXNFLGAME":
            out += [mk(ev + "-" + ka, ev, p, an), mk(ev + "-" + kh, ev, 1 - p, hn)]
        elif series == "KXNFLSPREAD":
            out += [mk(ev + "-%s3" % ka, ev, p - 0.12, "%s wins by over 2.5 points" % an),
                    mk(ev + "-%s7" % ka, ev, p - 0.28, "%s wins by over 6.5 points" % an)]
        elif series == "KXNFLTOTAL":
            out += [mk(ev + "-44", ev, 0.55, "Over 43.5 points scored"), mk(ev + "-48", ev, 0.41, "Over 47.5 points scored")]
        elif series == "KXNFLRSHYDS" and a == "BUF":
            out += [mk(ev + "-JALLEN25", ev, 0.64, "Josh Allen: 25+ rushing yards"),
                    mk(ev + "-JALLEN50", ev, 0.22, "Josh Allen: 50+ rushing yards")]
        elif series == "KXNFLANYTD" and a == "BUF":
            out += [mk(ev + "-JALLEN", ev, 0.54, "Josh Allen"), mk(ev + "-JCOOK", ev, 0.47, "James Cook"),
                    mk(ev + "-JJEFF", ev, 0.38, "Justin Jefferson")]
    return out

def fetch(url):
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.path.endswith("/scoreboard"):
        return scoreboard()
    if u.path.endswith("/summary"):
        return summary(q["event"])
    if u.path.endswith("/series"):
        return {"series": [{"ticker": "KXNFLGAME", "title": "Pro Football Game"},
                           {"ticker": "KXNFLSPREAD", "title": "Spread"}, {"ticker": "KXNFLTOTAL", "title": "Total Points"},
                           {"ticker": "KXNFLRSHYDS", "title": "Rushing Yards"}, {"ticker": "KXNFLANYTD", "title": "Anytime Touchdown"}]}
    if u.path.endswith("/markets"):
        if "series_ticker" in q:
            return {"markets": series_markets(q["series_ticker"]), "cursor": ""}
        if "tickers" in q:
            want = set(q["tickers"].split(","))
            allm = [m for s in ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLRSHYDS", "KXNFLANYTD") for m in series_markets(s)]
            return {"markets": [m for m in allm if m["ticker"] in want]}
    return {}
