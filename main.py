"""
Clean, Tested & Fixed Multi-Bet Discord Tracker
Fixes:
1. FIXED 0-0 Score Bug: Reads `event["competitions"][0]["competitors"]` so real live scores & goal alerts work 100%.
2. FIXED Multi-Bet / Bet History Paste: Splits separate bets by `ID:`, `Stake:`, `Single`, `Mix Parlay`, or blank blocks instead of lumping them into 1 giant parlay.
3. FIXED Past Match Lookup: Fetches Yesterday + Today + Tomorrow scores so past bets in your history are graded with real final scores.
4. FIXED Messy Formatting: Ultra-clean, short, easy-to-read cards with zero clutter.
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
LAST_SCORES = {}  # (slip_id, leg_idx) -> (home_score, away_score)

LEDGER_FILE = "ledger.json"

# =====================================================================
# 2. SIMPLE LIFETIME & DAILY STATS LEDGER
# =====================================================================
def load_ledger():
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_bets": 0,
        "won": 0,
        "lost": 0,
        "total_staked": 0.0,
        "net_profit": 0.0,
        "biggest_win": 0.0,
        "streak": 0,
        "best_streak": 0,
        "history": []
    }

def save_ledger(data):
    try:
        with open(LEDGER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Ledger error: {e}")

def record_bet(slip, won):
    stake = float(slip.get("stake", 100.0))
    profit = float(slip.get("potential_profit", 100.0))
    net = profit if won else -stake

    ledger = load_ledger()
    ledger["total_bets"] = ledger.get("total_bets", 0) + 1
    ledger["total_staked"] = ledger.get("total_staked", 0.0) + stake
    ledger["net_profit"] = ledger.get("net_profit", 0.0) + net

    if won:
        ledger["won"] = ledger.get("won", 0) + 1
        ledger["streak"] = ledger.get("streak", 0) + 1
        if ledger["streak"] > ledger.get("best_streak", 0):
            ledger["best_streak"] = ledger["streak"]
        if profit > ledger.get("biggest_win", 0.0):
            ledger["biggest_win"] = profit
    else:
        ledger["lost"] = ledger.get("lost", 0) + 1
        ledger["streak"] = 0

    ledger["history"].append({
        "date": slip.get("date", today_bst_date()),
        "title": slip.get("title", "Bet"),
        "won": won,
        "net": round(net, 2)
    })
    save_ledger(ledger)
    return ledger, net

# =====================================================================
# 3. LIVE & HISTORICAL SCORE ENGINE (FIXED `competitions[0]`)
# =====================================================================
async def fetch_scores_multi_day():
    """Fetches scores for Yesterday, Today, and Tomorrow so both live & past bets work."""
    now_utc = datetime.now(timezone.utc)
    days = [
        (now_utc - timedelta(days=2)).strftime("%Y%m%d"),
        (now_utc - timedelta(days=1)).strftime("%Y%m%d"),
        now_utc.strftime("%Y%m%d"),
        (now_utc + timedelta(days=1)).strftime("%Y%m%d"),
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    events = []

    async with aiohttp.ClientSession() as session:
        for d in days:
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/scorepanel?dates={d}"
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        for group in data.get("scores", []):
                            events.extend(group.get("events", []))
            except Exception as e:
                print(f"Error fetching {d}: {e}")
    return events

def clean_team_tokens(name):
    s = name.lower().strip()
    for suffix in [" fc", " afc", " united", " city", " hotspur", " athletic", " bilbao", " club", " wanderers", " albion", " forest", " town", " sc", " cf"]:
        if len(s) > 6:
            s = s.replace(suffix, "").strip()
    return s

def find_match_event(leg, events):
    t1 = leg.get("home_team", "").lower().strip()
    t2 = leg.get("away_team", "").lower().strip()
    t1_short = clean_team_tokens(t1)
    t2_short = clean_team_tokens(t2)

    # 1. Exact or cleaned match on BOTH teams
    for ev in events:
        name = ev.get("name", "").lower()
        m1 = (t1 and t1 in name) or (t1_short and len(t1_short) >= 3 and t1_short in name)
        m2 = (t2 and t2 in name) or (t2_short and len(t2_short) >= 3 and t2_short in name)
        if m1 and m2:
            return ev

    # 2. First word match on both teams (e.g., "Athletic Bilbao" -> "athletic" & "Osasuna" -> "osasuna")
    w1 = t1.split()[0] if t1.split() else ""
    w2 = t2.split()[0] if t2.split() else ""
    for ev in events:
        name = ev.get("name", "").lower()
        if len(w1) >= 4 and len(w2) >= 4 and w1 in name and w2 in name:
            return ev

    return None

def poisson_prob(k_needed, expected_goals):
    if k_needed <= 0:
        return 1.0
    if expected_goals <= 0:
        return 0.0
    prob_less = sum((expected_goals ** i) * math.exp(-expected_goals) / math.factorial(i) for i in range(k_needed))
    return max(0.03, min(0.97, 1.0 - prob_less))

def evaluate_leg(leg, event):
    """Correctly reads `event['competitions'][0]['competitors']` from ESPN."""
    if not event:
        return {
            "found": False,
            "status": "Scheduled",
            "score": f"{leg.get('home_team')} vs {leg.get('away_team')}",
            "home_score": 0, "away_score": 0,
            "home_name": leg.get("home_team"), "away_name": leg.get("away_team"),
            "minute": 0, "clock": "Scheduled",
            "is_ht": False, "is_late": False, "is_ft": False,
            "won": False, "lost": False,
            "chance": 55,
            "need_text": "Waiting for kickoff",
            "short_read": "Match has not started yet."
        }

    # IMPORTANT FIX: ESPN puts competitors and status inside competitions[0]
    comp = (event.get("competitions") or [event])[0]
    st = comp.get("status") or event.get("status") or {}
    st_type = st.get("type", {})
    desc = st_type.get("description", "Scheduled")
    state = st_type.get("state", "pre")  # "pre", "in", "post"
    clock_str = st.get("displayClock", "0'")

    minute = 0
    try:
        minute = int(clock_str.replace("'", "").split("+")[0])
    except Exception:
        minute = 45 if "Halftime" in desc else (90 if state == "post" else 0)

    comps = comp.get("competitors", [])
    home_score, away_score = 0, 0
    home_name = leg.get("home_team", "Home")
    away_name = leg.get("away_team", "Away")

    for c in comps:
        s_val = int(c.get("score") or 0)
        team_obj = c.get("team", {})
        n_val = team_obj.get("displayName") or c.get("displayName") or ""
        if c.get("homeAway") == "home":
            home_score = s_val
            if n_val: home_name = n_val
        else:
            away_score = s_val
            if n_val: away_name = n_val

    is_ft = (state == "post") or ("Final" in desc) or (desc in ("Full Time", "FT"))
    is_ht = ("Halftime" in desc) or (desc == "HT") or (state == "in" and 45 <= minute <= 48)
    is_late = (state == "in" and 70 <= minute <= 76)
    rem_mins = max(0, 90 - minute)
    exp_goals = rem_mins * 0.032

    t_type = leg.get("target_type", "over_goals")
    t_val = float(leg.get("target_value", 0.5))

    won, lost = False, False
    chance = 50
    need_text = ""
    short_read = ""

    if t_type == "team_over":
        t_team = clean_team_tokens(leg.get("target_team", home_name))
        my_score = away_score if (t_team and t_team in away_name.lower()) else home_score
        my_name = away_name if (t_team and t_team in away_name.lower()) else home_name
        needed = math.ceil(t_val) - my_score

        if needed <= 0:
            won, chance = True, 100
            need_text = "WON ✅"
            short_read = f"{my_name} scored ({my_score})!"
        elif is_ft:
            lost, chance = True, 0
            need_text = f"LOST ❌ ({my_name} scored {my_score})"
            short_read = f"Finished {home_score}-{away_score}. Needed {needed} more goal from {my_name}."
        else:
            chance = int(round(poisson_prob(needed, rem_mins * 0.017) * 100))
            need_text = f"Need {needed} goal from {my_name} ({rem_mins}m left)"
            short_read = f"{my_name} needs {needed} goal in {rem_mins} mins."

    elif t_type == "over_goals":
        total = home_score + away_score
        needed = math.ceil(t_val) - total
        if needed <= 0:
            won, chance = True, 100
            need_text = "WON ✅"
            short_read = f"Total is {total} goals — target hit!"
        elif is_ft:
            lost, chance = True, 0
            need_text = f"LOST ❌ (Finished {total} goals)"
            short_read = f"Ended {home_score}-{away_score} ({total} total goals)."
        else:
            chance = int(round(poisson_prob(needed, exp_goals) * 100))
            need_text = f"Need {needed} more goal in {rem_mins}m"
            short_read = f"{needed} more goal needed in {rem_mins} mins."

    elif t_type == "under_goals":
        total = home_score + away_score
        limit = math.floor(t_val)
        if total > limit:
            lost, chance = True, 0
            need_text = f"LOST ❌ ({total} goals scored)"
            short_read = f"Too many goals ({total})."
        elif is_ft:
            won, chance = True, 100
            need_text = "WON ✅"
            short_read = f"Finished {home_score}-{away_score} (Under {t_val} held!)."
        else:
            max_allowed = limit - total
            chance = int(round((1.0 - poisson_prob(max_allowed + 1, exp_goals)) * 100))
            need_text = f"Max {max_allowed} more goal allowed ({rem_mins}m left)"
            short_read = f"Stay under {t_val} goals for {rem_mins} mins."

    elif t_type == "team_win":
        t_team = clean_team_tokens(leg.get("target_team", home_name))
        is_home = not (t_team and t_team in away_name.lower())
        my_name = home_name if is_home else away_name
        lead = (home_score - away_score) if is_home else (away_score - home_score)

        if is_ft:
            if lead > 0:
                won, chance = True, 100
                need_text = "WON ✅"
                short_read = f"{my_name} won {home_score}-{away_score}!"
            else:
                lost, chance = True, 0
                need_text = "LOST ❌"
                short_read = f"Finished {home_score}-{away_score}. {my_name} didn't win."
        else:
            if lead > 0:
                chance = min(95, int(72 + 22 * (minute / 90.0) + 8 * (lead - 1)))
                need_text = f"{my_name} leading by {lead} ({rem_mins}m left)"
                short_read = f"{my_name} is up by {lead} with {rem_mins} mins left."
            elif lead == 0:
                chance = max(18, int(45 - 20 * (minute / 90.0)))
                need_text = f"Tied! Need 1 goal from {my_name} ({rem_mins}m left)"
                short_read = f"Level at {home_score}-{away_score}. {my_name} needs 1 goal to lead."
            else:
                needed = abs(lead) + 1
                chance = int(round(poisson_prob(needed, exp_goals) * 100))
                need_text = f"Down {abs(lead)}! Need {needed} goals ({rem_mins}m left)"
                short_read = f"{my_name} trailing by {abs(lead)}, needs {needed} goals in {rem_mins} mins."

    clock_label = "FT" if is_ft else ("HT" if is_ht else (f"{minute}'" if state == "in" else "Scheduled"))
    return {
        "found": True,
        "state": state,
        "status": desc,
        "score": f"{home_name} {home_score} - {away_score} {away_name}",
        "home_score": home_score, "away_score": away_score,
        "home_name": home_name, "away_name": away_name,
        "minute": minute, "clock": clock_label,
        "is_ht": is_ht, "is_late": is_late, "is_ft": is_ft,
        "won": won, "lost": lost,
        "chance": chance,
        "need_text": need_text,
        "short_read": short_read
    }

# =====================================================================
# 4. MULTI-BET / HISTORY PARSER (SPLITS MULTIPLE BETS PROPERLY!)
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
        print(f"OCR error: {e}")
    return ""

def parse_single_slip_block(block_text):
    """Parses a single bet block (either a Single bet or 1 Mix Parlay)."""
    lines = [l.strip() for l in block_text.replace("\r", "").split("\n") if l.strip()]
    legs = []
    pending_target = None
    odds_val = 0.0
    stake_val = 100.0
    profit_val = 0.0
    explicit_won = None  # If history explicitly says "Won" or "Lost"
    bdt_numbers = []

    for line in lines:
        low = line.lower()
        if low in ("won", "win", "settled - won"):
            explicit_won = True
        elif low in ("lost", "lose", "loss", "settled - lost"):
            explicit_won = False

        if "bdt" in low:
            m = re.search(r"([+-]?[\d,]+\.\d+|[+-]?\d+)", line)
            if m:
                bdt_numbers.append(float(m.group(1).replace(",", "")))

        if "odds:" in low:
            m = re.search(r"(\d+(?:\.\d+)?)", line)
            if m: odds_val = float(m.group(1))
        elif "stake:" in low or "risk:" in low:
            m = re.search(r"([\d,]+\.\d+|\d+)", line)
            if m: stake_val = float(m.group(1).replace(",", ""))
        elif "potential winnings" in low or "win/loss" in low or "return" in low:
            m = re.search(r"([+-]?[\d,]+\.\d+|[+-]?\d+)", line)
            if m:
                val = float(m.group(1).replace(",", ""))
                profit_val = abs(val)
                if val < 0:
                    explicit_won = False

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
                    if home.lower() in c_low or away.lower() in c_low:
                        t_type = "team_over"
                        t_team = away if away.lower() in c_low else home
                    else:
                        t_type = "over_goals"
                elif "under" in c_low:
                    t_type = "under_goals"
                else:
                    t_type = "team_win"
                    t_team = away if away.lower() in c_low else home

                label = cond if pending_target else f"{t_team} Win"
                legs.append({
                    "home_team": home,
                    "away_team": away,
                    "target_team": t_team,
                    "target_type": t_type,
                    "target_value": t_val,
                    "label": label
                })
                pending_target = None

    if not legs:
        return None

    if len(bdt_numbers) >= 2 and profit_val <= 0:
        stake_val = abs(bdt_numbers[0])
        profit_val = abs(bdt_numbers[1])
        if bdt_numbers[1] < 0:
            explicit_won = False

    if odds_val <= 1.0:
        odds_val = round(1.35 ** len(legs), 2)
    if profit_val <= 0:
        profit_val = round(stake_val * (odds_val - 1.0), 2)

    title = f"{len(legs)}-Leg Parlay" if len(legs) > 1 else f"{legs[0]['home_team']} vs {legs[0]['away_team']}"
    return {
        "title": title,
        "odds": odds_val,
        "stake": stake_val,
        "potential_profit": profit_val,
        "explicit_won": explicit_won,
        "legs": legs
    }

def parse_all_slips_from_text(raw_text):
    """
    Splits pasted Bet History into separate slips instead of lumping everything into 1 parlay!
    Detects 'ID:' headers OR footer boundaries (Stake / Total Risk / BDT) before the next bet starts.
    """
    lines = [l.strip() for l in raw_text.replace("\r", "").split("\n") if l.strip()]
    blocks = []
    curr_lines = []
    has_match = False
    in_footer = False

    for line in lines:
        low = line.lower()
        is_id_header = bool(re.search(r"\bID\s*[:#]?\s*\d{5,}", line, flags=re.IGNORECASE)) or ("mix parlay" in low and has_match)
        is_footer_line = any(k in low for k in ["stake:", "risk:", "total risk", "potential winnings", "win/loss"])
        is_new_match_start = ("soccer match" in low or "@" in line or "-vs-" in low or " vs " in low) and not is_footer_line

        if has_match and (is_id_header or (in_footer and is_new_match_start)):
            blocks.append("\n".join(curr_lines))
            curr_lines = [line]
            has_match = ("-vs-" in low or " vs " in low)
            in_footer = False
        else:
            curr_lines.append(line)
            if "-vs-" in low or " vs " in low or " v " in low:
                has_match = True
            if is_footer_line and has_match:
                in_footer = True

    if curr_lines:
        blocks.append("\n".join(curr_lines))

    slips = [parse_single_slip_block(b) for b in blocks if b.strip()]
    return [s for s in slips if s]

# =====================================================================
# 5. ULTRA-CLEAN, EASY-TO-READ DISCORD CARDS
# =====================================================================
def build_stats_embed():
    ledger = load_ledger()
    total = ledger.get("total_bets", 0)
    won = ledger.get("won", 0)
    lost = ledger.get("lost", 0)
    win_pct = round((won / total * 100), 1) if total > 0 else 0.0
    net = ledger.get("net_profit", 0.0)
    staked = ledger.get("total_staked", 0.0)
    roi = round((net / staked * 100), 1) if staked > 0 else 0.0

    today_str = today_bst_date()
    today_items = [h for h in ledger.get("history", []) if h.get("date") == today_str]
    t_won = sum(1 for h in today_items if h.get("won"))
    t_lost = len(today_items) - t_won
    t_net = sum(h.get("net", 0.0) for h in today_items)

    embed = discord.Embed(
        title="🏆 My Stats (Lifetime & Today)",
        color=0x2ECC71 if net >= 0 else 0xE74C3C
    )
    embed.add_field(
        name="📊 Lifetime Totals",
        value=(
            f"**Record:** `{won} Won` - `{lost} Lost` ({win_pct}%)\n"
            f"**Profit/Loss:** **`{net:+,.1f}`**\n"
            f"**ROI:** `{roi}%`  •  **Streak:** 🔥 `{ledger.get('streak', 0)}`"
        ),
        inline=False
    )
    embed.add_field(
        name=f"📅 Today ({today_str})",
        value=f"**Record:** `{t_won} Won` - `{t_lost} Lost`  •  **Today's Profit:** **`{t_net:+,.1f}`**",
        inline=False
    )
    recent = ledger.get("history", [])[-5:]
    if recent:
        lines = [f"{'✅' if h['won'] else '❌'} **{h['title']}** (`{h['net']:+,.1f}`)" for h in reversed(recent)]
        embed.add_field(name="🕒 Last 5 Bets", value="\n".join(lines), inline=False)
    return embed

async def build_active_embed():
    if not ACTIVE_SLIPS:
        return discord.Embed(
            title="🔥 Active Bets",
            description="No active bets right now.\nDrop a screenshot or paste a match to track!",
            color=0x95A5A6
        )

    events = await fetch_scores_multi_day()
    embed = discord.Embed(title=f"🔥 Active Bets ({len(ACTIVE_SLIPS)})", color=0x3498DB)

    for idx, slip in enumerate(ACTIVE_SLIPS, 1):
        legs = slip.get("legs", [])
        lines = []
        comb = 1.0
        hit_count = 0

        for l_i, leg in enumerate(legs, 1):
            ev = find_match_event(leg, events)
            res = evaluate_leg(leg, ev)
            comb *= (res["chance"] / 100.0)
            if res["won"]: hit_count += 1
            ic = "✅" if res["won"] else ("❌" if res["lost"] else "🟢" if res.get("state") == "in" else "🕒")
            lines.append(
                f"{ic} **{res['score']}** (`{res['clock']}`)\n"
                f"Pick: `{leg['label']}` → **{res['need_text']}** (`{res['chance']}%`)"
            )

        title_line = f"#{idx} {slip['title']} ({hit_count}/{len(legs)} Hit • To Win: +{slip['potential_profit']})"
        body = "\n".join(lines)
        if len(legs) > 1:
            body += f"\n**Parlay Win Chance:** `{int(round(comb * 100))}%`"
        embed.add_field(name=title_line, value=body, inline=False)

    embed.set_footer(text=f"Updated {now_bst_str()}")
    return embed

# =====================================================================
# 6. SIMPLE BUTTONS
# =====================================================================
class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔥 Active Bets", style=discord.ButtonStyle.primary)
    async def active_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        embed = await build_active_embed()
        await interaction.followup.send(embed=embed, view=DashboardView())

    @discord.ui.button(label="🏆 Lifetime Stats", style=discord.ButtonStyle.success)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_stats_embed(), view=DashboardView())

    @discord.ui.button(label="🗑️ Reset Stats", style=discord.ButtonStyle.danger)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ACTIVE_SLIPS.clear()
        save_ledger({"total_bets": 0, "won": 0, "lost": 0, "total_staked": 0.0, "net_profit": 0.0, "biggest_win": 0.0, "streak": 0, "best_streak": 0, "history": []})
        await interaction.response.send_message("🔄 **Stats & Active Bets Reset to 0!**", embed=build_stats_embed(), view=DashboardView())


class ConfirmView(discord.ui.View):
    def __init__(self, slips_list, evaluated_slips):
        super().__init__(timeout=600)
        self.slips_list = slips_list
        self.evaluated_slips = evaluated_slips  # list of (slip, is_over, won, hit_count)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        global TRACKING_CHANNEL_ID
        TRACKING_CHANNEL_ID = interaction.channel_id
        self.clear_items()

        saved_finished = 0
        started_live = 0

        for slip, is_over, won, hit_count in self.evaluated_slips:
            if is_over:
                record_bet(slip, won)
                saved_finished += 1
            else:
                s_obj = dict(slip)
                s_obj["id"] = f"s_{int(datetime.now().timestamp())}_{started_live}"
                s_obj["notified_ach"] = set(slip.get("pre_hit_indices", []))
                s_obj["notified_ht"] = set()
                s_obj["notified_late"] = set()
                s_obj["notified_ft"] = set()
                ACTIVE_SLIPS.append(s_obj)
                started_live += 1

        msg_parts = []
        if saved_finished > 0:
            msg_parts.append(f"✅ **Saved `{saved_finished}` finished bet(s) into your Lifetime Stats!**")
        if started_live > 0:
            msg_parts.append(f"🔥 **Tracking `{started_live}` live/upcoming bet(s)!**")

        await interaction.response.edit_message(
            content="\n".join(msg_parts),
            embed=build_stats_embed() if saved_finished > 0 else await build_active_embed(),
            view=DashboardView()
        )

    @discord.ui.button(label="✖ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.clear_items()
        await interaction.response.edit_message(content="❌ **Cancelled.**", embed=None, view=None)

# =====================================================================
# 7. PREVIEW & AUTO-CHECKER
# =====================================================================
async def evaluate_uploaded_slips(slips_list):
    events = await fetch_scores_multi_day()
    evaluated = []

    embed = discord.Embed(
        title=f"📋 Found {len(slips_list)} Bet(s) — Please Confirm",
        color=0x5865F2
    )

    for s_idx, slip in enumerate(slips_list, 1):
        legs = slip.get("legs", [])
        all_ft = True
        any_lost = False
        hits = 0
        pre_hits = []
        lines = []

        for l_idx, leg in enumerate(legs):
            ev = find_match_event(leg, events)
            res = evaluate_leg(leg, ev)

            if res["won"]:
                hits += 1
                pre_hits.append(l_idx)
                ic = "✅ WON"
            elif res["lost"]:
                any_lost = True
                ic = "❌ LOST"
            elif res["is_ft"]:
                ic = "🏁 FT"
            elif res.get("state") == "in":
                all_ft = False
                ic = f"🟢 LIVE {res['clock']}"
            else:
                all_ft = False
                ic = "🕒 Scheduled"

            if not res["is_ft"]:
                all_ft = False

            lines.append(f"• **{res['score']}** ({ic}) — `{leg['label']}`")

        slip["pre_hit_indices"] = pre_hits

        # Check if explicitly marked Won/Lost in pasted history or finished via API
        if slip.get("explicit_won") is not None:
            is_over = True
            won = slip["explicit_won"]
        else:
            is_over = (all_ft and len(legs) > 0) or any_lost
            won = (hits == len(legs)) and not any_lost

        if is_over:
            status_tag = f"🏆 FINISHED: WON (+{slip['potential_profit']})" if won else f"💔 FINISHED: LOST (-{slip['stake']})"
        else:
            status_tag = f"🔥 ACTIVE ({hits}/{len(legs)} Hit • To Win: +{slip['potential_profit']})"

        embed.add_field(
            name=f"Bet #{s_idx}: {slip['title']} — {status_tag}",
            value="\n".join(lines),
            inline=False
        )
        evaluated.append((slip, is_over, won, hits))

    embed.set_footer(text="Tap Confirm below to save finished bets & track live ones!")
    return embed, evaluated

# =====================================================================
# 8. DISCORD EVENTS
# =====================================================================
@bot.event
async def on_ready():
    print(f"✅ Clean Tracker Online as {bot.user}", flush=True)
    if not live_loop.is_running():
        live_loop.start()

@bot.event
async def on_message(message):
    global TRACKING_CHANNEL_ID
    if message.author.bot:
        return

    # 1. Image Upload
    if message.attachments:
        for att in message.attachments:
            if any(att.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                TRACKING_CHANNEL_ID = message.channel.id
                wait_msg = await message.channel.send("🔍 **Reading slip & checking real scores...**")
                img_bytes = await att.read()
                ocr_text = await extract_text_via_free_ocr(img_bytes)
                slips = parse_all_slips_from_text(ocr_text) if ocr_text else []

                if slips:
                    embed, evaluated = await evaluate_uploaded_slips(slips)
                    await wait_msg.edit(content=None, embed=embed, view=ConfirmView(slips, evaluated))
                    return
                else:
                    await wait_msg.edit(content="⚠️ Could not read teams from image. Try pasting the text or cropping closer!")
                    return

    # 2. Pasted Bet History or Typed Match
    content = message.content.strip()
    if content and not content.startswith("!"):
        low = content.lower()
        if low in ("stats", "my stats"):
            await message.channel.send(embed=build_stats_embed(), view=DashboardView())
            return
        if low in ("active", "live", "status"):
            await message.channel.send(embed=await build_active_embed(), view=DashboardView())
            return
        if any(k in low for k in ["vs", "-vs-", " v ", "over", "under", "stake:", "id:"]):
            TRACKING_CHANNEL_ID = message.channel.id
            slips = parse_all_slips_from_text(content)
            if slips:
                embed, evaluated = await evaluate_uploaded_slips(slips)
                await message.channel.send(embed=embed, view=ConfirmView(slips, evaluated))
                return

    await bot.process_commands(message)

# =====================================================================
# 9. 60-SECOND LIVE LOOP (REAL SCORES, GOAL ALERTS, HT & 72')
# =====================================================================
@tasks.loop(seconds=60)
async def live_loop():
    if not ACTIVE_SLIPS or not TRACKING_CHANNEL_ID:
        return

    channel = bot.get_channel(TRACKING_CHANNEL_ID)
    if not channel:
        return

    try:
        events = await fetch_scores_multi_day()
        done_slips = []

        for slip in ACTIVE_SLIPS:
            s_id = slip["id"]
            legs = slip.get("legs", [])
            total = len(legs)
            notified_ach = slip["notified_ach"]
            notified_ht = slip["notified_ht"]
            notified_late = slip["notified_late"]
            notified_ft = slip["notified_ft"]

            all_ft = True
            any_lost = False
            lost_leg_text = ""

            for idx, leg in enumerate(legs):
                ev = find_match_event(leg, events)
                res = evaluate_leg(leg, ev)

                if not res["is_ft"]:
                    all_ft = False
                if res["lost"]:
                    any_lost = True
                    lost_leg_text = f"**{res['score']} (FT)** — `{leg['label']}` missed."

                # 1. REAL-TIME GOAL ALERT
                key = (s_id, idx)
                prev = LAST_SCORES.get(key)
                curr = (res["home_score"], res["away_score"])
                if prev is not None and (curr[0] > prev[0] or curr[1] > prev[1]):
                    scorer = res["home_name"] if curr[0] > prev[0] else res["away_name"]
                    embed = discord.Embed(
                        title=f"⚽ GOAL! {scorer} ({res['clock']})",
                        description=(
                            f"### {res['score']}\n"
                            f"Pick: `{leg['label']}` → **{res['need_text']}**\n"
                            f"Win Chance: **`{res['chance']}%`**"
                        ),
                        color=0x00A8FF
                    )
                    await channel.send(embed=embed)
                LAST_SCORES[key] = curr

                # 2. LEG WON ALERT ("1 done, 3 to go")
                if res["won"] and idx not in notified_ach:
                    notified_ach.add(idx)
                    done_c = len(notified_ach)
                    left_c = total - done_c
                    msg = f"🎉 **{done_c} Done, {left_c} to Go!**" if left_c > 0 else "🎉 **All Legs Won!**"
                    embed = discord.Embed(
                        title=f"✅ Leg Hit! — {msg}",
                        description=f"**{res['score']}** (`{res['clock']}`)\nPick Hit: `{leg['label']}`",
                        color=0x2ECC71
                    )
                    await channel.send(embed=embed)

                # 3. HALF-TIME UPDATE (45')
                if res["is_ht"] and idx not in notified_ht and not res["won"]:
                    notified_ht.add(idx)
                    embed = discord.Embed(
                        title=f"⏱️ Half-Time (45') — {res['score']}",
                        description=(
                            f"Pick: `{leg['label']}`\n"
                            f"**Status:** {res['short_read']}\n"
                            f"**Win Chance:** **`{res['chance']}%`**"
                        ),
                        color=0xF39C12
                    )
                    await channel.send(embed=embed)

                # 4. LATE-MATCH UPDATE (~72')
                elif res["is_late"] and idx not in notified_late and not res["won"]:
                    notified_late.add(idx)
                    embed = discord.Embed(
                        title=f"🔥 Final 20 Mins ({res['clock']}) — {res['score']}",
                        description=(
                            f"Pick: `{leg['label']}`\n"
                            f"**Status:** {res['short_read']}\n"
                            f"**Win Chance:** **`{res['chance']}%`**"
                        ),
                        color=0xE67E22
                    )
                    await channel.send(embed=embed)

                if res["is_ft"]:
                    notified_ft.add(idx)

            # 5. SETTLE BET WHEN DONE OR BUSTED
            all_won = (len(notified_ach) == total)
            if any_lost or all_won or (all_ft and len(notified_ft) == total):
                won = all_won and not any_lost
                _, net = record_bet(slip, won)
                if won:
                    embed = discord.Embed(
                        title=f"🏆 BET WON! (+{slip['potential_profit']})",
                        description=f"**{slip['title']}** — All {total} leg(s) hit!",
                        color=0x2ECC71
                    )
                else:
                    embed = discord.Embed(
                        title=f"💔 Bet Lost ({net:,.1f})",
                        description=f"**{slip['title']}**\n{lost_leg_text}",
                        color=0xE74C3C
                    )
                await channel.send(embed=embed, view=DashboardView())
                done_slips.append(slip)

        for d in done_slips:
            if d in ACTIVE_SLIPS:
                ACTIVE_SLIPS.remove(d)

    except Exception as e:
        print(f"Loop error: {e}", flush=True)

# =====================================================================
# 10. KEEP-ALIVE WEB SERVER
# =====================================================================
async def health_check(request):
    return web.Response(text="Bot is online!")

async def main():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    await bot.start(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
