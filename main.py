"""
Match Bot — Discord bet tracker (soccer, UFC/MMA, cricket, basketball, esports, boxing, ...)

How it works
- Paste bet history text, drop a .txt, or drop a bet slip screenshot.
- Each bet (each "ID: ...") is read separately: legs, market, odds, stake, Win/Loss.
- You get a preview with real results, then tap Save.
  Finished bets go to your stats. Running bets are tracked live with alerts.
- Bets already saved (same ID) are skipped automatically.
- State is saved to the GitHub repo, so restarts do not lose anything.
"""

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import signal
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import aiohttp
import discord
from discord.ext import commands, tasks

# =====================================================================
# CONFIG
# =====================================================================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")
GH_REPO = os.getenv("GITHUB_REPOSITORY", "")
TZ = timezone(timedelta(hours=float(os.getenv("TZ_OFFSET_HOURS", "6"))))
CUR = os.getenv("CURRENCY", "৳")
LEDGER_FILE = "ledger.json"
ACTIVE_FILE = "active.json"
POLL_SECONDS = 45

SPORT_ICON = {
    "soccer": "⚽", "mma": "🥊", "boxing": "🥊", "cricket": "🏏", "basketball": "🏀",
    "esports": "🎮", "tennis": "🎾", "american_football": "🏈", "hockey": "🏒",
    "baseball": "⚾", "other": "🎯",
}
SPORT_NAME = {
    "soccer": "Soccer", "mma": "UFC/MMA", "boxing": "Boxing", "cricket": "Cricket",
    "basketball": "Basketball", "esports": "Esports", "tennis": "Tennis",
    "american_football": "NFL", "hockey": "Hockey", "baseball": "Baseball", "other": "Other",
}
# Live score feeds (ESPN). Sports not listed here are settled from your slip or manually.
SPORT_FEEDS = {
    "soccer": [("soccer", "scorepanel")],
    "mma": [("mma/ufc", "scoreboard")],
    "cricket": [("cricket", "scorepanel")],
    "basketball": [("basketball/nba", "scoreboard")],
    "american_football": [("football/nfl", "scoreboard")],
    "hockey": [("hockey/nhl", "scoreboard")],
    "baseball": [("baseball/mlb", "scoreboard")],
}


def now_local():
    return datetime.now(TZ)


def today_str():
    return now_local().strftime("%Y-%m-%d")


def money(x, sign=False):
    x = float(x or 0)
    s = f"{abs(x):,.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    if not sign:
        return ("-" if x < 0 else "") + CUR + s
    if abs(x) < 0.005:
        return CUR + "0"
    return ("+" if x > 0 else "−") + CUR + s


def clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


# =====================================================================
# STATE (local files + synced to GitHub repo)
# =====================================================================
class Store:
    def __init__(self):
        self.ledger = {"version": 2, "bets": []}
        self.active = []
        self.pending = {}
        self.dirty = False
        self.shas = {}
        self.lock = asyncio.Lock()

    def _active_doc(self):
        return {"active": self.active, "pending": self.pending}

    def _apply(self, name, doc):
        if name == LEDGER_FILE:
            if isinstance(doc, dict) and doc.get("version") == 2:
                self.ledger = doc
            else:
                self.ledger = {"version": 2, "bets": []}
        else:
            if isinstance(doc, dict):
                self.active = doc.get("active", []) or []
                self.pending = doc.get("pending", {}) or {}

    async def load(self, session):
        for name in (LEDGER_FILE, ACTIVE_FILE):
            doc = None
            if GH_TOKEN and GH_REPO:
                try:
                    url = f"https://api.github.com/repos/{GH_REPO}/contents/{name}"
                    async with session.get(url, headers=self._gh_headers()) as r:
                        if r.status == 200:
                            js = await r.json()
                            self.shas[name] = js.get("sha")
                            doc = json.loads(base64.b64decode(js["content"]).decode("utf-8"))
                except Exception as e:
                    print(f"[store] remote load {name} failed: {e}", flush=True)
            if doc is None and os.path.exists(name):
                try:
                    with open(name, "r", encoding="utf-8") as f:
                        doc = json.load(f)
                except Exception:
                    doc = None
            if doc is not None:
                self._apply(name, doc)
        cutoff = time.time() - 86400
        self.pending = {k: v for k, v in self.pending.items() if v.get("created", 0) > cutoff}
        print(f"[store] loaded {len(self.ledger['bets'])} saved bets, {len(self.active)} active", flush=True)

    def save(self):
        for name, doc in ((LEDGER_FILE, self.ledger), (ACTIVE_FILE, self._active_doc())):
            try:
                with open(name, "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=1, ensure_ascii=False)
            except Exception as e:
                print(f"[store] local save failed: {e}", flush=True)
        self.dirty = True

    def _gh_headers(self):
        return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                "User-Agent": "match-bot"}

    async def flush(self, session):
        if not self.dirty or not (GH_TOKEN and GH_REPO):
            return
        async with self.lock:
            self.dirty = False
            for name, doc in ((LEDGER_FILE, self.ledger), (ACTIVE_FILE, self._active_doc())):
                body = json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8")
                url = f"https://api.github.com/repos/{GH_REPO}/contents/{name}"
                for attempt in range(2):
                    try:
                        if name not in self.shas:
                            async with session.get(url, headers=self._gh_headers()) as r:
                                self.shas[name] = (await r.json()).get("sha") if r.status == 200 else None
                        payload = {"message": f"Save {name} [skip ci]",
                                   "content": base64.b64encode(body).decode()}
                        if self.shas.get(name):
                            payload["sha"] = self.shas[name]
                        async with session.put(url, headers=self._gh_headers(), json=payload) as r:
                            if r.status in (200, 201):
                                self.shas[name] = (await r.json())["content"]["sha"]
                                break
                            self.shas.pop(name, None)
                            if attempt == 1:
                                print(f"[store] flush {name} failed {r.status}", flush=True)
                                self.dirty = True
                    except Exception as e:
                        print(f"[store] flush {name} error: {e}", flush=True)
                        self.dirty = True
                        break


STORE = Store()

# =====================================================================
# NAME MATCHING
# =====================================================================
STOP = {"fc", "cf", "afc", "sc", "ac", "cd", "fk", "sk", "club", "the", "de", "ssc", "sv", "bk",
        "cfc", "and", "calcio", "if", "jk"}
ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "spurs": "tottenham hotspur", "wolves": "wolverhampton wanderers", "psg": "paris saint germain",
    "inter": "internazionale", "inter milan": "internazionale", "athletic bilbao": "athletic club",
    "nottm forest": "nottingham forest", "bayern": "bayern munich", "barca": "barcelona",
    "sporting lisbon": "sporting cp", "leverkusen": "bayer leverkusen", "atl madrid": "atletico madrid",
    "newcastle": "newcastle united", "west ham": "west ham united", "brighton": "brighton and hove albion",
}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)


def toks(s):
    n = norm(s).split()
    t = [x for x in n if x not in STOP]
    return t or n


def tok_eq(a, b):
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return len(a) >= 5 and len(b) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.85


def name_sim(a, b):
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    ma = sum(1 for x in ta if any(tok_eq(x, y) for y in tb))
    mb = sum(1 for y in tb if any(tok_eq(x, y) for x in ta))
    small = ta if len(ta) <= len(tb) else tb
    if min(ma, mb) >= len(small):
        return 0.9 if (len(small) >= 2 or len(small[0]) >= 4) else 0.6
    return 0.8 * max(ma, mb) / max(len(ta), len(tb))


def best_sim(name, aliases, abbr=""):
    if abbr and norm(name) == abbr.lower():
        return 0.95
    return max([name_sim(name, a) for a in aliases if a] or [0.0])


def raw_toks(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    n = [x for x in s.split() if x not in STOP]
    return n or s.split()


def team_in_text(team, text):
    """How many of the team's tokens appear in text. Checks both raw and alias tokens."""
    best_m, best_n = 0, 1
    for tt, xt in ((raw_toks(team), raw_toks(text)), (toks(team), norm(text).split())):
        if not tt:
            continue
        m = sum(1 for x in tt if any(tok_eq(x, y) for y in xt))
        if m == len(tt) and len(tt) >= best_m:
            return m, len(tt)
        if m / len(tt) > best_m / best_n:
            best_m, best_n = m, len(tt)
    return best_m, best_n


# =====================================================================
# LIVE SCORES (ESPN)
# =====================================================================
FEED_CACHE = {}


def _int(v):
    try:
        return int(float(v))
    except Exception:
        return None


def _parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _fixture(sport, ev, comp):
    st = comp.get("status") or ev.get("status") or {}
    stype = st.get("type", {}) or {}
    comps = comp.get("competitors", []) or []
    if len(comps) < 2:
        return None
    if sport == "mma":
        comps = sorted(comps, key=lambda c: c.get("order", 9))
    else:
        comps = sorted(comps, key=lambda c: 0 if c.get("homeAway") == "home" else 1)
    names, disp, score, stext, winner, abbr = [], [], [], [], [], []
    for c in comps[:2]:
        t = c.get("team") or c.get("athlete") or {}
        al = [t.get("displayName"), t.get("shortDisplayName"), t.get("name"), t.get("fullName"),
              t.get("location"), t.get("shortName")]
        names.append([a for a in al if a])
        disp.append(t.get("shortDisplayName") or t.get("displayName") or "?")
        abbr.append(t.get("abbreviation") or "")
        raw = c.get("score")
        if isinstance(raw, dict):
            raw = raw.get("displayValue") or raw.get("value")
        score.append(_int(raw))
        stext.append(str(raw) if raw not in (None, "") else "")
        winner.append(bool(c.get("winner")))
    clock = st.get("displayClock") or ""
    m = re.match(r"\s*(\d+)", clock)
    return {
        "sport": sport,
        "id": f"{sport}:{comp.get('id') or ev.get('id')}",
        "date": _parse_dt(comp.get("date") or ev.get("date")),
        "names": names, "disp": disp, "abbr": abbr,
        "score": score, "stext": stext, "winner": winner,
        "state": stype.get("state", "pre"),
        "completed": bool(stype.get("completed")),
        "desc": stype.get("description", ""),
        "short": stype.get("shortDetail") or stype.get("detail") or "",
        "clock": clock, "minute": int(m.group(1)) if m else 0,
        "period": _int(st.get("period")) or 0,
        "clock_secs": float(st.get("clock") or 0),
    }


async def fetch_feed(session, sport, path, kind, day):
    key = (path, day)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    ttl = 35 if abs((datetime.strptime(day, "%Y%m%d") - datetime.strptime(today, "%Y%m%d")).days) <= 1 else 3600
    hit = FEED_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/{kind}?dates={day}"
    out = []
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200:
                return hit[1] if hit else []
            data = await r.json(content_type=None)
        events = []
        if kind == "scorepanel":
            for g in data.get("scores", []) or []:
                events.extend(g.get("events", []) or [])
        else:
            events = data.get("events", []) or []
        for ev in events:
            for comp in ev.get("competitions", []) or []:
                fx = _fixture(sport, ev, comp)
                if fx:
                    out.append(fx)
        FEED_CACHE[key] = (time.time(), out)
    except Exception as e:
        print(f"[feed] {url} error: {e}", flush=True)
        return hit[1] if hit else []
    return out


def leg_days(leg):
    days = set()
    base = None
    if leg.get("date"):
        try:
            base = datetime.strptime(leg["date"], "%Y-%m-%d")
        except Exception:
            base = None
    if base is None:
        base = datetime.now(timezone.utc).replace(tzinfo=None)
    for d in (-1, 0, 1):
        days.add((base + timedelta(days=d)).strftime("%Y%m%d"))
    return days


async def fetch_for_legs(session, legs):
    need = {}
    for lg in legs:
        sp = lg.get("sport")
        if sp in SPORT_FEEDS and lg.get("a") and lg.get("b"):
            need.setdefault(sp, set()).update(leg_days(lg))
    tasks_ = []
    for sp, days in need.items():
        for path, kind in SPORT_FEEDS[sp]:
            for d in sorted(days)[:9]:
                tasks_.append(fetch_feed(session, sp, path, kind, d))
    res = await asyncio.gather(*tasks_) if tasks_ else []
    seen, fixtures = set(), []
    for lst in res:
        for fx in lst:
            if fx["id"] not in seen:
                seen.add(fx["id"])
                fixtures.append(fx)
    return fixtures


def find_fixture(leg, fixtures):
    best, best_score, best_flip = None, 0.0, False
    ldate = None
    if leg.get("date"):
        try:
            ldate = datetime.strptime(leg["date"], "%Y-%m-%d").date()
        except Exception:
            ldate = None
    for fx in fixtures:
        if fx["sport"] != leg.get("sport"):
            continue
        if ldate and fx["date"] and abs((fx["date"].date() - ldate).days) > 1:
            continue
        a0 = best_sim(leg["a"], fx["names"][0], fx["abbr"][0])
        b1 = best_sim(leg["b"], fx["names"][1], fx["abbr"][1])
        a1 = best_sim(leg["a"], fx["names"][1], fx["abbr"][1])
        b0 = best_sim(leg["b"], fx["names"][0], fx["abbr"][0])
        straight, flipped = min(a0, b1), min(a1, b0)
        sc, fl = (straight, False) if straight >= flipped else (flipped, True)
        if sc < 0.6:
            continue
        if fx["date"]:
            ref = datetime.combine(ldate, datetime.min.time(), tzinfo=timezone.utc) if ldate else datetime.now(timezone.utc)
            sc -= min(abs((fx["date"] - ref).total_seconds()) / 86400.0, 3) * 0.01
        if sc > best_score:
            best, best_score, best_flip = fx, sc, fl
    return best, best_flip


# =====================================================================
# BET PARSER
# =====================================================================
STATUS_WORDS = {
    "win": "won", "won": "won", "half win": "won", "half won": "won",
    "lose": "lost", "lost": "lost", "loss": "lost", "half lose": "lost", "half lost": "lost",
    "draw": "void", "void": "void", "refund": "void", "refunded": "void", "cancelled": "void",
    "canceled": "void", "rejected": "void", "push": "void",
    "running": "running", "pending": "running", "unsettled": "running", "open": "running",
}
ID_RE = re.compile(r"\bID\s*[:#]?\s*(\d{6,})", re.I)
DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?")
SEL_RE = re.compile(r"^(.*?)\s*@\s*([0-9]+(?:[.,][0-9]+)?)")
VS_RE = re.compile(r"\s*-\s*vs\.?\s*-\s*|\s+vs\.?\s+|\s+v\s+", re.I)
NUM = r"([+-]?\d[\d,]*(?:\.\d+)?)"
FOOT = {
    "odds": re.compile(r"^\s*(?:total\s+)?odds\s*:?\s*" + NUM, re.I),
    "stake": re.compile(r"^\s*(?:stake|bet amount|amount)\s*:?\s*" + NUM, re.I),
    "risk": re.compile(r"^\s*(?:total\s+)?risk\s*:?\s*" + NUM, re.I),
    "wl": re.compile(r"^\s*win\s*/\s*loss\s*:?\s*" + NUM, re.I),
    "potential": re.compile(r"^\s*(?:potential\s+(?:winnings?|return|payout|win)|to\s+win|est\.?\s*return)\s*:?\s*" + NUM, re.I),
}
FOOT_WORDS = ("odds", "stake", "risk", "win / loss", "win/loss", "potential", "total risk", "return")
BRACKET_FT = re.compile(r"\[(?:match|ft|full\s*time|result)\s*:\s*(\d+)\s*[-:]\s*(\d+)\]", re.I)
BRACKET_HT = re.compile(r"\[(?:1st\s*h(?:alf)?|ht)\s*:\s*(\d+)\s*[-:]\s*(\d+)\]", re.I)
LEAGUE_HINTS = ("league", "liga", "serie", "bundesliga", "ligue", "cup", "copa", "championship", "ufc", "pfl",
                "bellator", "one championship", "nba", "euroleague", "ipl", "bbl", "psl", "bpl", "t20", "odi",
                "test match", "atp", "wta", "nfl", "nhl", "mlb", "uefa", "fifa", "eredivisie", "mls", "saudi",
                "premier", "division", "conference", "trophy", "series", "world", "open")
NOISE = re.compile(r"^(\d{1,2}:\d{2}|my bets|all dates|all sports|all leagues|hide filter.*|unsettled|settled|"
                   r"sportsbook.*|e my bets|total risk|potential winnings|.*\.com|[\d,]+\.\d{2}\s*[a-z৳$]*)$", re.I)

SPORT_KEYS = [
    ("mma", ("mixed martial arts", "mma", "ufc", "pfl", "bellator")),
    ("boxing", ("boxing",)),
    ("esports", ("e-sports", "esports", "e sports", "cs2", "csgo", "counter-strike", "dota", "valorant",
                 "league of legends", "mobile legends", "lol ")),
    ("cricket", ("cricket", "ipl", "t20", "odi", "bbl", "psl", "bpl", "test match")),
    ("basketball", ("basketball", "nba", "euroleague")),
    ("tennis", ("tennis", "atp", "wta")),
    ("american_football", ("american football", "nfl")),
    ("hockey", ("ice hockey", "hockey", "nhl")),
    ("baseball", ("baseball", "mlb")),
    ("soccer", ("soccer", "football", "premier league", "la liga", "serie a", "bundesliga", "ligue 1")),
]


def detect_sport(*texts):
    t = " " + " ".join(x for x in texts if x).lower() + " "
    for sp, keys in SPORT_KEYS:
        if any(k in t for k in keys):
            return sp
    return None


def fnum(s):
    try:
        return float(str(s).replace(",", ""))
    except Exception:
        return None


def is_league_line(line):
    l = line.lower()
    if " - " in line and not VS_RE.search(line):
        return True
    return len(line) <= 40 and any(h in l for h in LEAGUE_HINTS) and "@" not in line


def split_vs(line):
    parts = VS_RE.split(line, maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(" -:|"), parts[1].strip()
    if not a or not b:
        return None
    return a, b


PICK_CUT = re.compile(r"\s*(?:[:|,]|\s-\s)\s*|\s+(?=(?:over|under|o|u|win|to win|draw|btts|gg|ng|yes|no|ah|handicap|dnb|[+-]?\d)\b)", re.I)


def cut_team_pick(b):
    """'Chelsea over 2.5' -> ('Chelsea', 'over 2.5')."""
    m = PICK_CUT.search(b)
    if not m or m.start() == 0:
        return b.strip(), ""
    return b[: m.start()].strip(), b[m.end():].strip() if m.group(0).strip() in (":", "|", ",", "-") else b[m.start():].strip()


def new_leg(pick="", odds=None, market=""):
    return {"pick": pick.strip(), "odds": odds, "market": market or "", "details": [], "league": "",
            "a": "", "b": "", "date": "", "ft": None, "ht": None}


def parse_block(text):
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.replace("\r", "").split("\n")]
    lines = [l for l in lines if l]
    bet = {"id": None, "placed": None, "kind": None, "status": None, "stake": None, "odds": None,
           "wl": None, "potential": None, "risk": None, "legs": []}
    cur = None
    last_plain = ""
    money_lines = []
    typed_stake = None

    for line in lines:
        low = line.lower()
        m = ID_RE.search(line)
        if m:
            bet["id"] = m.group(1)
            if "parlay" in low or "combo" in low or "multi" in low:
                bet["kind"] = "parlay"
            dm = DT_RE.search(line)
            if dm:
                bet["placed"] = dm.group(1)
            continue
        if low in STATUS_WORDS:
            if not (low in ("unsettled", "settled") and bet["status"]):
                bet["status"] = STATUS_WORDS[low]
            continue
        hit = False
        for k, rx in FOOT.items():
            fm = rx.match(line)
            if fm:
                bet[k] = fnum(fm.group(1))
                hit = True
                break
        if hit:
            continue
        if re.fullmatch(r"[\d,]+\.\d{2}\s*(bdt|inr|usd|৳|tk)?", low):
            money_lines.append(fnum(re.sub(r"[^\d.,]", "", line)))
            continue
        tm = re.search(r"(?:stake|bet|risk)\s*[:=]?\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:tk|bdt|taka|৳)\b", low)
        if tm and not any(rx.match(line) for rx in FOOT.values()):
            typed_stake = fnum(tm.group(1) or tm.group(2))
            line = (line[:tm.start()] + " " + line[tm.end():]).strip()
            low = line.lower()
            if not line:
                continue
        dm = DT_RE.match(line)
        if dm and cur is not None:
            cur["date"] = cur["date"] or dm.group(1)
            f = BRACKET_FT.search(line)
            h = BRACKET_HT.search(line)
            if f:
                cur["ft"] = [int(f.group(1)), int(f.group(2))]
            if h:
                cur["ht"] = [int(h.group(1)), int(h.group(2))]
            continue
        if dm:
            continue

        sm = SEL_RE.match(line) if "@" in line else None
        if sm:
            pick, odds = sm.group(1).strip(), fnum(sm.group(2).replace(",", "."))
            vs = split_vs(pick)
            cur = new_leg(pick, odds, last_plain)
            if vs:
                a, rest = vs
                b, p = cut_team_pick(rest)
                cur.update(a=a, b=b, pick=p or pick)
            bet["legs"].append(cur)
            last_plain = ""
            continue

        vs = split_vs(line)
        if vs:
            a, rest = vs
            b, p = cut_team_pick(rest)
            if cur is not None and not cur["a"]:
                cur["a"], cur["b"] = a, b
                if p and not cur["pick"]:
                    cur["pick"] = p
            else:
                cur = new_leg(p, None, last_plain)
                cur["a"], cur["b"] = a, b
                bet["legs"].append(cur)
            last_plain = ""
            continue

        if NOISE.match(line):
            continue
        if is_league_line(line):
            if cur is not None and not cur["league"] and not cur["a"]:
                cur["league"] = line
            elif cur is not None and cur["a"] and not cur["league"]:
                cur["league"] = line
            last_plain = ""
            continue
        # plain line
        if cur is not None and not cur["a"]:
            cur["details"].append(line)
        elif cur is not None and cur["a"] and not cur["pick"]:
            cur["pick"] = line
        else:
            last_plain = line

    legs = [lg for lg in bet["legs"] if lg["a"] and lg["b"]]
    if not legs:
        return None
    if bet["stake"] is None:
        bet["stake"] = bet["risk"] if bet["risk"] is not None else typed_stake
    if bet["potential"] is None and len(money_lines) >= 2:
        bet["potential"] = money_lines[1]
        if bet["stake"] is None:
            bet["stake"] = money_lines[0]
    for lg in legs:
        finish_leg(lg)
    bet["legs"] = legs
    if bet["odds"] is None:
        prod = 1.0
        ok = True
        for lg in legs:
            if lg["odds"]:
                prod *= lg["odds"]
            else:
                ok = False
        bet["odds"] = round(prod, 3) if ok else None
    if len(legs) > 1:
        bet["kind"] = "parlay"
    bet["kind"] = bet["kind"] or "single"
    if all(lg["mk"]["type"] == "watch" for lg in legs) and bet["stake"] is None:
        bet["kind"] = "watch"
    sports = {lg["sport"] for lg in legs}
    bet["sport"] = sports.pop() if len(sports) == 1 else "mixed"
    if bet["kind"] == "parlay":
        bet["title"] = f"{len(legs)}-Leg Parlay"
    else:
        bet["title"] = f"{legs[0]['a']} vs {legs[0]['b']}"
    if bet["id"]:
        bet["key"] = "id" + bet["id"]
    else:
        sig = "|".join(f"{norm(l['a'])}-{norm(l['b'])}-{norm(l['pick'])}" for l in legs) + f"|{bet['stake']}"
        bet["key"] = "h" + hashlib.md5(sig.encode()).hexdigest()[:12]
    return bet


def finish_leg(lg):
    lg["sport"] = detect_sport(lg["market"], lg["league"], " ".join(lg["details"])) or "soccer"
    lg["mk"] = parse_market(lg)
    lg["label"] = make_label(lg)


def parse_market(lg):
    pick = lg["pick"] or ""
    mk = (lg["market"] or "").lower()
    det = " ".join(lg["details"]).lower()
    pl = norm(pick)
    ptoks = pl.split()
    if not pick:
        return {"type": "watch"}
    side, best = None, 0.0
    for idx, team in ((0, lg["a"]), (1, lg["b"])):
        m, n = team_in_text(team, pick)
        if n and m == n and m / n > best:
            side, best = idx, m / n
    rest = pl
    if side is not None:
        tname = lg["a"] if side == 0 else lg["b"]
        tt = set(toks(tname)) | set(raw_toks(tname))
        rest = " ".join(t for t in ptoks if not any(tok_eq(t, x) for x in tt))
    nm = re.search(r"([+-]?\d+(?:\.\d+)?)", pick.replace(",", "."))
    num = float(nm.group(1)) if nm else None
    rt = rest.split()
    ou = "over" if ("over" in rt or (rt and rt[0] == "o")) else ("under" if ("under" in rt or (rt and rt[0] == "u")) else None)

    if "both teams" in mk or "btts" in mk or pl in ("gg", "ng"):
        return {"type": "btts", "yes": pl.startswith("yes") or pl == "gg"}
    if "round" in det and "start" in det:
        rn = re.search(r"round\s*(\d+)", det)
        return {"type": "starts_round", "round": int(rn.group(1)) if rn else 2, "yes": pl.startswith("yes")}
    if "distance" in det or "distance" in mk:
        return {"type": "distance", "yes": pl.startswith("yes")}
    if ou and num is not None:
        if lg["sport"] == "mma" or lg["sport"] == "boxing":
            return {"type": "rounds_total", "ou": ou, "line": abs(num)}
        if side is not None:
            return {"type": "team_total", "side": side, "ou": ou, "line": abs(num)}
        return {"type": "total", "ou": ou, "line": abs(num)}
    if "or draw" in pl or "draw or" in pl or "double chance" in mk:
        return {"type": "double_chance", "side": side}
    if side is not None and num is not None and ("handicap" in mk or "spread" in mk or "1x2" not in mk and "money" not in mk):
        return {"type": "handicap", "side": side, "line": num}
    if side is not None:
        return {"type": "winner", "side": side}
    if pl in ("draw", "x", "tie"):
        return {"type": "draw"}
    return {"type": "prop"}


def make_label(lg):
    mk = lg["mk"]
    if mk["type"] == "watch":
        return "Following match"
    base = lg["pick"]
    if mk["type"] in ("starts_round", "distance", "prop") and lg["details"]:
        return f"{lg['details'][0]}: {base}"
    if mk["type"] == "winner":
        return f"{base} to win"
    if mk["type"] == "handicap":
        return f"{base} (handicap)"
    if mk["type"] == "rounds_total":
        return f"{base} rounds"
    return base


def split_blocks(text):
    lines = text.replace("\r", "").split("\n")
    idx = [i for i, l in enumerate(lines) if ID_RE.search(l)]
    if idx:
        blocks = []
        if idx[0] > 0:
            head = "\n".join(lines[: idx[0]])
            if split_vs(head) or "@" in head:
                blocks.append(head)
        for n, i in enumerate(idx):
            j = idx[n + 1] if n + 1 < len(idx) else len(lines)
            blocks.append("\n".join(lines[i:j]))
        return blocks
    # no IDs: a new bet starts when a pick appears after a footer (stake / win-loss)
    blocks, cur, after_footer = [], [], False
    for l in lines:
        low = l.lower().strip()
        starts = ("@" in l or split_vs(l)) and not any(low.startswith(w) for w in FOOT_WORDS)
        if after_footer and starts:
            blocks.append("\n".join(cur))
            cur, after_footer = [], False
        cur.append(l)
        if any(rx.match(l) for rx in FOOT.values()):
            after_footer = True
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def parse_text(text):
    bets, seen = [], set()
    for b in split_blocks(text or ""):
        bet = parse_block(b)
        if bet and bet["key"] not in seen:
            seen.add(bet["key"])
            bets.append(bet)
    return bets


# =====================================================================
# GRADING
# =====================================================================
def asian(margin, line):
    q = round(line * 4)
    halves = [line] if q % 2 == 0 else [line - 0.25, line + 0.25]
    r = sorted(1 if margin(h) > 1e-9 else (-1 if margin(h) < -1e-9 else 0) for h in halves)
    if len(r) == 1:
        return {1: "won", 0: "push", -1: "lost"}[r[0]]
    return {(1, 1): "won", (0, 1): "half_won", (0, 0): "push", (-1, 0): "half_lost", (-1, -1): "lost"}.get(tuple(r), "push")


def unit(sport, n):
    u = {"soccer": "goal", "hockey": "goal", "basketball": "pt", "cricket": "run",
         "american_football": "pt", "baseball": "run"}.get(sport, "pt")
    return f"{n} {u}" + ("" if n == 1 or u == "pt" else "s")


def grade_scores(mk, sport, sa, sb, final, wa=False, wb=False):
    """Returns (status, note). status: won/lost/push/half_won/half_lost/open."""
    t = mk["type"]
    if sa is None or sb is None:
        if final and t == "winner" and (wa or wb):
            mine = wa if mk["side"] == 0 else wb
            return ("won" if mine else "lost"), ""
        return "open", ""
    if t == "winner":
        mine, oth = (sa, sb) if mk["side"] == 0 else (sb, sa)
        if final:
            if wa or wb:
                return ("won" if (wa if mk["side"] == 0 else wb) else "lost"), ""
            return ("won" if mine > oth else "lost"), ""
        d = mine - oth
        return "open", ("leading" if d > 0 else "level" if d == 0 else "behind") + (f" by {abs(d)}" if d else "")
    if t == "draw":
        if final:
            return ("won" if sa == sb else "lost"), ""
        return "open", "level" if sa == sb else "not level"
    if t == "double_chance":
        mine, oth = (sa, sb) if mk.get("side") == 0 else (sb, sa)
        if final:
            return ("won" if mine >= oth else "lost"), ""
        return "open", "covering" if mine >= oth else "behind"
    if t == "handicap":
        mine, oth = (sa, sb) if mk["side"] == 0 else (sb, sa)
        res = asian(lambda h: mine - oth + h, mk["line"])
        if final:
            return res, ""
        return "open", "covering" if res in ("won", "half_won") else ("level on line" if res == "push" else "not covering")
    if t in ("total", "team_total"):
        cnt = sa + sb if t == "total" else (sa if mk["side"] == 0 else sb)
        line = mk["line"]
        q = round(line * 4)
        halves = [line] if q % 2 == 0 else [line - 0.25, line + 0.25]
        if mk["ou"] == "over":
            if cnt > max(halves):
                return "won", ""
            if final:
                return asian(lambda h: cnt - h, line), ""
            need = int(math.floor(max(halves))) + 1 - cnt
            return "open", f"needs {unit(sport, need)}"
        if cnt > max(halves):
            return "lost", ""
        if final:
            return asian(lambda h: h - cnt, line), ""
        allowed = int(math.ceil(min(halves))) - 1 - cnt
        return "open", f"{unit(sport, allowed)} to spare" if allowed > 0 else "no more allowed"
    if t == "btts":
        both = sa > 0 and sb > 0
        if mk["yes"]:
            if both:
                return "won", ""
            return ("lost", "") if final else ("open", "needs both to score")
        if both:
            return "lost", ""
        return ("won", "") if final else ("open", "holding")
    return "open", ""


def grade_mma(mk, fx, flip):
    wa, wb = fx["winner"]
    if flip:
        wa, wb = wb, wa
    final = fx["state"] == "post"
    elapsed = max(0, fx["period"] - 1) * 300 + fx["clock_secs"]
    t = mk["type"]
    if t == "winner":
        if final:
            if not (wa or wb):
                return "push", "draw / no contest"
            return ("won" if (wa if mk["side"] == 0 else wb) else "lost"), ""
        return "open", ""
    if t == "rounds_total":
        thr = (int(mk["line"]) * 300) + (150 if (mk["line"] % 1) else 0)
        if fx["state"] == "in" and elapsed > thr:
            return ("won" if mk["ou"] == "over" else "lost"), ""
        if final:
            over = elapsed > thr
            return ("won" if over == (mk["ou"] == "over") else "lost"), ""
        return "open", ""
    if t == "starts_round":
        started = fx["period"] >= mk["round"]
        if started and fx["state"] != "pre":
            return ("won" if mk["yes"] else "lost"), ""
        if final:
            return ("lost" if mk["yes"] else "won"), ""
        return "open", ""
    return "open", ""


def mma_result_text(fx, flip):
    if fx["state"] != "post":
        return ""
    wa, wb = fx["winner"]
    names = fx["disp"]
    if wa or wb:
        w = names[0] if wa else names[1]
        return f"{w} won · R{fx['period']} {fx['clock']}"
    return "Draw / NC"


def evaluate_leg(lg, fixtures):
    """Live/finished state of one leg. Never raises."""
    out = {"status": "open", "note": "", "score": "", "clock": "", "state": "pre",
           "sa": None, "sb": None, "fx": None, "minute": 0}
    mk = lg["mk"]
    sport = lg["sport"]
    a, b = lg["a"], lg["b"]
    # 1) result printed on the slip
    if lg.get("ft") and sport not in ("mma", "boxing"):
        sa, sb = lg["ft"]
        st, note = grade_scores(mk, sport, sa, sb, True)
        out.update(status=st if st != "open" else "settled", score=f"{a} {sa}–{sb} {b}", clock="FT",
                   state="post", sa=sa, sb=sb)
        return out
    # 2) live feed
    fx, flip = (None, False)
    if sport in SPORT_FEEDS:
        fx, flip = find_fixture(lg, fixtures)
    if fx is None:
        if sport not in SPORT_FEEDS:
            out.update(status="manual", note="no live feed — settles from slip or by you")
        else:
            out.update(note="not found yet")
        out["score"] = f"{a} vs {b}"
        return out
    out["fx"] = fx["id"]
    out["state"] = fx["state"]
    na, nb = fx["disp"][1 if flip else 0], fx["disp"][0 if flip else 1]
    sa, sb = fx["score"][1 if flip else 0], fx["score"][0 if flip else 1]
    ta, tb = fx["stext"][1 if flip else 0], fx["stext"][0 if flip else 1]
    wa, wb = fx["winner"][1 if flip else 0], fx["winner"][0 if flip else 1]
    final = fx["state"] == "post"
    if fx["state"] == "pre":
        when = fx["date"].astimezone(TZ).strftime("%a %H:%M") if fx["date"] else "soon"
        out.update(score=f"{na} vs {nb}", clock=when)
    elif sport == "mma":
        out.update(score=f"{na} vs {nb}", clock=mma_result_text(fx, flip) or f"R{fx['period']} {fx['clock']}")
    elif sport == "cricket":
        out.update(score=f"{na} {ta or '-'} · {nb} {tb or '-'}", clock="Result" if final else "Live")
    else:
        clk = "FT" if final else ("HT" if "half" in fx["desc"].lower() else (fx["short"] or fx["clock"]))
        out.update(score=f"{na} {sa if sa is not None else '-'}–{sb if sb is not None else '-'} {nb}", clock=clk)
    out.update(sa=sa, sb=sb, minute=fx["minute"])
    if fx["state"] == "pre":
        return out
    if sport == "mma":
        st, note = grade_mma(mk, fx, flip)
    elif mk["type"] in ("watch", "prop", "starts_round", "distance", "rounds_total"):
        st, note = ("settled" if final else "open"), ""
    elif sport == "cricket":
        st, note = grade_scores(mk, sport, None, None, final, wa, wb) if mk["type"] == "winner" else ("open", "")
    else:
        st, note = grade_scores(mk, sport, sa, sb, final, wa if sport != "soccer" else False,
                                wb if sport != "soccer" else False)
    if final and st == "open":
        st = "settled" if mk["type"] in ("watch",) else "manual"
        note = note or ("check result" if mk["type"] != "watch" else "")
    out.update(status=st, note=note)
    return out


LEG_FACTOR = {"won": None, "lost": 0.0, "push": 1.0, "half_lost": 0.5}


def leg_factor(st, odds):
    if st == "won":
        return odds or 2.0
    if st == "half_won":
        return ((odds or 2.0) + 1) / 2
    return LEG_FACTOR.get(st, 1.0)


def bet_outcome(bet, evs):
    """Compute (result, net) from legs if decided, else (None, None)."""
    stake = bet.get("stake") or 0.0
    sts = [e["status"] for e in evs]
    if any(s == "lost" for s in sts):
        if bet["kind"] == "parlay" or len(sts) == 1:
            return "lost", -stake
    decided = {"won", "lost", "push", "half_won", "half_lost"}
    if all(s in decided for s in sts):
        ret = stake
        for s, lg in zip(sts, bet["legs"]):
            ret *= leg_factor(s, lg.get("odds"))
        net = round(ret - stake, 2)
        if all(s == "push" for s in sts):
            return "void", 0.0
        return ("won" if net > 0 else "lost" if net < 0 else "void"), net
    return None, None


def settled_from_slip(bet):
    st = bet.get("status")
    if st not in ("won", "lost", "void"):
        return None, None
    stake = bet.get("stake") or 0.0
    if bet.get("wl") is not None:
        net = bet["wl"]
    elif st == "won":
        net = bet["potential"] if bet.get("potential") else (stake * ((bet.get("odds") or 2.0) - 1))
    elif st == "lost":
        net = -stake
    else:
        net = 0.0
    return st, round(net, 2)


def potential_profit(bet):
    if bet.get("potential"):
        return bet["potential"]
    if bet.get("stake") and bet.get("odds"):
        return round(bet["stake"] * (bet["odds"] - 1), 2)
    if bet.get("stake"):
        return round(bet["stake"], 2)
    return 0.0


def settle_date(bet):
    ds = [lg["date"] for lg in bet["legs"] if lg.get("date")]
    return max(ds) if ds else (bet.get("placed") or today_str())


# =====================================================================
# LEDGER / STATS
# =====================================================================
def saved_keys():
    return {b["key"] for b in STORE.ledger["bets"]} | {b["key"] for b in STORE.active}


def record(bet, result, net, evs=None, date=None):
    legs = []
    for i, lg in enumerate(bet["legs"]):
        e = evs[i] if evs and i < len(evs) else {}
        legs.append({"label": lg["label"], "match": f"{lg['a']} vs {lg['b']}", "sport": lg["sport"],
                     "status": e.get("status", ""), "score": e.get("score", "")})
    STORE.ledger["bets"].append({
        "key": bet["key"], "id": bet.get("id"), "title": bet["title"], "kind": bet["kind"],
        "sport": bet["sport"], "stake": bet.get("stake") or 0.0, "odds": bet.get("odds"),
        "result": result, "net": round(net or 0.0, 2), "date": date or today_str(), "legs": legs,
    })


def stats_for(bets):
    w = sum(1 for b in bets if b["result"] == "won")
    l = sum(1 for b in bets if b["result"] == "lost")
    net = sum(b["net"] for b in bets)
    staked = sum(b["stake"] for b in bets if b["result"] != "void")
    return w, l, net, staked


def streaks(bets):
    cur_t, cur_n, best_w = None, 0, 0
    run_w = 0
    for b in bets:
        r = b["result"]
        if r == "void":
            continue
        if r == cur_t:
            cur_n += 1
        else:
            cur_t, cur_n = r, 1
        run_w = run_w + 1 if r == "won" else 0
        best_w = max(best_w, run_w)
    return cur_t, cur_n, best_w


def build_stats_embed():
    bets = [b for b in STORE.ledger["bets"] if b["kind"] != "watch"]
    w, l, net, staked = stats_for(bets)
    total = w + l
    pct = round(w * 100 / total) if total else 0
    roi = round(net * 100 / staked, 1) if staked else 0.0
    cur_t, cur_n, best_w = streaks(bets)
    biggest = max([b["net"] for b in bets] + [0])
    e = discord.Embed(title="📊 Your Stats", color=0x2ECC71 if net >= 0 else 0xE74C3C)
    if not bets:
        e.description = "No bets saved yet. Paste your bet history or drop a slip to start."
        return e
    streak_txt = "—"
    if cur_t:
        streak_txt = f"{'🔥' if cur_t == 'won' else '🧊'} {cur_n} {'win' if cur_t == 'won' else 'loss'}{'es' if cur_t == 'lost' and cur_n != 1 else ('s' if cur_n != 1 and cur_t == 'won' else '')} in a row"
    e.add_field(name="All time", inline=False, value=(
        f"**{w}** won · **{l}** lost · {pct}% hit\n"
        f"Profit: **{money(net, True)}** · Staked {money(staked)} · ROI {roi:+}%\n"
        f"Best win: {money(biggest, True)} · Streak: {streak_txt} · Best run: {best_w}"))
    t = today_str()
    tb = [b for b in bets if b["date"] == t]
    tw, tl, tn, _ = stats_for(tb)
    wk_start = (now_local() - timedelta(days=6)).strftime("%Y-%m-%d")
    wb = [b for b in bets if b["date"] >= wk_start]
    ww, wl, wn, _ = stats_for(wb)
    e.add_field(name="Today", inline=True, value=f"{tw}W – {tl}L\n**{money(tn, True)}**")
    e.add_field(name="Last 7 days", inline=True, value=f"{ww}W – {wl}L\n**{money(wn, True)}**")
    singles = [b for b in bets if b["kind"] == "single"]
    parlays = [b for b in bets if b["kind"] == "parlay"]
    sw, sl, sn, _ = stats_for(singles)
    pw, pl, pn, _ = stats_for(parlays)
    e.add_field(name="Singles / Parlays", inline=True,
                value=f"Singles {sw}–{sl} {money(sn, True)}\nParlays {pw}–{pl} {money(pn, True)}")
    by = {}
    for b in bets:
        by.setdefault(b["sport"], []).append(b)
    lines = []
    for sp, lst in sorted(by.items(), key=lambda kv: -len(kv[1])):
        a, c, n, _ = stats_for(lst)
        lines.append(f"{SPORT_ICON.get(sp, '🎯')} {SPORT_NAME.get(sp, 'Mixed')}: {a}–{c} · {money(n, True)}")
    e.add_field(name="By sport", inline=False, value="\n".join(lines[:10]))
    e.set_footer(text=f"{len(STORE.active)} bet(s) live right now")
    return e


def result_tag(result, net):
    if result == "won":
        return f"✅ WON {money(net, True)}"
    if result == "lost":
        return f"❌ LOST {money(net, True)}"
    return "↩️ VOID"


def build_history_embed(n=10):
    bets = STORE.ledger["bets"][-n:]
    e = discord.Embed(title=f"🧾 Last {len(bets)} bets", color=0x5865F2)
    if not bets:
        e.description = "Nothing saved yet."
        return e
    lines = []
    for b in reversed(bets):
        lines.append(f"{result_tag(b['result'], b['net'])} · {SPORT_ICON.get(b['sport'], '🎯')} {clip(b['title'], 40)} · {b['date'][5:]}")
    e.description = "\n".join(lines)
    return e


HELP_TEXT = (
    "**Add bets**\n"
    "• Paste your bet history (any number of bets) or drop a `.txt`\n"
    "• Drop a bet slip screenshot\n"
    "• Or type one: `Arsenal vs Chelsea over 2.5 @1.8 stake 500`\n"
    "• Just `Arsenal vs Chelsea` = follow the match (no stats)\n\n"
    "You always get a preview first → tap **Save**.\n"
    "Finished bets go to stats. Running bets are tracked live (goals, HT, 70', results).\n"
    "Bets already saved are skipped automatically.\n\n"
    "**Words you can type:** `stats` · `live` · `history` · `help` · `reset stats`\n"
    "Live feeds: soccer, UFC, cricket, NBA, NFL, NHL, MLB. Esports/boxing/props: settled from your slip or by you (Live → Settle)."
)

# =====================================================================
# CARDS
# =====================================================================
ICON = {"won": "✅", "lost": "❌", "push": "↩️", "half_won": "🟩", "half_lost": "🟥", "open": "🟢",
        "manual": "✋", "settled": "🏁"}


def leg_icon(ev):
    if ev["status"] == "open":
        return "🟢" if ev["state"] == "in" else "🕒"
    return ICON.get(ev["status"], "•")


def leg_lines(lg, ev):
    first = f"{leg_icon(ev)} **{clip(lg['label'], 70)}**" + (f" @{lg['odds']:g}" if lg.get("odds") else "")
    bits = [clip(ev["score"] or f"{lg['a']} vs {lg['b']}", 70)]
    if ev["clock"]:
        bits.append(ev["clock"])
    if ev["note"]:
        bits.append(f"*{ev['note']}*")
    return first + "\n┗ " + " · ".join(bits)


def money_line(bet):
    parts = []
    if bet.get("stake"):
        parts.append(f"Stake {money(bet['stake'])}")
    if bet.get("odds"):
        parts.append(f"Odds {bet['odds']:g}")
    return " · ".join(parts)


def preview_info(bet, evs):
    """Returns (verdict, net, color, tag)."""
    if bet["key"] in saved_keys():
        return "dup", 0, 0x95A5A6, "⚠️ Already saved — will skip"
    if bet["kind"] == "watch":
        return "watch", 0, 0x3498DB, "👀 Follow only (not in stats)"
    r, n = settled_from_slip(bet)
    if r is None:
        r, n = bet_outcome(bet, evs)
    if r == "won":
        return r, n, 0x2ECC71, f"✅ WON {money(n, True)}"
    if r == "lost":
        return r, n, 0xE74C3C, f"❌ LOST {money(n, True)}"
    if r == "void":
        return r, n, 0x95A5A6, "↩️ VOID"
    return "live", 0, 0x3498DB, f"🔥 LIVE · to win {money(potential_profit(bet), True)}"


def bet_embed(i, bet, evs, tag, color):
    icon = SPORT_ICON.get(bet["sport"], "🎯")
    head = f"{icon} Bet {i} · {clip(bet['title'], 60)}"
    e = discord.Embed(title=head, color=color)
    body = [leg_lines(lg, ev) for lg, ev in zip(bet["legs"], evs)]
    ml = money_line(bet)
    body.append("")
    body.append(f"**{tag}**" + (f"\n{ml}" if ml else ""))
    e.description = clip("\n".join(body), 4000)
    if bet.get("id"):
        e.set_footer(text=f"ID {bet['id']}" + (f" · placed {bet['placed']}" if bet.get("placed") else ""))
    return e


def chunk_embeds(embeds):
    out, cur, size = [], [], 0
    for e in embeds:
        s = len(e.title or "") + len(e.description or "") + len((e.footer.text if e.footer else "") or "")
        if cur and (len(cur) >= 10 or size + s > 5800):
            out.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += s
    if cur:
        out.append(cur)
    return out


async def build_preview(session, bets):
    legs = [lg for b in bets for lg in b["legs"]]
    fixtures = await fetch_for_legs(session, legs)
    infos, counts = [], {"won": 0, "lost": 0, "void": 0, "live": 0, "dup": 0, "watch": 0}
    for b in bets:
        evs = [evaluate_leg(lg, fixtures) for lg in b["legs"]]
        verdict, net, color, tag = preview_info(b, evs)
        counts[verdict] = counts.get(verdict, 0) + 1
        infos.append((b, evs, verdict, net, color, tag))
    embeds = []
    if len(bets) <= 6:
        for i, (b, evs, v, n, c, t) in enumerate(infos, 1):
            embeds.append(bet_embed(i, b, evs, t, c))
    else:
        lines = []
        for i, (b, evs, v, n, c, t) in enumerate(infos, 1):
            lines.append(f"**{i}.** {SPORT_ICON.get(b['sport'], '🎯')} {clip(b['title'], 34)} — {t}")
        text = "\n".join(lines)
        part = []
        for ln in text.split("\n"):
            if sum(len(x) + 1 for x in part) + len(ln) > 3900:
                embeds.append(discord.Embed(description="\n".join(part), color=0x5865F2))
                part = []
            part.append(ln)
        if part:
            embeds.append(discord.Embed(description="\n".join(part), color=0x5865F2))
        embeds[0].title = f"📋 {len(bets)} bets found"
    summary = []
    if counts["won"] or counts["lost"] or counts["void"]:
        net = sum(n for (_, _, v, n, _, _) in infos if v in ("won", "lost", "void"))
        summary.append(f"🏁 {counts['won'] + counts['lost'] + counts['void']} finished ({counts['won']}W–{counts['lost']}L, {money(net, True)})")
    if counts["live"]:
        summary.append(f"🔥 {counts['live']} live → will be tracked")
    if counts["watch"]:
        summary.append(f"👀 {counts['watch']} match(es) to follow")
    if counts["dup"]:
        summary.append(f"⚠️ {counts['dup']} already saved (skipped)")
    new = len(bets) - counts["dup"]
    return embeds, " · ".join(summary), new


def leg_snapshot(ev):
    return {"sa": ev["sa"], "sb": ev["sb"], "state": ev["state"], "done": ev["status"] in ("won", "lost", "push", "half_won", "half_lost"),
            "status": ev["status"], "ko": ev["state"] != "pre", "ht": False, "late": False, "fx": ev["fx"]}


async def build_active_embed(session):
    e = discord.Embed(title=f"🔥 Live bets ({len(STORE.active)})", color=0x3498DB)
    if not STORE.active:
        e.description = "Nothing live right now. Paste a bet or drop a slip to start tracking."
        return e
    legs = [lg for b in STORE.active for lg in b["legs"]]
    fixtures = await fetch_for_legs(session, legs)
    total_size = 0
    for i, b in enumerate(STORE.active[:20], 1):
        evs = [evaluate_leg(lg, fixtures) for lg in b["legs"]]
        won = sum(1 for x in evs if x["status"] in ("won", "half_won"))
        name = f"{i}. {SPORT_ICON.get(b['sport'], '🎯')} {clip(b['title'], 40)}"
        if b["kind"] == "parlay":
            name += f" · {won}/{len(evs)} done"
        if b["kind"] != "watch":
            name += f" · to win {money(potential_profit(b), True)}"
        val = clip("\n".join(leg_lines(lg, ev) for lg, ev in zip(b["legs"], evs)), 1020)
        total_size += len(name) + len(val)
        if total_size > 5500:
            e.add_field(name="…", value=f"+{len(STORE.active) - i + 1} more", inline=False)
            break
        e.add_field(name=clip(name, 250), value=val or "—", inline=False)
    e.set_footer(text=f"Updated {now_local().strftime('%H:%M')} · refreshes every {POLL_SECONDS}s in the background")
    return e


# =====================================================================
# DISCORD
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True


class Bot(commands.Bot):
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        if GH_TOKEN:
            await asyncio.sleep(8)  # let a previous instance finish saving
        await STORE.load(self.http_session)
        self.add_view(DashboardView())
        self.add_dynamic_items(PreviewButton)
        live_loop.start()
        flush_loop.start()


bot = Bot(command_prefix="!", intents=intents, help_command=None)


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Live bets", emoji="🔥", style=discord.ButtonStyle.primary, custom_id="dash:live")
    async def live_btn(self, it: discord.Interaction, _):
        await it.response.defer(thinking=True)
        e = await build_active_embed(bot.http_session)
        await it.followup.send(embed=e, view=ManageView() if STORE.active else DashboardView())

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.success, custom_id="dash:stats")
    async def stats_btn(self, it: discord.Interaction, _):
        await it.response.send_message(embed=build_stats_embed(), view=DashboardView())

    @discord.ui.button(label="History", emoji="🧾", style=discord.ButtonStyle.secondary, custom_id="dash:history")
    async def hist_btn(self, it: discord.Interaction, _):
        await it.response.send_message(embed=build_history_embed(), view=DashboardView())

    @discord.ui.button(label="Help", emoji="❔", style=discord.ButtonStyle.secondary, custom_id="dash:help")
    async def help_btn(self, it: discord.Interaction, _):
        await it.response.send_message(embed=discord.Embed(title="How to use", description=HELP_TEXT, color=0x5865F2), ephemeral=True)


class SettleSelect(discord.ui.Select):
    def __init__(self):
        opts = []
        for i, b in enumerate(STORE.active[:25], 1):
            opts.append(discord.SelectOption(label=clip(f"{i}. {b['title']}", 100), value=b["key"],
                                             description=clip(money_line(b) or "follow only", 100)))
        super().__init__(placeholder="Settle or remove a bet by hand…", options=opts or [discord.SelectOption(label="none", value="none")])

    async def callback(self, it: discord.Interaction):
        key = self.values[0]
        b = next((x for x in STORE.active if x["key"] == key), None)
        if not b:
            await it.response.send_message("That bet is no longer live.", ephemeral=True)
            return
        await it.response.send_message(f"**{b['title']}** — what happened?", view=SettleView(key), ephemeral=True)


class ManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)
        self.add_item(SettleSelect())

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.primary)
    async def refresh(self, it: discord.Interaction, _):
        await it.response.defer()
        e = await build_active_embed(bot.http_session)
        await it.edit_original_response(embed=e, view=ManageView() if STORE.active else DashboardView())

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.success)
    async def stats(self, it: discord.Interaction, _):
        await it.response.send_message(embed=build_stats_embed(), view=DashboardView())


class SettleView(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=300)
        self.key = key

    async def _do(self, it, result):
        b = next((x for x in STORE.active if x["key"] == self.key), None)
        if not b:
            await it.response.edit_message(content="Already settled.", view=None)
            return
        STORE.active.remove(b)
        if result == "remove" or b["kind"] == "watch":
            STORE.save()
            await it.response.edit_message(content=f"🗑️ Stopped tracking **{b['title']}**.", view=None)
            return
        net = potential_profit(b) if result == "won" else (-(b.get("stake") or 0) if result == "lost" else 0.0)
        record(b, result, net)
        STORE.save()
        await it.response.edit_message(content=f"Saved: **{b['title']}** → {result_tag(result, net)}", view=None)

    @discord.ui.button(label="Won", emoji="✅", style=discord.ButtonStyle.success)
    async def won(self, it, _):
        await self._do(it, "won")

    @discord.ui.button(label="Lost", emoji="❌", style=discord.ButtonStyle.danger)
    async def lost(self, it, _):
        await self._do(it, "lost")

    @discord.ui.button(label="Void", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def void(self, it, _):
        await self._do(it, "void")

    @discord.ui.button(label="Stop tracking", emoji="🗑️", style=discord.ButtonStyle.secondary)
    async def remove(self, it, _):
        await self._do(it, "remove")


class ResetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Yes, wipe everything", style=discord.ButtonStyle.danger)
    async def yes(self, it: discord.Interaction, _):
        STORE.ledger = {"version": 2, "bets": []}
        STORE.active = []
        STORE.save()
        await it.response.edit_message(content="🧹 Stats and live bets cleared.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, it: discord.Interaction, _):
        await it.response.edit_message(content="Nothing changed.", view=None)


class PreviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pv:(?P<act>ok|no):(?P<key>[a-z0-9]+)"):
    def __init__(self, act, key):
        style = discord.ButtonStyle.success if act == "ok" else discord.ButtonStyle.secondary
        label = "Save" if act == "ok" else "Cancel"
        emoji = "✅" if act == "ok" else "✖️"
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"pv:{act}:{key}"))
        self.act, self.key = act, key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], match["key"])

    async def callback(self, it: discord.Interaction):
        pend = STORE.pending.pop(self.key, None)
        if self.act == "no":
            STORE.save()
            await it.response.edit_message(content="✖️ Cancelled — nothing saved.", view=None)
            return
        if not pend:
            await it.response.send_message("This preview expired or was already saved. Send the bets again.", ephemeral=True)
            return
        await it.response.defer()
        summary = await commit_bets(pend["bets"], it.channel_id)
        STORE.save()
        await it.edit_original_response(content=summary, view=DashboardView())


async def commit_bets(bets, channel_id):
    keys = saved_keys()
    legs = [lg for b in bets for lg in b["legs"]]
    fixtures = await fetch_for_legs(bot.http_session, legs)
    saved, live, follow, dup, net_sum = 0, 0, 0, 0, 0.0
    for b in bets:
        if b["key"] in keys:
            dup += 1
            continue
        keys.add(b["key"])
        evs = [evaluate_leg(lg, fixtures) for lg in b["legs"]]
        r, n = (None, None) if b["kind"] == "watch" else settled_from_slip(b)
        if r is None and b["kind"] != "watch":
            r, n = bet_outcome(b, evs)
        if r is not None:
            record(b, r, n, evs, settle_date(b))
            saved += 1
            net_sum += n
            continue
        b["channel_id"] = channel_id
        b["added"] = time.time()
        for lg, ev in zip(b["legs"], evs):
            lg["live"] = leg_snapshot(ev)
        STORE.active.append(b)
        if b["kind"] == "watch":
            follow += 1
        else:
            live += 1
    parts = []
    if saved:
        parts.append(f"✅ Saved **{saved}** finished bet(s) ({money(net_sum, True)})")
    if live:
        parts.append(f"🔥 Tracking **{live}** live bet(s) — alerts will post here")
    if follow:
        parts.append(f"👀 Following **{follow}** match(es)")
    if dup:
        parts.append(f"⚠️ Skipped **{dup}** already saved")
    return "\n".join(parts) or "Nothing new to save."


async def send_preview(channel, bets, reply_to=None):
    embeds, summary, new = await build_preview(bot.http_session, bets)
    chunks = chunk_embeds(embeds)
    key = None
    if new:
        key = hashlib.md5(f"{time.time()}{channel.id}".encode()).hexdigest()[:10]
        STORE.pending[key] = {"bets": bets, "created": time.time()}
        STORE.save()
    for i, ch in enumerate(chunks):
        last = i == len(chunks) - 1
        view = None
        content = None
        if last:
            content = summary or None
            if key:
                view = discord.ui.View(timeout=None)
                view.add_item(PreviewButton("ok", key))
                view.add_item(PreviewButton("no", key))
        if i == 0 and reply_to is not None:
            msg = await reply_to.reply(content=content, embeds=ch, view=view, mention_author=False) if view else \
                await reply_to.reply(content=content, embeds=ch, mention_author=False)
        else:
            msg = await channel.send(content=content, embeds=ch, view=view) if view else \
                await channel.send(content=content, embeds=ch)


async def ocr_image(data, filename):
    try:
        form = aiohttp.FormData()
        form.add_field("apikey", os.getenv("OCR_SPACE_KEY", "helloworld"))
        form.add_field("language", "eng")
        form.add_field("OCREngine", "2")
        form.add_field("scale", "true")
        form.add_field("file", data, filename=filename or "slip.png")
        async with bot.http_session.post("https://api.ocr.space/parse/image", data=form,
                                         timeout=aiohttp.ClientTimeout(total=40)) as r:
            js = await r.json(content_type=None)
        res = js.get("ParsedResults") or []
        return "\n".join(x.get("ParsedText", "") for x in res)
    except Exception as e:
        print(f"[ocr] {e}", flush=True)
        return ""


def looks_like_bet(text):
    return bool(ID_RE.search(text) or split_vs_any(text) or re.search(r"@\s*\d", text))


def split_vs_any(text):
    return any(split_vs(l) for l in text.split("\n"))


@bot.event
async def on_ready():
    print(f"✅ Match Bot online as {bot.user} · {len(STORE.active)} live · {len(STORE.ledger['bets'])} saved", flush=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    text = (message.content or "").strip()
    low = text.lower()

    for att in message.attachments:
        name = (att.filename or "").lower()
        if name.endswith(".txt"):
            try:
                text = (text + "\n" + (await att.read()).decode("utf-8", errors="replace")).strip()
                low = text.lower()
            except Exception:
                pass
        elif name.endswith((".png", ".jpg", ".jpeg", ".webp")):
            async with message.channel.typing():
                raw = await ocr_image(await att.read(), att.filename)
                bets = parse_text(raw)
            if bets:
                await send_preview(message.channel, bets, reply_to=message)
            else:
                await message.reply("🤔 I couldn't read any bets from that image. Try a closer crop, or paste the text.", mention_author=False)
            return

    if not text or text.startswith("!"):
        return
    if low in ("stats", "my stats", "stat", "profit"):
        await message.channel.send(embed=build_stats_embed(), view=DashboardView())
        return
    if low in ("live", "active", "status", "bets", "active bets", "live bets"):
        async with message.channel.typing():
            e = await build_active_embed(bot.http_session)
        await message.channel.send(embed=e, view=ManageView() if STORE.active else DashboardView())
        return
    if low in ("history", "last", "recent"):
        await message.channel.send(embed=build_history_embed(), view=DashboardView())
        return
    if low in ("help", "menu", "start", "hi", "hello"):
        await message.channel.send(embed=discord.Embed(title="🎯 Match Bot", description=HELP_TEXT, color=0x5865F2), view=DashboardView())
        return
    if low in ("reset stats", "reset"):
        await message.channel.send("⚠️ This deletes **all** saved stats and live bets. Sure?", view=ResetView())
        return
    if looks_like_bet(text):
        async with message.channel.typing():
            bets = parse_text(text)
            if bets:
                await send_preview(message.channel, bets, reply_to=message)
                return
        await message.reply("🤔 I couldn't find a full bet in that. Type `help` to see examples.", mention_author=False)


# =====================================================================
# LIVE LOOP — alerts
# =====================================================================
def alert(title, desc, color):
    return discord.Embed(title=clip(title, 250), description=clip(desc, 3000), color=color)


@tasks.loop(seconds=POLL_SECONDS)
async def live_loop():
    if not STORE.active:
        return
    try:
        legs = [lg for b in STORE.active for lg in b["legs"]]
        fixtures = await fetch_for_legs(bot.http_session, legs)
    except Exception as e:
        print(f"[loop] fetch error {e}", flush=True)
        return
    announced = set()
    changed = False
    for b in list(STORE.active):
        try:
            out = []
            evs = []
            for i, lg in enumerate(b["legs"]):
                ev = evaluate_leg(lg, fixtures)
                evs.append(ev)
                snap = lg.setdefault("live", leg_snapshot(ev))
                fxk = ev["fx"] or f"{lg['a']}-{lg['b']}"
                pick = f"Your pick: **{lg['label']}**" + (f" → *{ev['note']}*" if ev["note"] else "")
                # kick-off
                if ev["state"] == "in" and not snap.get("ko"):
                    snap["ko"] = True
                    changed = True
                    if (fxk, "ko") not in announced:
                        announced.add((fxk, "ko"))
                        out.append(alert(f"🟢 Started · {ev['score']}", pick, 0x3498DB))
                # goals (soccer/hockey)
                if lg["sport"] in ("soccer", "hockey") and ev["sa"] is not None and snap.get("sa") is not None:
                    pa, pb = snap["sa"], snap["sb"]
                    if (ev["sa"], ev["sb"]) != (pa, pb):
                        changed = True
                        if ev["sa"] + ev["sb"] > pa + pb and (fxk, "g", ev["sa"], ev["sb"]) not in announced:
                            announced.add((fxk, "g", ev["sa"], ev["sb"]))
                            who = lg["a"] if ev["sa"] > pa else lg["b"]
                            out.append(alert(f"⚽ GOAL — {who} ({ev['clock']})", f"### {ev['score']}\n{pick}", 0x00A8FF))
                        elif ev["sa"] + ev["sb"] < pa + pb:
                            out.append(alert(f"🚫 Goal taken back ({ev['clock']})", f"### {ev['score']}\n{pick}", 0x95A5A6))
                if ev["sa"] is not None:
                    snap["sa"], snap["sb"] = ev["sa"], ev["sb"]
                # half-time & 70'
                if lg["sport"] == "soccer" and ev["state"] == "in" and not snap.get("done"):
                    if ev["clock"] == "HT" and not snap.get("ht"):
                        snap["ht"] = True
                        changed = True
                        if (fxk, "ht") not in announced:
                            announced.add((fxk, "ht"))
                            out.append(alert(f"⏸️ Half-time · {ev['score']}", pick, 0xF39C12))
                    if ev["minute"] >= 70 and not snap.get("late") and ev["clock"] != "HT":
                        snap["late"] = True
                        snap["ht"] = True
                        changed = True
                        if (fxk, "late") not in announced:
                            announced.add((fxk, "late"))
                            out.append(alert(f"⏱️ {ev['minute']}' · {ev['score']}", f"About 20 minutes left.\n{pick}", 0xE67E22))
                # leg decided
                decided = ev["status"] in ("won", "lost", "push", "half_won", "half_lost")
                if decided and not snap.get("done"):
                    snap["done"] = True
                    snap["status"] = ev["status"]
                    changed = True
                    if b["kind"] != "watch":
                        done_n = sum(1 for l2 in b["legs"] if l2.get("live", {}).get("done"))
                        left = len(b["legs"]) - done_n
                        if ev["status"] in ("won", "half_won"):
                            more = f"{done_n} done, {left} to go!" if left else "All legs in!"
                            out.append(alert(f"✅ Leg won · {lg['label']}", f"{ev['score']} · {ev['clock']}\n**{more}**" if b["kind"] == "parlay" else f"{ev['score']} · {ev['clock']}", 0x2ECC71))
                        elif ev["status"] in ("lost", "half_lost"):
                            out.append(alert(f"❌ Leg lost · {lg['label']}", f"{ev['score']} · {ev['clock']}", 0xE74C3C))
                        else:
                            out.append(alert(f"↩️ Leg void · {lg['label']}", f"{ev['score']} · {ev['clock']}", 0x95A5A6))
                if ev["state"] == "post" and snap.get("state") != "post":
                    changed = True
                    if b["kind"] == "watch" or ev["status"] == "manual":
                        out.append(alert(f"🏁 Full time · {ev['score']}",
                                         "" if b["kind"] == "watch" else f"{pick}\nI can't grade this pick automatically — use **Live → Settle**.", 0x95A5A6))
                snap["state"] = ev["state"]

            # settle?
            if b["kind"] == "watch":
                if evs and all(e["state"] == "post" for e in evs):
                    STORE.active.remove(b)
                    changed = True
            else:
                r, n = bet_outcome(b, evs)
                if r is not None:
                    STORE.active.remove(b)
                    record(b, r, n, evs, today_str())
                    changed = True
                    tw, tl, tn, _ = stats_for([x for x in STORE.ledger["bets"] if x["date"] == today_str()])
                    if r == "won":
                        out.append(alert(f"🏆 BET WON {money(n, True)}", f"**{b['title']}**\nToday: {tw}W–{tl}L · {money(tn, True)}", 0x2ECC71))
                    elif r == "lost":
                        out.append(alert(f"💔 Bet lost {money(n, True)}", f"**{b['title']}**\nToday: {tw}W–{tl}L · {money(tn, True)}", 0xE74C3C))
                    else:
                        out.append(alert("↩️ Bet void — stake back", f"**{b['title']}**", 0x95A5A6))
            if out:
                ch = bot.get_channel(b.get("channel_id") or 0)
                if ch is None and b.get("channel_id"):
                    try:
                        ch = await bot.fetch_channel(b["channel_id"])
                    except Exception:
                        ch = None
                if ch:
                    for grp in chunk_embeds(out):
                        await ch.send(embeds=grp)
        except Exception as e:
            print(f"[loop] bet {b.get('key')} error: {e}", flush=True)
    if changed:
        STORE.save()


@live_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def flush_loop():
    await STORE.flush(bot.http_session)


async def shutdown():
    print("[bot] shutting down, saving…", flush=True)
    try:
        STORE.dirty = True
        await STORE.flush(bot.http_session)
    finally:
        await bot.close()


async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:
            pass
    async with bot:
        await bot.start(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
