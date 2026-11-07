"""
⚡ Ultimate Multi-Sport Tracker — All-In-One 24/7 Discord Edition
Supports:
  ⚽ Football / Soccer (Goals, 1x2, Team Totals, BTTS)
  🏏 Cricket (Match Winner, Total Runs, Live Overs, Chase Target & Innings Reads)
  🥊 UFC / MMA / Boxing (Fighter Win, Round-by-Round Live Tracking, Method/Result)
  🎮 Esports / Custom Sports (Val/CS2/Dota/LoL Map Tracking + 1-Tap Instant Settle)

Included Convenience Features:
  1. 🏁 Past / Finished Match Auto-Grader (Detects already-finished matches on upload & offers 1-tap [✅ Save Result])
  2. ✏️ 1-Tap Edit Modal ([✏️ Edit Stake/Odds] popup box right in Discord)
  3. 💾 Permanent Cloud Stats Backup (Saves ledger.json permanently so server restarts never wipe your stats!)
  4. 🧠 "Why I Win / Why I Lose" Smart Insights (Win rate by Sport, Single vs Parlay, Parlays missed by 1 leg)
  5. 🏦 Bankroll & Profit Goal Progress Bar (Set your starting bankroll & profit target via popup modal)
"""

import asyncio
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks

# =====================================================================
# 1. CONFIGURATION
# =====================================================================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PORT = int(os.getenv("PORT", 8080))

BST = timezone(timedelta(hours=6))

def now_bst_str():
    return datetime.now(BST).strftime("%I:%M %p BST")

def today_bst_date():
    return datetime.now(BST).strftime("%Y-%m-%d")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ACTIVE_SLIPS = []
TRACKING_CHANNEL_ID = None
LAST_STATE = {}

LEDGER_FILE = "ledger.json"

SPORT_ICONS = {
    "football": "⚽",
    "cricket": "🏏",
    "ufc": "🥊",
    "mma": "🥊",
    "boxing": "🥊",
    "esports": "🎮",
    "custom": "🎯"
}

# =====================================================================
# 2. PERMANENT LIFETIME STATS, INSIGHTS & BANKROLL ENGINE
# =====================================================================
def load_ledger():
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "starting_bankroll": 5000.0,
        "profit_goal": 10000.0,
        "total_slips": 0,
        "slips_won": 0,
        "slips_lost": 0,
        "total_legs": 0,
        "legs_won": 0,
        "total_staked": 0.0,
        "net_profit": 0.0,
        "biggest_win": 0.0,
        "best_odds_hit": 0.0,
        "current_streak": 0,
        "best_streak": 0,
        "parlays_missed_by_one": 0,
        "singles_won": 0,
        "singles_total": 0,
        "parlays_won": 0,
        "parlays_total": 0,
        "sport_stats": {},
        "history": []
    }

def save_ledger(data):
    try:
        with open(LEDGER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Ledger save error: {e}")

def record_slip_in_ledger(slip, won, legs_hit_count):
    legs = slip.get("legs", [])
    total_legs = len(legs)
    stake = float(slip.get("stake", 100.0))
    profit = float(slip.get("potential_profit", 100.0))
    odds = float(slip.get("odds", 1.5))
    net_change = profit if won else -stake

    ledger = load_ledger()
    ledger["total_slips"] = ledger.get("total_slips", 0) + 1
    ledger["total_legs"] = ledger.get("total_legs", 0) + total_legs
    ledger["legs_won"] = ledger.get("legs_won", 0) + legs_hit_count
    ledger["total_staked"] = ledger.get("total_staked", 0.0) + stake
    ledger["net_profit"] = ledger.get("net_profit", 0.0) + net_change

    if total_legs == 1:
        ledger["singles_total"] = ledger.get("singles_total", 0) + 1
        if won:
            ledger["singles_won"] = ledger.get("singles_won", 0) + 1
    else:
        ledger["parlays_total"] = ledger.get("parlays_total", 0) + 1
        if won:
            ledger["parlays_won"] = ledger.get("parlays_won", 0) + 1
        elif legs_hit_count == total_legs - 1:
            ledger["parlays_missed_by_one"] = ledger.get("parlays_missed_by_one", 0) + 1

    sp_stats = ledger.get("sport_stats", {})
    for leg in legs:
        sp = leg.get("sport", "football").lower()
        if sp not in sp_stats:
            sp_stats[sp] = {"won": 0, "total": 0}
        sp_stats[sp]["total"] += 1
        if won or leg.get("achieved"):
            sp_stats[sp]["won"] += 1
    ledger["sport_stats"] = sp_stats

    if won:
        ledger["slips_won"] = ledger.get("slips_won", 0) + 1
        ledger["current_streak"] = ledger.get("current_streak", 0) + 1
        if ledger["current_streak"] > ledger.get("best_streak", 0):
            ledger["best_streak"] = ledger["current_streak"]
        if profit > ledger.get("biggest_win", 0.0):
            ledger["biggest_win"] = profit
        if odds > ledger.get("best_odds_hit", 0.0):
            ledger["best_odds_hit"] = odds
    else:
        ledger["slips_lost"] = ledger.get("slips_lost", 0) + 1
        ledger["current_streak"] = 0

    ledger["history"].append({
        "date": today_bst_date(),
        "title": slip.get("title", "Slip"),
        "sport": legs[0].get("sport", "football") if legs else "football",
        "won": won,
        "net": net_change
    })
    save_ledger(ledger)
    return ledger, net_change

def get_player_rank(wins, net_profit):
    if wins >= 25 or net_profit >= 15000:
        return "👑 GOAT Predictor"
    elif wins >= 10 or net_profit >= 5000:
        return "💎 Sharp Master"
    elif wins >= 5 or net_profit >= 1500:
        return "🔥 Hot Striker"
    elif wins >= 1:
        return "⚡ Rising Contender"
    return "🌱 Rookie Scout"

# =====================================================================
# 3. VISUAL HELPERS (BARS & GAUGES)
# =====================================================================
def render_progress_bar(done, total):
    filled = "🟩" * done
    empty = "⬜" * max(0, total - done)
    pct = int(round((done / total) * 100)) if total > 0 else 0
    return f"{filled}{empty}  **{done}/{total} Hit ({pct}%)**"

def render_goal_bar(current, target):
    if target <= 0:
        return "`██████████` 100%"
    ratio = max(0.0, min(1.0, current / target))
    bars = int(round(ratio * 10))
    pct = int(round(ratio * 100))
    return f"`{'█' * bars}{'░' * (10 - bars)}` **{pct}%** ({current:,.0f} / {target:,.0f})"

def render_win_meter(pct):
    bars = int(round(pct / 10))
    bar_str = "█" * bars + "░" * (10 - bars)
    if pct >= 75:
        vibe = "🔥 Looking Great"
    elif pct >= 45:
        vibe = "⚡ 50/50 Sweat"
    else:
        vibe = "🚨 Tough Spot"
    return f"`{bar_str}` **{pct}%** ({vibe})"

def poisson_prob(k_needed, expected_goals):
    if k_needed <= 0:
        return 1.0
    if expected_goals <= 0:
        return 0.0
    prob_less = sum((expected_goals ** i) * math.exp(-expected_goals) / math.factorial(i) for i in range(k_needed))
    return max(0.02, min(0.98, 1.0 - prob_less))

# =====================================================================
# 4. MULTI-SPORT IMAGE OCR & AI PARSER
# =====================================================================
async def extract_text_via_free_ocr(image_bytes):
    url = "https://api.ocr.space/parse/image"
    try:
        data = aiohttp.FormData()
        data.add_field("apikey", "helloworld")
        data.add_field("language", "eng")
        data.add_field("isOverlayRequired", "false")
        data.add_field("file", image_bytes, filename="slip.png", content_type="image/png")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    parsed_results = res.get("ParsedResults", [])
                    if parsed_results:
                        return parsed_results[0].get("ParsedText", "")
    except Exception as e:
        print(f"Free OCR error: {e}")
    return ""

def detect_sport_from_text(text):
    low = text.lower()
    if any(k in low for k in ["ufc", "mma", "bellator", "pfl", "fight night", "ko", "tko", "submission", "round"]):
        return "ufc"
    if any(k in low for k in ["boxing", "bout", "heavyweight", "welterweight"]):
        return "boxing"
    if any(k in low for k in ["cricket", "ipl", "t20", "odi", "wickets", "overs", "runs", "bpl", "psl", "bbl", "sixers", "scorchers", "tigers", "lions"]):
        return "cricket"
    if any(k in low for k in ["esports", "valorant", "cs2", "csgo", "dota", "lol", "league of legends", "map 1", "map 2", "bo3", "bo5"]):
        return "esports"
    return "football"

def call_gemini_parser(text_input=None, image_bytes=None):
    if not GEMINI_API_KEY:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        system_prompt = """
        Extract the sports slip details from the image or text.
        Detect the sport for each leg: "football", "cricket", "ufc", "boxing", or "esports".
        Convert kickoff times to Bangladesh Time (BST, UTC+6) if shown, otherwise write 'Today (BST)'.
        Return ONLY valid raw JSON with this exact structure:
        {
          "title": "4-Leg Mix Parlay" or "Single Match",
          "odds": 2.30,
          "stake": 990,
          "potential_profit": 1291.84,
          "legs": [
            {
              "sport": "football" | "cricket" | "ufc" | "boxing" | "esports",
              "home_team": "Team/Fighter 1",
              "away_team": "Team/Fighter 2",
              "target_team": "Team/Fighter 1",
              "target_type": "team_win" | "over_goals" | "under_goals" | "team_over" | "btts" | "over_runs" | "over_rounds",
              "target_value": 0.5,
              "label": "Team 1 to Win",
              "kickoff_bst": "9:00 PM BST"
            }
          ]
        }
        Only output raw JSON.
        """
        contents = [system_prompt]
        if image_bytes:
            contents.append({"mime_type": "image/png", "data": image_bytes})
        if text_input:
            contents.append(f"User Input: {text_input}")

        resp = model.generate_content(contents)
        raw = resp.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Gemini error: {e}")
        return None

def smart_universal_text_parser(raw_text):
    lines = [l.strip() for l in raw_text.replace("\r", "").split("\n") if l.strip()]
    legs = []
    pending_target = None
    pending_sport = detect_sport_from_text(raw_text)
    odds_val = 0.0
    stake_val = 100.0
    profit_val = 0.0

    for line in lines:
        low = line.lower()
        sp_line = detect_sport_from_text(line)
        if sp_line != "football":
            pending_sport = sp_line

        if "odds:" in low:
            m = re.search(r"(\d+(?:\.\d+)?)", line)
            if m: odds_val = float(m.group(1))
        elif "stake:" in low or "total risk" in low:
            m = re.search(r"([\d,]+\.\d+|\d+)", line)
            if m: stake_val = float(m.group(1).replace(",", ""))
        elif "potential winnings" in low:
            m = re.search(r"([\d,]+\.\d+|\d+)", line)
            if m: profit_val = float(m.group(1).replace(",", ""))

        if "@" in line and not any(x in low for x in ["odds:", "stake:", "risk:"]):
            pending_target = line.split("@")[0].strip()

        if "-vs-" in low or " vs " in low or " v " in low:
            sep = "-vs-" if "-vs-" in low else (" vs " if " vs " in low else " v ")
            parts = re.split(sep, line, flags=re.IGNORECASE)
            if len(parts) >= 2:
                home = parts[0].strip()
                away = parts[1].strip()
                cond = pending_target if pending_target else line
                c_low = cond.lower()

                t_type = "team_win"
                t_val = 0.5
                t_team = home

                num_match = re.search(r"(\d+(?:\.\d+)?)", cond)
                if num_match:
                    t_val = float(num_match.group(1))

                if "over" in c_low:
                    if pending_sport == "cricket" or "run" in c_low:
                        t_type = "over_runs"
                    elif pending_sport in ("ufc", "boxing") or "round" in c_low:
                        t_type = "over_rounds"
                    elif home.lower() in c_low or away.lower() in c_low:
                        t_type = "team_over"
                        t_team = away if away.lower() in c_low else home
                    else:
                        t_type = "over_goals"
                elif "under" in c_low:
                    t_type = "under_goals"
                elif "btts" in c_low:
                    t_type = "btts"
                else:
                    t_type = "team_win"
                    t_team = away if away.lower() in c_low else home

                label = cond if pending_target else f"{t_team} Win"
                legs.append({
                    "sport": pending_sport,
                    "home_team": home,
                    "away_team": away,
                    "target_team": t_team,
                    "target_type": t_type,
                    "target_value": t_val,
                    "label": label,
                    "kickoff_bst": "Today (BST)"
                })
                pending_target = None

    if not legs:
        return None

    if odds_val <= 1.0:
        odds_val = round(1.4 ** len(legs), 2)
    if profit_val <= 0:
        profit_val = round(stake_val * (odds_val - 1.0), 2)

    return {
        "title": f"{len(legs)}-Leg Multi-Sport Parlay" if len(legs) > 1 else f"{legs[0]['home_team']} vs {legs[0]['away_team']}",
        "odds": odds_val,
        "stake": stake_val,
        "potential_profit": profit_val,
        "legs": legs
    }

# =====================================================================
# 5. MULTI-SPORT LIVE DATA FEEDS (FOOTBALL + CRICKET + UFC/MMA)
# =====================================================================
async def fetch_all_sports_feeds():
    feeds = {"football": [], "cricket": [], "ufc": []}
    headers = {"User-Agent": "Mozilla/5.0"}

    async def get_json(session, url):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        return None

    async with aiohttp.ClientSession() as session:
        s_data, c_data, u_data = await asyncio.gather(
            get_json(session, "https://site.api.espn.com/apis/site/v2/sports/soccer/scorepanel"),
            get_json(session, "https://site.api.espn.com/apis/site/v2/sports/cricket/scorepanel"),
            get_json(session, "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard")
        )

        if s_data:
            for g in s_data.get("scores", []):
                feeds["football"].extend(g.get("events", []))
        if c_data:
            for g in c_data.get("scores", []):
                feeds["cricket"].extend(g.get("events", []))
        if u_data:
            for ev in u_data.get("events", []):
                feeds["ufc"].extend(ev.get("competitions", []))

    return feeds

def match_event_multi_sport(leg, feeds):
    sp = leg.get("sport", "football").lower()
    t1 = leg.get("home_team", "").lower()
    t2 = leg.get("away_team", "").lower()

    if sp in ("ufc", "mma", "boxing") or feeds.get("ufc"):
        for comp in feeds.get("ufc", []):
            c_list = comp.get("competitors", [])
            names = " ".join(c.get("athlete", {}).get("displayName", "").lower() for c in c_list)
            if (t1 and t1 in names) or (t2 and t2 in names):
                leg["sport"] = "ufc"
                return comp

    if sp == "cricket" or feeds.get("cricket"):
        for ev in feeds.get("cricket", []):
            name = ev.get("name", "").lower()
            if (t1 and len(t1) > 3 and t1 in name) or (t2 and len(t2) > 3 and t2 in name):
                leg["sport"] = "cricket"
                return ev

    for ev in feeds.get("football", []):
        name = ev.get("name", "").lower()
        short = ev.get("shortName", "").lower()
        if (t1 in name or t1 in short) and (t2 in name or t2 in short):
            return ev
        if t1 and len(t1) > 3 and t1 in name:
            return ev
        if t2 and len(t2) > 3 and t2 in name:
            return ev

    return None

def evaluate_leg_multi_sport(leg, event):
    sp = leg.get("sport", "football").lower()
    icon = SPORT_ICONS.get(sp, "⚽")

    if leg.get("manual_status") == "won":
        return {
            "sport_icon": icon, "status": "Final", "score": f"{leg.get('home_team')} vs {leg.get('away_team')}",
            "clock_display": "FT", "is_mid": False, "is_late": False, "is_final": True,
            "achieved": True, "impossible": False, "likelihood_pct": 100,
            "requirement": "WON! ✅", "analysis": "Marked as Won!"
        }
    elif leg.get("manual_status") == "lost":
        return {
            "sport_icon": icon, "status": "Final", "score": f"{leg.get('home_team')} vs {leg.get('away_team')}",
            "clock_display": "FT", "is_mid": False, "is_late": False, "is_final": True,
            "achieved": False, "impossible": True, "likelihood_pct": 0,
            "requirement": "LOST ❌", "analysis": "Marked as Lost."
        }

    if not event:
        return {
            "sport_icon": icon,
            "status": "Scheduled / Custom",
            "score": f"{leg.get('home_team')} vs {leg.get('away_team')}",
            "clock_display": leg.get("kickoff_bst", "Live / Today"),
            "is_mid": False, "is_late": False, "is_final": False,
            "achieved": False, "impossible": False,
            "likelihood_pct": 55,
            "requirement": "In Play / Waiting",
            "analysis": "Tracking active. Use Quick Win button anytime for custom/esports!"
        }

    # --- CASE A: UFC / MMA / BOXING ---
    if sp in ("ufc", "mma", "boxing") and "competitors" in event and "athlete" in event["competitors"][0]:
        st = event.get("status", {})
        desc = st.get("type", {}).get("description", "Scheduled")
        rnd = st.get("period", 1)
        clk = st.get("displayClock", "5:00")
        is_final = "Final" in desc

        c_list = event.get("competitors", [])
        f1 = c_list[0].get("athlete", {}).get("displayName", "Fighter 1")
        f2 = c_list[1].get("athlete", {}).get("displayName", "Fighter 2") if len(c_list) > 1 else "Fighter 2"
        w1 = c_list[0].get("winner", False)
        w2 = c_list[1].get("winner", False) if len(c_list) > 1 else False

        t_team = leg.get("target_team", f1).lower()
        picked_f1 = t_team in f1.lower()
        my_fighter = f1 if picked_f1 else f2
        my_won = w1 if picked_f1 else w2

        if is_final:
            return {
                "sport_icon": "🥊", "status": "Final",
                "score": f"{f1} vs {f2} (Ended R{rnd} {clk})",
                "clock_display": f"Final (R{rnd})",
                "is_mid": False, "is_late": False, "is_final": True,
                "achieved": bool(my_won), "impossible": not bool(my_won),
                "likelihood_pct": 100 if my_won else 0,
                "requirement": f"{my_fighter} WON! ✅" if my_won else f"{my_fighter} LOST ❌",
                "analysis": f"Fight finished in Round {rnd} at {clk}."
            }
        else:
            in_fight = rnd > 0 and desc != "Scheduled"
            return {
                "sport_icon": "🥊", "status": desc,
                "score": f"{f1} vs {f2}",
                "clock_display": f"Round {rnd} ({clk})" if in_fight else "Pre-Fight",
                "is_mid": (in_fight and rnd == 2), "is_late": (in_fight and rnd >= 3), "is_final": False,
                "achieved": False, "impossible": False,
                "likelihood_pct": 60,
                "requirement": f"Need {my_fighter} to win ({'Round ' + str(rnd) if in_fight else 'Upcoming'})",
                "analysis": f"Fight is in Round {rnd} ({clk}). Going the distance so far!" if in_fight else "Bout is queued on the card."
            }

    # --- CASE B: CRICKET ---
    if sp == "cricket":
        st = event.get("status", {})
        desc = st.get("type", {}).get("description", "Scheduled")
        summary = st.get("summary", "")
        is_final = desc in ("Result", "Final", "Complete") or st.get("type", {}).get("state") == "post"
        is_live = st.get("type", {}).get("state") == "in"

        comp = (event.get("competitions") or [{}])[0]
        c_list = comp.get("competitors", [])
        if len(c_list) >= 2:
            t1_name = c_list[0].get("team", {}).get("displayName", "Team 1")
            t1_score = c_list[0].get("score", "0/0")
            t1_win = c_list[0].get("winner", False)
            t2_name = c_list[1].get("team", {}).get("displayName", "Team 2")
            t2_score = c_list[1].get("score", "0/0")
            t2_win = c_list[1].get("winner", False)
        else:
            t1_name, t1_score, t1_win = leg.get("home_team"), "", False
            t2_name, t2_score, t2_win = leg.get("away_team"), "", False

        target_team = leg.get("target_team", t1_name).lower()
        picked_t1 = target_team in t1_name.lower()
        my_team = t1_name if picked_t1 else t2_name
        my_won = t1_win if picked_t1 else t2_win

        score_str = f"{t1_name} {t1_score} vs {t2_name} {t2_score}".strip()
        if is_final:
            won_bool = bool(my_won) or (my_team.lower() in summary.lower() and "won" in summary.lower())
            return {
                "sport_icon": "🏏", "status": "Result",
                "score": score_str, "clock_display": "Final",
                "is_mid": False, "is_late": False, "is_final": True,
                "achieved": won_bool, "impossible": not won_bool,
                "likelihood_pct": 100 if won_bool else 0,
                "requirement": f"WON! ({summary}) ✅" if won_bool else f"LOST ({summary}) ❌",
                "analysis": summary or "Match completed."
            }
        else:
            return {
                "sport_icon": "🏏", "status": "Live" if is_live else "Scheduled",
                "score": score_str, "clock_display": "In Play" if is_live else "Scheduled",
                "is_mid": is_live and ("10 ov" in score_str or "target" in score_str.lower()),
                "is_late": is_live and ("16 ov" in score_str or "17 ov" in score_str or "18 ov" in score_str),
                "is_final": False, "achieved": False, "impossible": False,
                "likelihood_pct": 62,
                "requirement": f"Pick: {leg.get('label')} • {summary}",
                "analysis": summary if summary else "Cricket match in progress."
            }

    # --- CASE C: FOOTBALL / SOCCER ---
    status_obj = event.get("status", {})
    type_desc = status_obj.get("type", {}).get("description", "Scheduled")
    clock_str = status_obj.get("displayClock", "0'")

    minute = 0
    try:
        minute = int(clock_str.replace("'", "").split("+")[0])
    except Exception:
        minute = 45 if "Halftime" in type_desc else (90 if "Final" in type_desc else 0)

    comps = event.get("competitors", [])
    home_score, away_score = 0, 0
    home_name, away_name = leg.get("home_team", "Home"), leg.get("away_team", "Away")
    for c in comps:
        s_val = int(c.get("score") or 0)
        n_val = c.get("displayName", "")
        if c.get("homeAway") == "home":
            home_score, home_name = s_val, n_val
        else:
            away_score, away_name = s_val, n_val

    is_final = "Final" in type_desc or type_desc in ("Full Time", "FT")
    is_halftime = "Halftime" in type_desc or type_desc == "HT" or (45 <= minute <= 48)
    is_late = (70 <= minute <= 76)
    rem_mins = max(0, 90 - minute)
    exp_rem_goals = rem_mins * 0.032

    t_type = leg.get("target_type", "over_goals")
    t_val = float(leg.get("target_value", 0.5))

    achieved, impossible = False, False
    likelihood = 0.55
    requirement = ""
    analysis = ""

    if t_type == "team_over":
        t_team = leg.get("target_team", home_name).lower()
        team_score = away_score if t_team in away_name.lower() else home_score
        t_disp = leg.get("target_team", home_name)
        needed = math.ceil(t_val) - team_score

        if needed <= 0:
            achieved, likelihood = True, 1.0
            requirement = "LOCKED IN! ✅"
            analysis = f"{t_disp} scored {team_score} goal(s)!"
        elif is_final:
            impossible, likelihood = True, 0.0
            requirement = f"MISSED ❌ (Finished with {team_score})"
            analysis = f"Full time reached. {t_disp} didn't score enough."
        else:
            likelihood = poisson_prob(needed, rem_mins * 0.017)
            requirement = f"Need {needed} more goal from {t_disp} in {rem_mins} mins"
            analysis = f"{t_disp} has {rem_mins} mins + stoppage time to score {needed} goal."

    elif t_type == "over_goals":
        total = home_score + away_score
        needed = math.ceil(t_val) - total
        if needed <= 0:
            achieved, likelihood = True, 1.0
            requirement = "LOCKED IN! ✅"
            analysis = f"Match has {total} goals—target already hit!"
        elif is_final:
            impossible, likelihood = True, 0.0
            requirement = f"MISSED ❌ (Finished at {total} goals)"
            analysis = f"Full time reached with {total} total goals."
        else:
            likelihood = poisson_prob(needed, exp_rem_goals)
            requirement = f"Need {needed} more goal(s) in {rem_mins} mins"
            analysis = f"{needed} more goal needed in the final {rem_mins} minutes."

    elif t_type == "team_win":
        t_team = leg.get("target_team", home_name).lower()
        is_home = t_team in home_name.lower()
        lead = (home_score - away_score) if is_home else (away_score - home_score)
        t_disp = leg.get("target_team", home_name)

        if is_final:
            if lead > 0:
                achieved, likelihood = True, 1.0
                requirement = "WON! ✅"
                analysis = f"Full time! {t_disp} won the match!"
            else:
                impossible, likelihood = True, 0.0
                requirement = "LOST ❌"
                analysis = f"Full time reached. {t_disp} did not win."
        else:
            if lead > 0:
                likelihood = min(0.96, 0.74 + (0.22 * (minute / 90.0)) + (0.08 * (lead - 1)))
                requirement = f"Leading by {lead} ({rem_mins} mins left)"
                analysis = f"{t_disp} is winning by {lead} and in control."
            elif lead == 0:
                likelihood = max(0.18, 0.46 - (0.16 * (minute / 90.0)))
                requirement = f"Need 1 goal to take the lead ({rem_mins} mins left)"
                analysis = f"Tied {home_score}-{away_score}. {t_disp} needs 1 goal to lead."
            else:
                needed = abs(lead) + 1
                likelihood = poisson_prob(needed, exp_rem_goals)
                requirement = f"Trailing! Need {needed} goals in {rem_mins} mins"
                analysis = f"{t_disp} is down by {abs(lead)} and needs a comeback."

    pct = int(round(likelihood * 100))
    return {
        "sport_icon": "⚽", "status": type_desc,
        "score": f"{home_name} {home_score} - {away_score} {away_name}",
        "clock_display": f"{minute}'" if minute > 0 else type_desc,
        "is_mid": is_halftime, "is_late": is_late, "is_final": is_final,
        "achieved": achieved, "impossible": impossible,
        "likelihood_pct": pct, "requirement": requirement, "analysis": analysis
    }

# =====================================================================
# 6. EMBED BUILDERS (LIFETIME STATS, INSIGHTS, BANKROLL & ACTIVE HUB)
# =====================================================================
def build_lifetime_stats_embed():
    ledger = load_ledger()
    total = ledger.get("total_slips", 0)
    won = ledger.get("slips_won", 0)
    lost = ledger.get("slips_lost", 0)
    win_rate = round((won / total * 100), 1) if total > 0 else 0.0

    t_legs = ledger.get("total_legs", 0)
    w_legs = ledger.get("legs_won", 0)
    leg_rate = round((w_legs / t_legs * 100), 1) if t_legs > 0 else 0.0

    staked = ledger.get("total_staked", 0.0)
    net = ledger.get("net_profit", 0.0)
    roi = round((net / staked * 100), 1) if staked > 0 else 0.0

    start_bank = ledger.get("starting_bankroll", 5000.0)
    curr_bank = start_bank + net
    goal = ledger.get("profit_goal", 10000.0)

    rank = get_player_rank(won, net)
    streak = ledger.get("current_streak", 0)
    best_streak = ledger.get("best_streak", 0)

    s_tot = ledger.get("singles_total", 0)
    s_won = ledger.get("singles_won", 0)
    s_pct = round(s_won / s_tot * 100) if s_tot > 0 else 0

    p_tot = ledger.get("parlays_total", 0)
    p_won = ledger.get("parlays_won", 0)
    p_pct = round(p_won / p_tot * 100) if p_tot > 0 else 0
    one_leg_heartbreaks = ledger.get("parlays_missed_by_one", 0)

    sp_stats = ledger.get("sport_stats", {})
    sp_lines = []
    for sp_name, d in sp_stats.items():
        if d.get("total", 0) > 0:
            ic = SPORT_ICONS.get(sp_name, "🎯")
            rate = round(d["won"] / d["total"] * 100)
            sp_lines.append(f"{ic} **{sp_name.upper()}:** `{d['won']}/{d['total']}` (**{rate}%**)")
    sp_summary = "  •  ".join(sp_lines) if sp_lines else "Play a match to unlock sport-by-sport accuracy!"

    today_str = today_bst_date()
    today_list = [h for h in ledger.get("history", []) if h.get("date") == today_str]
    t_won = sum(1 for h in today_list if h.get("won"))
    t_lost = len(today_list) - t_won
    t_net = sum(h.get("net", 0.0) for h in today_list)

    embed = discord.Embed(
        title=f"🏆 Lifetime Career Hub — {rank}",
        description=f"🏦 **Current Bankroll:** **`{curr_bank:,.1f}`** *(Started: {start_bank:,.0f})*\n🎯 **Profit Goal Progress:** {render_goal_bar(max(0, net), goal)}",
        color=0xF1C40F
    )
    embed.add_field(
        name="👑 All-Time Record",
        value=(
            f"• **Total Slips:** `{total}`\n"
            f"• **Won / Lost:** ✅ `{won}W` — ❌ `{lost}L`\n"
            f"• **Win Rate:** 🎯 **`{win_rate}%`**\n"
            f"• **Legs Hit:** `{w_legs}/{t_legs}` (`{leg_rate}%`)"
        ),
        inline=True
    )
    embed.add_field(
        name="💰 Profit & Streaks",
        value=(
            f"• **Net Profit:** **`{net:+,.1f}`**\n"
            f"• **Total ROI:** 📈 `{roi}%`\n"
            f"• **Biggest Win:** 💎 `+{ledger.get('biggest_win', 0):,.1f}`\n"
            f"• **Streak:** 🔥 `{streak}` *(Best: {best_streak})*"
        ),
        inline=True
    )
    embed.add_field(
        name="🧠 Smart Insights (Why You Win / Lose)",
        value=(
            f"• **Singles vs Parlays:** Singles `{s_won}/{s_tot} ({s_pct}%)`  |  Parlays `{p_won}/{p_tot} ({p_pct}%)`\n"
            f"• **Parlay Heartbreaks:** 💔 `{one_leg_heartbreaks}` Parlays missed by just **1 leg**\n"
            f"• **By Sport:** {sp_summary}"
        ),
        inline=False
    )
    embed.add_field(
        name=f"📅 Today's Action ({today_str})",
        value=f"• **Record:** `{t_won} Won` - `{t_lost} Lost`  •  **Today's Profit:** **`{t_net:+,.1f}`**",
        inline=False
    )
    recent = ledger.get("history", [])[-5:]
    if recent:
        rec_lines = []
        for h in reversed(recent):
            ic = "✅ WON" if h.get("won") else "❌ LOST"
            sp_ic = SPORT_ICONS.get(h.get("sport", "football"), "⚽")
            rec_lines.append(f"{ic} {sp_ic} **{h.get('title')}** (`{h.get('net', 0):+,.1f}`) — *{h.get('date')}*")
        embed.add_field(name="📜 Last 5 Settled Slips", value="\n".join(rec_lines), inline=False)

    embed.set_footer(text=f"Updated {now_bst_str()}")
    return embed

async def build_active_matches_embed():
    if not ACTIVE_SLIPS:
        return discord.Embed(
            title="📡 Active Matches Center",
            description="No active slips running right now!\n\n📸 **Drop any slip screenshot** or **type a match** (Football, Cricket, UFC, Boxing, Esports) to start!",
            color=0x95A5A6
        )

    feeds = await fetch_all_sports_feeds()
    embed = discord.Embed(
        title=f"🔥 Currently Active Slips ({len(ACTIVE_SLIPS)} Running)",
        description="Live scores, round/over clocks, and win meters across all your sports.",
        color=0x00A8FF
    )

    for s_idx, slip in enumerate(ACTIVE_SLIPS, 1):
        legs = slip.get("legs", [])
        done_count = len(slip.get("notified_achieved", set()))
        comb_prob = 1.0
        leg_lines = []

        for l_idx, leg in enumerate(legs, 1):
            ev = match_event_multi_sport(leg, feeds)
            res = evaluate_leg_multi_sport(leg, ev)
            comb_prob *= (res["likelihood_pct"] / 100.0)
            st_ic = "✅" if res["achieved"] else ("❌" if res["impossible"] else "🟢")
            leg_lines.append(
                f"{st_ic} {res['sport_icon']} **Leg {l_idx}:** {res['score']} (`{res['clock_display']}`)\n"
                f"   ↳ Pick: `{leg.get('label')}` • **{res['requirement']}**\n"
                f"   ↳ Chance: {render_win_meter(res['likelihood_pct'])}"
            )

        overall_pct = int(round(comb_prob * 100))
        header = (
            f"🎟️ **Slip #{s_idx}: {slip.get('title')}** (Odds: `{slip.get('odds')}x` • Profit: `+{slip.get('potential_profit')}`)\n"
            f"{render_progress_bar(done_count, len(legs))}"
        )
        body = "\n".join(leg_lines)
        if len(legs) > 1:
            body += f"\n🔗 **Full Parlay Chance:** {render_win_meter(overall_pct)}"
        embed.add_field(name=header, value=body, inline=False)

    embed.set_footer(text=f"Live Check at {now_bst_str()}")
    return embed

# =====================================================================
# 7. POPUP MODALS (1-TAP EDIT STAKE/ODDS & BANKROLL GOAL)
# =====================================================================
class EditSlipModal(discord.ui.Modal, title="✏️ Edit Slip Stake, Odds & Profit"):
    stake_input = discord.ui.TextInput(label="Stake Amount", placeholder="e.g. 990", required=True)
    odds_input = discord.ui.TextInput(label="Total Odds", placeholder="e.g. 2.30", required=True)
    profit_input = discord.ui.TextInput(label="Potential Net Profit (Optional)", placeholder="Leave blank to auto-calculate", required=False)

    def __init__(self, parsed_slip, parent_view):
        super().__init__()
        self.parsed_slip = parsed_slip
        self.parent_view = parent_view
        self.stake_input.default = str(parsed_slip.get("stake", 100))
        self.odds_input.default = str(parsed_slip.get("odds", 2.0))
        self.profit_input.default = str(parsed_slip.get("potential_profit", 100))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            st = float(self.stake_input.value.replace(",", ""))
            od = float(self.odds_input.value.replace(",", ""))
            if self.profit_input.value.strip():
                pr = float(self.profit_input.value.replace(",", ""))
            else:
                pr = round(st * (od - 1.0), 2)
            self.parsed_slip["stake"] = st
            self.parsed_slip["odds"] = od
            self.parsed_slip["potential_profit"] = pr
            embed, _, _, _ = await build_smart_preview_card(self.parsed_slip)
            await interaction.response.edit_message(embed=embed, view=self.parent_view)
        except Exception:
            await interaction.response.send_message("⚠️ Please enter valid numbers!", ephemeral=True)

class BankrollGoalModal(discord.ui.Modal, title="🏦 Set Bankroll & Profit Goal"):
    bankroll_input = discord.ui.TextInput(label="Starting Bankroll", placeholder="e.g. 10000", required=True)
    goal_input = discord.ui.TextInput(label="Target Profit Goal", placeholder="e.g. 25000", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ledger = load_ledger()
            ledger["starting_bankroll"] = float(self.bankroll_input.value.replace(",", ""))
            ledger["profit_goal"] = float(self.goal_input.value.replace(",", ""))
            save_ledger(ledger)
            await interaction.response.send_message(embed=build_lifetime_stats_embed(), view=MainDashboardView())
        except Exception:
            await interaction.response.send_message("⚠️ Please enter valid numbers!", ephemeral=True)

# =====================================================================
# 8. INTERACTIVE VIEWS (FINISHED MATCH AUTO-GRADER + DASHBOARD)
# =====================================================================
class MainDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔥 Active Matches", style=discord.ButtonStyle.primary)
    async def active_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await build_active_matches_embed()
        await interaction.response.send_message(embed=embed, view=MainDashboardView())

    @discord.ui.button(label="🏆 Lifetime Stats", style=discord.ButtonStyle.success)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_lifetime_stats_embed(), view=MainDashboardView())

    @discord.ui.button(label="✅ Quick Win", style=discord.ButtonStyle.secondary)
    async def force_win_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not ACTIVE_SLIPS:
            await interaction.response.send_message("ℹ️ No active slip to settle!", ephemeral=True)
            return
        slip = ACTIVE_SLIPS.pop(0)
        legs = slip.get("legs", [])
        record_slip_in_ledger(slip, won=True, legs_hit_count=len(legs))
        await interaction.response.send_message(
            content=f"🏆 **BOOM! Settled `{slip.get('title')}` as WON (`+{slip.get('potential_profit')}`) and added to Lifetime Stats!**",
            embed=build_lifetime_stats_embed(),
            view=MainDashboardView()
        )

    @discord.ui.button(label="🏦 Set Bankroll", style=discord.ButtonStyle.secondary)
    async def bank_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BankrollGoalModal())

    @discord.ui.button(label="🗑️ Clear Active", style=discord.ButtonStyle.danger)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ACTIVE_SLIPS.clear()
        await interaction.response.send_message("🗑️ **Cleared active slips!**", view=MainDashboardView())


class SmartPreviewView(discord.ui.View):
    def __init__(self, parsed_slip, is_already_over=False, already_won=False, legs_hit=0):
        super().__init__(timeout=600)
        self.parsed_slip = parsed_slip
        self.is_already_over = is_already_over
        self.already_won = already_won
        self.legs_hit = legs_hit

    @discord.ui.button(label="✅ Confirm & Lock In / Save Result", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        global TRACKING_CHANNEL_ID
        TRACKING_CHANNEL_ID = interaction.channel_id
        self.clear_items()

        if self.is_already_over:
            ledger, net_change = record_slip_in_ledger(self.parsed_slip, self.already_won, self.legs_hit)
            res_word = f"🏆 WON (+{self.parsed_slip.get('potential_profit')})" if self.already_won else f"💔 LOST ({net_change:,.1f})"
            await interaction.response.edit_message(
                content=f"📥 **Finished Slip Auto-Graded as {res_word} & Added to Lifetime Stats!**",
                embed=build_lifetime_stats_embed(),
                view=MainDashboardView()
            )
            return

        slip_obj = dict(self.parsed_slip)
        slip_obj["id"] = f"slip_{int(datetime.now().timestamp())}"
        slip_obj["notified_kickoff"] = set()
        slip_obj["notified_achieved"] = set(slip_obj.get("pre_achieved_indices", []))
        slip_obj["notified_halftime"] = set()
        slip_obj["notified_late"] = set()
        slip_obj["notified_final"] = set(slip_obj.get("pre_final_indices", []))
        ACTIVE_SLIPS.append(slip_obj)

        legs = slip_obj.get("legs", [])
        done_now = len(slip_obj["notified_achieved"])
        embed = discord.Embed(
            title=f"🔥 LOCKED IN! Tracking {slip_obj.get('title')}",
            description=(
                f"**Odds:** `{slip_obj.get('odds')}x`  •  **Stake:** `{slip_obj.get('stake')}`  •  **Profit:** `+{slip_obj.get('potential_profit')}`\n"
                f"**Progress:** {render_progress_bar(done_now, len(legs))}\n\n"
                f"🔔 **Multi-Sport Auto-Alerts Active:**\n"
                f"• ⚽🏏🥊🎮 **Live Score, Round & Over Updates**\n"
                f"• 🎉 **Instant Leg Hit Pings (1 done, {max(0, len(legs)-1)} to go!)**\n"
                f"• 🏆 **Auto Settlement & Lifetime Stats Backup**"
            ),
            color=0x2ECC71
        )
        for i, leg in enumerate(legs, 1):
            sp_ic = SPORT_ICONS.get(leg.get("sport", "football"), "⚽")
            pre_st = "✅ Already Hit!" if (i - 1) in slip_obj["notified_achieved"] else f"🕒 {leg.get('kickoff_bst', 'Today BST')}"
            embed.add_field(
                name=f"{sp_ic} Leg {i}: {leg.get('home_team')} vs {leg.get('away_team')}",
                value=f"Pick: **`{leg.get('label')}`**  •  {pre_st}",
                inline=False
            )
        await interaction.response.edit_message(content=None, embed=embed, view=MainDashboardView())

    @discord.ui.button(label="✏️ Edit Stake / Odds", style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditSlipModal(self.parsed_slip, self))

    @discord.ui.button(label="✖ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.clear_items()
        await interaction.response.edit_message(content="❌ **Cancelled.** Drop a screenshot or type a match anytime!", embed=None, view=self)

# =====================================================================
# 9. SMART PREVIEW & FINISHED-MATCH AUTO-GRADER
# =====================================================================
async def build_smart_preview_card(parsed):
    feeds = await fetch_all_sports_feeds()
    legs = parsed.get("legs", [])

    all_finished = True
    any_failed = False
    legs_hit = 0
    pre_achieved = []
    pre_final = []
    leg_Blocks = []

    for idx, leg in enumerate(legs):
        ev = match_event_multi_sport(leg, feeds)
        res = evaluate_leg_multi_sport(leg, ev)
        sp_ic = res["sport_icon"]

        if res["achieved"]:
            legs_hit += 1
            pre_achieved.append(idx)
            leg["achieved"] = True
            st_badge = f"✅ **ALREADY HIT!** ({res['score']})"
        elif res["impossible"]:
            any_failed = True
            st_badge = f"❌ **ALREADY MISSED** ({res['score']} — FT)"
        elif res["is_final"]:
            st_badge = f"🏁 **Finished** ({res['score']})"
        elif res["status"] not in ("Scheduled", "Scheduled / Custom"):
            all_finished = False
            st_badge = f"🟢 **LIVE NOW:** {res['score']} (`{res['clock_display']}`)"
        else:
            all_finished = False
            st_badge = f"🕒 Scheduled: {leg.get('kickoff_bst', 'Today BST')}"

        if res["is_final"]:
            pre_final.append(idx)
        else:
            all_finished = False

        leg_Blocks.append(( f"{sp_ic} Leg {idx+1}: {leg.get('home_team')} vs {leg.get('away_team')}", f"• **Pick:** `{leg.get('label')}`\n• **Status:** {st_badge}" ))

    parsed["pre_achieved_indices"] = pre_achieved
    parsed["pre_final_indices"] = pre_final

    is_over = (all_finished and len(legs) > 0) or any_failed
    won = (legs_hit == len(legs)) and not any_failed

    if is_over:
        verdict = f"🏆 **ALL LEGS WON! (+{parsed.get('potential_profit')})**" if won else f"💔 **SLIP LOST (-{parsed.get('stake')})**"
        title = f"🏁 Finished Match Detected! — {parsed.get('title')}"
        desc = (
            f"{verdict}\n"
            f"**Odds:** `{parsed.get('odds')}x`  •  **Stake:** `{parsed.get('stake')}`  •  **Legs Hit:** `{legs_hit}/{len(legs)}`\n\n"
            f"👉 **Tap `[✅ Confirm & Lock In / Save Result]` below to add this result to your Lifetime Stats!**"
        )
        color = 0x2ECC71 if won else 0xE74C3C
    else:
        title = f"🎟️ Slip Preview — {parsed.get('title')}"
        desc = (
            f"**Legs:** `{len(legs)}`  •  **Odds:** `{parsed.get('odds')}x`  •  **Stake:** `{parsed.get('stake')}`  •  **To Win:** `+{parsed.get('potential_profit')}`\n"
            + (f"🔥 **`{legs_hit}/{len(legs)}` legs are ALREADY hit!**\n\n" if legs_hit > 0 else "\n")
            + "👉 **Tap `[✅ Confirm & Lock In]` to start live tracking (or `[✏️ Edit Stake / Odds]`)!**"
        )
        color = 0x5865F2

    embed = discord.Embed(title=title, description=desc, color=color)
    for name, val in leg_Blocks:
        embed.add_field(name=name, value=val, inline=False)
    embed.set_footer(text=f"Checked at {now_bst_str()}")
    return embed, is_over, won, legs_hit

# =====================================================================
# 10. DISCORD EVENTS & MESSAGE HANDLER
# =====================================================================
@bot.event
async def on_ready():
    print(f"🚀 Multi-Sport Ultimate Tracker Online as {bot.user}", flush=True)
    if not autonomous_loop.is_running():
        autonomous_loop.start()

@bot.event
async def on_message(message):
    global TRACKING_CHANNEL_ID
    if message.author.bot:
        return

    if message.attachments:
        for att in message.attachments:
            if any(att.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                TRACKING_CHANNEL_ID = message.channel.id
                wait_msg = await message.channel.send("🔍 **Scanning your slip & checking live/finished scores...**")
                img_bytes = await att.read()

                parsed = call_gemini_parser(text_input=message.content, image_bytes=img_bytes)
                if not parsed or not parsed.get("legs"):
                    ocr_text = await extract_text_via_free_ocr(img_bytes)
                    if ocr_text:
                        parsed = call_gemini_parser(text_input=ocr_text) or smart_universal_text_parser(ocr_text)

                if parsed and parsed.get("legs"):
                    embed, is_over, won, hits = await build_smart_preview_card(parsed)
                    view = SmartPreviewView(parsed, is_already_over=is_over, already_won=won, legs_hit=hits)
                    await wait_msg.edit(content=None, embed=embed, view=view)
                    return
                else:
                    await wait_msg.edit(content="⚠️ Couldn't read the match names clearly from that image. Try cropping closer or type the match!")
                    return

    content = message.content.strip()
    if content and not content.startswith("!"):
        low = content.lower()
        if low in ["stats", "my stats", "lifetime"]:
            await message.channel.send(embed=build_lifetime_stats_embed(), view=MainDashboardView())
            return
        if low in ["active", "matches", "live", "status"]:
            embed = await build_active_matches_embed()
            await message.channel.send(embed=embed, view=MainDashboardView())
            return
        if any(k in low for k in ["vs", "-vs-", " v ", "over", "under", "win", "btts", "ufc", "cricket"]):
            TRACKING_CHANNEL_ID = message.channel.id
            parsed = call_gemini_parser(text_input=content) or smart_universal_text_parser(content)
            if parsed and parsed.get("legs"):
                embed, is_over, won, hits = await build_smart_preview_card(parsed)
                view = SmartPreviewView(parsed, is_already_over=is_over, already_won=won, legs_hit=hits)
                await message.channel.send(embed=embed, view=view)
                return

    await bot.process_commands(message)

# =====================================================================
# 11. 60-SECOND AUTONOMOUS MULTI-SPORT TRACKING LOOP
# =====================================================================
@tasks.loop(seconds=60)
async def autonomous_loop():
    if not ACTIVE_SLIPS or not TRACKING_CHANNEL_ID:
        return

    channel = bot.get_channel(TRACKING_CHANNEL_ID)
    if not channel:
        return

    try:
        feeds = await fetch_all_sports_feeds()
        slips_to_remove = []

        for slip in ACTIVE_SLIPS:
            s_id = slip["id"]
            legs = slip.get("legs", [])
            total_legs = len(legs)
            notified_ko = slip["notified_kickoff"]
            notified_ach = slip["notified_achieved"]
            notified_ht = slip["notified_halftime"]
            notified_late = slip["notified_late"]
            notified_ft = slip["notified_final"]

            leg_results = []
            all_finished = True
            any_failed = False
            failed_leg_info = None

            for idx, leg in enumerate(legs):
                ev = match_event_multi_sport(leg, feeds)
                res = evaluate_leg_multi_sport(leg, ev)
                leg_results.append(res)

                if not res["is_final"]:
                    all_finished = False
                if res["impossible"]:
                    any_failed = True
                    failed_leg_info = (idx + 1, leg, res)

                sp_ic = res["sport_icon"]

                if res["status"] not in ("Scheduled", "Scheduled / Custom") and not res["is_final"] and idx not in notified_ko:
                    notified_ko.add(idx)
                    ko_embed = discord.Embed(
                        title=f"🟢 LIVE NOW — {res['score']}",
                        description=(
                            f"{sp_ic} **Pick:** `{leg.get('label')}`\n"
                            f"• **Status:** `{res['clock_display']}`\n"
                            f"• **Win Meter:** {render_win_meter(res['likelihood_pct'])}"
                        ),
                        color=0x2ECC71
                    )
                    await channel.send(embed=ko_embed, view=MainDashboardView())

                state_key = (s_id, idx)
                prev_score = LAST_STATE.get(state_key)
                if prev_score is not None and prev_score != res["score"] and leg.get("sport", "football") == "football":
                    g_embed = discord.Embed(
                        title=f"⚽ GOAL! Score Update ({res['clock_display']})",
                        description=(
                            f"### {res['score']}\n"
                            f"• **Pick:** `{leg.get('label')}`\n"
                            f"• **Situation:** **{res['requirement']}**\n"
                            f"• **Win Meter:** {render_win_meter(res['likelihood_pct'])}"
                        ),
                        color=0x00A8FF
                    )
                    await channel.send(embed=g_embed, view=MainDashboardView())
                LAST_STATE[state_key] = res["score"]

                if res["achieved"] and idx not in notified_ach:
                    notified_ach.add(idx)
                    leg["achieved"] = True
                    done = len(notified_ach)
                    left = total_legs - done
                    headline = f"🎉 {done} Leg Hit, {left} to Go!" if left > 0 else "🎉 Every Leg Hit!"
                    hit_embed = discord.Embed(
                        title=f"✅ {sp_ic} LEG LOCKED IN! — {headline}",
                        description=(
                            f"**Match:** {res['score']} (`{res['clock_display']}`)\n"
                            f"**Pick Hit:** `{leg.get('label')}`\n\n"
                            f"**Parlay Progress:**\n{render_progress_bar(done, total_legs)}"
                        ),
                        color=0x2ECC71
                    )
                    await channel.send(embed=hit_embed, view=MainDashboardView())

                if res["is_mid"] and idx not in notified_ht and not res["achieved"]:
                    notified_ht.add(idx)
                    comb = 1.0
                    for r in leg_results:
                        comb *= (r["likelihood_pct"] / 100.0)
                    ht_embed = discord.Embed(
                        title=f"⏱️ Mid-Match Update ({res['clock_display']}) — {leg.get('home_team')} vs {leg.get('away_team')}",
                        description=(
                            f"**Current State:** `{res['score']}`\n"
                            f"**What You Need:** **{res['requirement']}**\n\n"
                            f"**Leg Win Meter:** {render_win_meter(res['likelihood_pct'])}\n"
                            f"**Game Read:** 💬 *{res['analysis']}*\n"
                            + (f"\n**Full Parlay Chance:** {render_win_meter(int(round(comb*100)))}" if total_legs > 1 else "")
                        ),
                        color=0xF39C12
                    )
                    await channel.send(embed=ht_embed, view=MainDashboardView())

                elif res["is_late"] and idx not in notified_late and not res["achieved"]:
                    notified_late.add(idx)
                    comb = 1.0
                    for r in leg_results:
                        comb *= (r["likelihood_pct"] / 100.0)
                    late_embed = discord.Embed(
                        title=f"🔥 Crunch-Time Sweat ({res['clock_display']}) — {leg.get('home_team')} vs {leg.get('away_team')}",
                        description=(
                            f"**Current State:** `{res['score']}`\n"
                            f"**What You Need:** **{res['requirement']}**\n\n"
                            f"**Leg Win Meter:** {render_win_meter(res['likelihood_pct'])}\n"
                            f"**Game Read:** 💬 *{res['analysis']}*\n"
                            + (f"\n**Full Parlay Chance:** {render_win_meter(int(round(comb*100)))}" if total_legs > 1 else "")
                        ),
                        color=0xE67E22
                    )
                    await channel.send(embed=late_embed, view=MainDashboardView())

                if res["is_final"] and idx not in notified_ft:
                    notified_ft.add(idx)

            all_cleared = (len(notified_ach) == total_legs)
            if any_failed or all_cleared or (all_finished and len(notified_ft) == total_legs):
                won = all_cleared and not any_failed
                ledger, net_change = record_slip_in_ledger(slip, won, len(notified_ach))

                if won:
                    end_title = f"🏆 BOOM! CASHED IT! (+{slip.get('potential_profit')})"
                    end_sub = f"Every single leg in **{slip.get('title')}** hit! 🔥"
                    end_color = 0x2ECC71
                else:
                    leg_num, f_leg, f_res = failed_leg_info if failed_leg_info else (1, legs[0], leg_results[0])
                    end_title = f"💔 Slip Busted — Leg {leg_num} Missed ({net_change:,.1f})"
                    end_sub = f"**{f_res['score']}** — `{f_leg.get('label')}` missed."
                    end_color = 0xE74C3C

                final_embed = discord.Embed(
                    title=end_title,
                    description=f"{end_sub}\n**Legs Hit:** {render_progress_bar(len(notified_ach), total_legs)}",
                    color=end_color
                )
                await channel.send(embed=final_embed)
                await channel.send(embed=build_lifetime_stats_embed(), view=MainDashboardView())
                slips_to_remove.append(slip)

        for r_slip in slips_to_remove:
            if r_slip in ACTIVE_SLIPS:
                ACTIVE_SLIPS.remove(r_slip)

    except Exception as e:
        print(f"Loop error: {e}", flush=True)

# =====================================================================
# 12. KEEP-ALIVE SERVER FOR 24/7 CLOUD HOSTING
# =====================================================================
async def health_check(request):
    return web.Response(text="⚡ Multi-Sport Ultimate Tracker is 24/7 Online!")

async def main():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Cloud Keep-Alive Server listening on port {PORT}", flush=True)
    await bot.start(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
