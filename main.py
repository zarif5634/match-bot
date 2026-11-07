"""
⚡ Match Intelligence Engine — 100% Zero-Type Fully Automatic Edition
Features:
- 100% Automatic Start: Upload a photo -> Starts tracking immediately (no typing or !confirm needed!)
- Built-in Free OCR + Gemini Vision: Reads screenshots automatically out of the box
- Interactive One-Tap Buttons on every card ([📡 Live Scoreboard], [📊 My Stats], [🛑 Stop])
- Broadcast-quality Discord embeds with gauges, progress bars & BD Time (BST)
- Active-Day Full Performance Stats Report (only triggers on days you play)
- Instant Parlay Failure Detection (stops & alerts immediately if a leg misses at FT)
- Zero gambling/betting/currency terminology
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
DISCORD_BOT_TOKEN = os.getenv(
    "DISCORD_BOT_TOKEN",
    "MTU2ODY3Mjg3ODkxNDcwNzczMA.GGdTbz.NhH-GU8OML_usfsQqJN0u4uES0LgYdee2ZDC34"
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # Optional: works with built-in OCR even if empty
PORT = int(os.getenv("PORT", 8080))

# Bangladesh Standard Time (UTC+6)
BST = timezone(timedelta(hours=6))

def now_bst_str():
    return datetime.now(BST).strftime("%I:%M %p BST")

def today_bst_date():
    return datetime.now(BST).strftime("%Y-%m-%d")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Runtime Tracking State
ACTIVE_SERIES = None
TRACKING_CHANNEL_ID = None

NOTIFIED_KICKOFF = set()
NOTIFIED_ACHIEVED = set()
NOTIFIED_HALFTIME = set()
NOTIFIED_LATE = set()
NOTIFIED_FINAL = set()
LAST_SCORES = {}

LEDGER_FILE = "ledger.json"

# =====================================================================
# 2. PERFORMANCE & STATS LEDGER (ACTIVE DAYS ONLY)
# =====================================================================
def load_ledger():
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_series": 0,
        "cleared_series": 0,
        "missed_series": 0,
        "total_legs_tracked": 0,
        "total_legs_cleared": 0,
        "current_streak": 0,
        "best_streak": 0,
        "net_score": 0.0,
        "history": []
    }

def save_ledger(data):
    try:
        with open(LEDGER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Ledger save error: {e}")

def get_active_day_stats(ledger):
    today_str = today_bst_date()
    history = ledger.get("history", [])
    today_entries = [h for h in history if h.get("date") == today_str]

    today_played = len(today_entries)
    today_cleared = sum(1 for h in today_entries if h.get("cleared"))
    today_legs_hit = sum(h.get("legs_hit", 0) for h in today_entries)
    today_legs_total = sum(h.get("legs_total", 0) for h in today_entries)
    today_net = sum(h.get("net", 0.0) for h in today_entries)

    total_series = ledger.get("total_series", 0)
    cleared_series = ledger.get("cleared_series", 0)
    series_acc = round((cleared_series / total_series * 100), 1) if total_series > 0 else 0.0

    t_legs = ledger.get("total_legs_tracked", 0)
    c_legs = ledger.get("total_legs_cleared", 0)
    leg_acc = round((c_legs / t_legs * 100), 1) if t_legs > 0 else 0.0

    return {
        "today_played": today_played,
        "today_cleared": today_cleared,
        "today_legs": f"{today_legs_hit}/{today_legs_total}",
        "today_net": round(today_net, 2),
        "all_series": f"{cleared_series}/{total_series}",
        "series_acc": series_acc,
        "leg_acc": leg_acc,
        "streak": ledger.get("current_streak", 0),
        "best_streak": ledger.get("best_streak", 0),
        "net_score": round(ledger.get("net_score", 0.0), 2)
    }

# =====================================================================
# 3. VISUAL UI HELPERS (GAUGES & BARS)
# =====================================================================
def render_leg_progress(done, total):
    filled = "🟩" * done
    empty = "⬜" * max(0, total - done)
    pct = int(round((done / total) * 100)) if total > 0 else 0
    return f"{filled}{empty}  **{done} of {total} Cleared ({pct}%)**"

def render_likelihood_gauge(pct):
    bars = int(round(pct / 10))
    bar_str = "█" * bars + "░" * (10 - bars)
    if pct >= 75:
        tag = "🟢 Strong"
    elif pct >= 45:
        tag = "🟡 Balanced"
    else:
        tag = "🔴 Challenging"
    return f"`{bar_str}` **{pct}%** ({tag})"

def poisson_prob(k_needed, expected_goals):
    if k_needed <= 0:
        return 1.0
    if expected_goals <= 0:
        return 0.0
    prob_less = sum((expected_goals ** i) * math.exp(-expected_goals) / math.factorial(i) for i in range(k_needed))
    return max(0.02, min(0.98, 1.0 - prob_less))

# =====================================================================
# 4. ZERO-CONFIG IMAGE OCR + GEMINI AI PARSER
# =====================================================================
async def extract_text_via_free_ocr(image_bytes):
    """Fallback free cloud OCR so screenshots work automatically even without a Gemini key."""
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
        print(f"Free OCR fallback error: {e}")
    return ""

def call_gemini_parser(text_input=None, image_bytes=None):
    """Uses Google Gemini API if key is provided."""
    if not GEMINI_API_KEY:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        system_prompt = """
        You are an elite sports match intelligence parser.
        Extract the match targets from the user's message or image.
        Convert any kickoff times to Bangladesh Time (BST, UTC+6) if known, otherwise write 'Today (BST)'.
        Never use gambling or currency words.

        Return ONLY valid raw JSON matching this exact structure:
        {
          "title": "Match Series Challenge",
          "series_type": "Single Match" or "4-Leg Series",
          "multiplier": 2.30,
          "allocated_score": 990,
          "target_reward": 2281.84,
          "legs": [
            {
              "home_team": "Arsenal",
              "away_team": "Chelsea",
              "target_team": "Arsenal",
              "target_type": "team_win" | "over_goals" | "under_goals" | "team_over" | "btts",
              "target_value": 0.5,
              "label": "Arsenal to Win",
              "kickoff_bst": "9:00 PM BST"
            }
          ]
        }
        Only return raw JSON without markdown code blocks.
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
        print(f"Gemini parser error: {e}")
        return None

def smart_universal_text_parser(raw_text):
    """Parses OCR text from slip screenshots OR typed text automatically."""
    lines = [l.strip() for l in raw_text.replace("\r", "").split("\n") if l.strip()]
    legs = []
    pending_target = None

    for idx, line in enumerate(lines):
        low = line.lower()

        # Detect target condition lines (e.g., "Over 2 @1.289", "Tottenham Hotspur Over 0.5 @1.274", "Arsenal @1.145")
        if "@" in line and not any(x in low for x in ["odds:", "stake:", "risk:"]):
            clean_target = line.split("@")[0].strip()
            pending_target = clean_target

        # Detect Match Fixture lines (e.g., "Leeds United -vs- Tottenham Hotspur" or "Arsenal vs Chelsea")
        if "-vs-" in low or " vs " in low:
            sep = "-vs-" if "-vs-" in low else " vs "
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
                elif "btts" in c_low:
                    t_type = "btts"
                else:
                    t_type = "team_win"
                    t_team = away if away.lower() in c_low else home

                label = cond if pending_target else f"{t_team} Target"
                legs.append({
                    "home_team": home,
                    "away_team": away,
                    "target_team": t_team,
                    "target_type": t_type,
                    "target_value": t_val,
                    "label": label,
                    "kickoff_bst": "Live / Today (BST)"
                })
                pending_target = None

    if not legs:
        return None

    return {
        "title": f"{len(legs)}-Match Active Series" if len(legs) > 1 else f"{legs[0]['home_team']} vs {legs[0]['away_team']}",
        "series_type": f"{len(legs)}-Leg Combination" if len(legs) > 1 else "Single Match",
        "multiplier": round(1.3 ** len(legs), 2),
        "allocated_score": 100,
        "target_reward": round(100 * (1.3 ** len(legs)), 1),
        "legs": legs
    }

def generate_ai_match_insight(leg, res):
    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-1.5-flash")
            prompt = (
                f"Write a 1-sentence easy English tactical football update (max 22 words, zero gambling words). "
                f"Match: {res['score']} at {res['minute']} mins. "
                f"Target: {leg['label']}. Current Status: {res['requirement']}. "
                f"Likelihood: {res['likelihood_pct']}%."
            )
            resp = model.generate_content(prompt)
            return resp.text.strip()
        except Exception:
            pass
    return res.get("analysis", "Match is active with steady tempo.")

# =====================================================================
# 5. LIVE MATCH SCORE ENGINE
# =====================================================================
async def fetch_live_soccer_scores():
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/scorepanel"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    events = []
                    for group in data.get("scores", []):
                        events.extend(group.get("events", []))
                    return events
    except Exception as e:
        print(f"Fetch error: {e}")
    return []

def match_event_for_leg(leg, events):
    t1 = leg.get("home_team", "").lower()
    t2 = leg.get("away_team", "").lower()
    for ev in events:
        name = ev.get("name", "").lower()
        short = ev.get("shortName", "").lower()
        if (t1 in name or t1 in short) and (t2 in name or t2 in short):
            return ev
        if t1 and len(t1) > 3 and t1 in name:
            return ev
        if t2 and len(t2) > 3 and t2 in name:
            return ev
    return None

def evaluate_leg(leg, event):
    if not event:
        return {
            "status": "Scheduled",
            "score": f"{leg.get('home_team')} vs {leg.get('away_team')}",
            "home_score": 0, "away_score": 0,
            "home_name": leg.get("home_team"), "away_name": leg.get("away_team"),
            "minute": 0, "is_halftime": False, "is_final": False,
            "achieved": False, "impossible": False,
            "likelihood_pct": 55,
            "requirement": "Waiting for kickoff",
            "analysis": "Scheduled and waiting for kickoff."
        }

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
    is_halftime = "Halftime" in type_desc or type_desc == "HT"
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
            requirement = "Target Cleared! ✅"
            analysis = f"{t_disp} scored {team_score} goal(s). Target is locked in!"
        elif is_final:
            impossible, likelihood = True, 0.0
            requirement = f"Missed (Finished with {team_score} goals)"
            analysis = f"Full time reached. {t_disp} did not score enough."
        else:
            likelihood = poisson_prob(needed, rem_mins * 0.017)
            requirement = f"{needed} more goal needed from {t_disp} in ~{rem_mins} mins"
            analysis = f"{t_disp} has ~{rem_mins} mins plus stoppage time to score {needed} goal."

    elif t_type == "over_goals":
        total = home_score + away_score
        needed = math.ceil(t_val) - total
        if needed <= 0:
            achieved, likelihood = True, 1.0
            requirement = "Target Cleared! ✅"
            analysis = f"Total goals reached {total}. Target comfortably achieved!"
        elif is_final:
            impossible, likelihood = True, 0.0
            requirement = f"Missed (Finished at {total} goals)"
            analysis = f"Full time reached with {total} total goals."
        else:
            likelihood = poisson_prob(needed, exp_rem_goals)
            requirement = f"{needed} more goal(s) needed in ~{rem_mins} mins"
            analysis = f"Match needs {needed} more goal in the remaining {rem_mins} minutes."

    elif t_type == "team_win":
        t_team = leg.get("target_team", home_name).lower()
        is_home = t_team in home_name.lower()
        lead = (home_score - away_score) if is_home else (away_score - home_score)
        t_disp = leg.get("target_team", home_name)

        if is_final:
            if lead > 0:
                achieved, likelihood = True, 1.0
                requirement = "Victory Secured! ✅"
                analysis = f"Full time! {t_disp} takes the win."
            else:
                impossible, likelihood = True, 0.0
                requirement = "Missed at Full Time"
                analysis = f"Full time reached without a {t_disp} victory."
        else:
            if lead > 0:
                likelihood = min(0.96, 0.74 + (0.22 * (minute / 90.0)) + (0.08 * (lead - 1)))
                requirement = f"Hold {lead}-goal lead for ~{rem_mins} mins"
                analysis = f"{t_disp} is ahead by {lead} and controlling the game state."
            elif lead == 0:
                likelihood = max(0.18, 0.46 - (0.16 * (minute / 90.0)))
                requirement = f"1 goal needed to take the lead in ~{rem_mins} mins"
                analysis = f"Score is level ({home_score}-{away_score}). {t_disp} needs 1 goal to go ahead."
            else:
                needed = abs(lead) + 1
                likelihood = poisson_prob(needed, exp_rem_goals)
                requirement = f"{needed} goals needed in ~{rem_mins} mins to turn it around"
                analysis = f"{t_disp} is trailing by {abs(lead)} and needs a fast comeback."

    pct = int(round(likelihood * 100))
    return {
        "status": type_desc,
        "score": f"{home_name} {home_score} - {away_score} {away_name}",
        "home_score": home_score, "away_score": away_score,
        "home_name": home_name, "away_name": away_name,
        "minute": minute, "is_halftime": is_halftime, "is_final": is_final,
        "achieved": achieved, "impossible": impossible,
        "likelihood_pct": pct, "requirement": requirement, "analysis": analysis
    }

# =====================================================================
# 6. ONE-TAP DASHBOARD BUTTONS (NO TYPING NEEDED EVER)
# =====================================================================
class LiveControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📡 Live Scoreboard", style=discord.ButtonStyle.primary)
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not ACTIVE_SERIES:
            await interaction.response.send_message("ℹ️ No active series right now. Drop a screenshot to start!", ephemeral=True)
            return
        legs = ACTIVE_SERIES.get("legs", [])
        events = await fetch_live_soccer_scores()
        comb_prob = 1.0
        embed = discord.Embed(
            title=f"📡 Live Match Center — {ACTIVE_SERIES.get('title')}",
            description=f"{render_leg_progress(len(NOTIFIED_ACHIEVED), len(legs))}",
            color=0x3498DB
        )
        for i, leg in enumerate(legs, 1):
            ev = match_event_for_leg(leg, events)
            res = evaluate_leg(leg, ev)
            comb_prob *= (res["likelihood_pct"] / 100.0)
            icon = "✅" if res["achieved"] else ("❌" if res["impossible"] else "🟢" if res["minute"] > 0 else "🕒")
            embed.add_field(
                name=f"{icon} Leg {i}: {res['score']} ({res['minute']}')",
                value=(
                    f"• **Target:** `{leg.get('label')}`\n"
                    f"• **Needed:** {res['requirement']}\n"
                    f"• **Chance:** {render_likelihood_gauge(res['likelihood_pct'])}\n"
                    f"• **Read:** *{res['analysis']}*"
                ),
                inline=False
            )
        if len(legs) > 1:
            overall = int(round(comb_prob * 100))
            embed.add_field(name="🔗 Combined Series Chance", value=render_likelihood_gauge(overall), inline=False)
        embed.set_footer(text=f"Updated {now_bst_str()}")
        await interaction.response.send_message(embed=embed, view=LiveControlView())

    @discord.ui.button(label="📊 My Stats", style=discord.ButtonStyle.secondary)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ledger = load_ledger()
        s = get_active_day_stats(ledger)
        streak_icon = "🔥" if s["streak"] > 0 else "⚡"
        embed = discord.Embed(
            title="📊 Personal Performance & Accuracy Hub",
            color=0xF1C40F
        )
        embed.add_field(
            name=f"📅 Today's Session ({today_bst_date()})",
            value=(
                f"• **Series Cleared:** `{s['today_cleared']} of {s['today_played']}`\n"
                f"• **Individual Legs Hit:** `{s['today_legs']}`\n"
                f"• **Today's Net Score:** `{s['today_net']:+.1f} pts`"
            ),
            inline=False
        )
        embed.add_field(
            name="🏆 All-Time Career Totals",
            value=(
                f"• **Series Accuracy:** `{s['series_acc']}%` ({s['all_series']})\n"
                f"• **Leg Accuracy:** `{s['leg_acc']}%`\n"
                f"• **Current Streak:** {streak_icon} `{s['streak']} in a row` (Best: `{s['best_streak']}`)\n"
                f"• **Overall Net Score:** `{s['net_score']:+.1f} pts`"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @discord.ui.button(label="🛑 Stop Tracking", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        global ACTIVE_SERIES
        ACTIVE_SERIES = None
        await interaction.response.send_message("🛑 **Tracking stopped.** Drop a new screenshot whenever you're ready!")

# =====================================================================
# 7. ZERO-TYPE AUTO-LAUNCH ON IMAGE UPLOAD
# =====================================================================
def start_series_automatically(parsed, channel_id):
    global ACTIVE_SERIES, TRACKING_CHANNEL_ID
    global NOTIFIED_KICKOFF, NOTIFIED_ACHIEVED, NOTIFIED_HALFTIME, NOTIFIED_LATE, NOTIFIED_FINAL, LAST_SCORES

    ACTIVE_SERIES = parsed
    TRACKING_CHANNEL_ID = channel_id
    NOTIFIED_KICKOFF, NOTIFIED_ACHIEVED, NOTIFIED_HALFTIME, NOTIFIED_LATE, NOTIFIED_FINAL = set(), set(), set(), set(), set()
    LAST_SCORES = {}

    legs = ACTIVE_SERIES.get("legs", [])
    embed = discord.Embed(
        title=f"⚡ Auto-Tracking Started — {ACTIVE_SERIES.get('title')}",
        description=(
            f"**Format:** `{ACTIVE_SERIES.get('series_type')}`  •  **Multiplier:** `{ACTIVE_SERIES.get('multiplier', 1.0)}x`\n"
            f"**Overall Progress:**\n{render_leg_progress(0, len(legs))}\n\n"
            f"🔔 **100% Automatic Live Alerts Active:**\n"
            f"• 🟢 **Kickoff & ⚽ Live Goal Alerts**\n"
            f"• 🎉 **Leg Cleared Alerts (1 done, {max(0, len(legs)-1)} to go)**\n"
            f"• ⏱️ **Half-Time (45') & Final 20 Mins (72') Reads**\n"
            f"• 📊 **Active-Day Full Stats Card at Completion**"
        ),
        color=0x2ECC71
    )
    for i, leg in enumerate(legs, 1):
        embed.add_field(
            name=f"📌 Leg {i}: {leg.get('home_team')} vs {leg.get('away_team')}",
            value=f"• **Target:** `{leg.get('label')}`\n• **Schedule:** {leg.get('kickoff_bst', 'Today (BST)')}",
            inline=False
        )
    embed.set_footer(text=f"Auto-launched at {now_bst_str()} • Zero commands required")
    return embed

@bot.event
async def on_ready():
    print(f"🚀 Zero-Type Auto Match Bot Online as {bot.user}", flush=True)
    if not autonomous_loop.is_running():
        autonomous_loop.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # 1. User drops a Screenshot -> 100% Auto-Parse & Auto-Start (No typing needed!)
    if message.attachments:
        for att in message.attachments:
            if any(att.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                wait_msg = await message.channel.send("⚡ **Reading your image & launching auto-tracker...**")
                img_bytes = await att.read()

                # Try Gemini Vision first, then automatic Free OCR + Universal Parser
                parsed = call_gemini_parser(text_input=message.content, image_bytes=img_bytes)
                if not parsed or not parsed.get("legs"):
                    ocr_text = await extract_text_via_free_ocr(img_bytes)
                    if ocr_text:
                        parsed = call_gemini_parser(text_input=ocr_text) or smart_universal_text_parser(ocr_text)

                if parsed and parsed.get("legs"):
                    embed = start_series_automatically(parsed, message.channel.id)
                    await wait_msg.edit(content=None, embed=embed, view=LiveControlView())
                    return
                else:
                    await wait_msg.edit(
                        content="⚠️ Could not read clear match names from that image. Try cropping closer to the match names!"
                    )
                    return

    # 2. User pastes or types text -> Auto-Start immediately
    content = message.content.strip()
    if content and not content.startswith("!"):
        if any(k in content.lower() for k in ["vs", "-vs-", "over", "under", "win", "btts"]):
            parsed = call_gemini_parser(text_input=content) or smart_universal_text_parser(content)
            if parsed and parsed.get("legs"):
                embed = start_series_automatically(parsed, message.channel.id)
                await message.channel.send(embed=embed, view=LiveControlView())
                return

    await bot.process_commands(message)

# =====================================================================
# 8. 60-SECOND AUTONOMOUS LIVE TRACKING ENGINE
# =====================================================================
@tasks.loop(seconds=60)
async def autonomous_loop():
    global ACTIVE_SERIES, NOTIFIED_KICKOFF, NOTIFIED_ACHIEVED, NOTIFIED_HALFTIME, NOTIFIED_LATE, NOTIFIED_FINAL, LAST_SCORES
    if not ACTIVE_SERIES or not TRACKING_CHANNEL_ID:
        return

    channel = bot.get_channel(TRACKING_CHANNEL_ID)
    if not channel:
        return

    try:
        events = await fetch_live_soccer_scores()
        legs = ACTIVE_SERIES.get("legs", [])
        total_legs = len(legs)
        leg_results = []
        all_finished = True
        any_failed = False
        failed_leg_info = None

        for idx, leg in enumerate(legs):
            ev = match_event_for_leg(leg, events)
            res = evaluate_leg(leg, ev)
            leg_results.append(res)

            if not res["is_final"]:
                all_finished = False
            if res["impossible"]:
                any_failed = True
                failed_leg_info = (idx + 1, leg, res)

            minute = res["minute"]
            h_score, a_score = res["home_score"], res["away_score"]
            h_name, a_name = res["home_name"], res["away_name"]

            # 1. Kickoff Alert
            if (minute > 0 or "First Half" in res["status"]) and idx not in NOTIFIED_KICKOFF:
                NOTIFIED_KICKOFF.add(idx)
                ko_embed = discord.Embed(
                    title=f"🟢 KICKOFF — {h_name} vs {a_name}",
                    description=(
                        f"Match is now live!\n"
                        f"• **Target:** `{leg.get('label')}`\n"
                        f"• **Initial Chance:** {render_likelihood_gauge(res['likelihood_pct'])}"
                    ),
                    color=0x2ECC71
                )
                ko_embed.set_footer(text=now_bst_str())
                await channel.send(embed=ko_embed, view=LiveControlView())

            # 2. Instant Live Goal Alert
            prev_h, prev_a = LAST_SCORES.get(idx, (None, None))
            if prev_h is not None and (h_score > prev_h or a_score > prev_a):
                scorer = h_name if h_score > prev_h else a_name
                g_embed = discord.Embed(
                    title=f"⚽ GOAL! {scorer} scores! ({minute}')",
                    description=(
                        f"### {h_name} {h_score} - {a_score} {a_name}\n"
                        f"• **Your Target:** `{leg.get('label')}`\n"
                        f"• **Updated Situation:** {res['requirement']}\n"
                        f"• **Live Chance:** {render_likelihood_gauge(res['likelihood_pct'])}"
                    ),
                    color=0x00A8FF
                )
                g_embed.set_footer(text=now_bst_str())
                await channel.send(embed=g_embed, view=LiveControlView())
            LAST_SCORES[idx] = (h_score, a_score)

            # 3. Instant Leg Hit Celebration ("1 done, 3 to go!")
            if res["achieved"] and idx not in NOTIFIED_ACHIEVED:
                NOTIFIED_ACHIEVED.add(idx)
                done = len(NOTIFIED_ACHIEVED)
                left = total_legs - done
                headline = f"🎉 {done} Leg Done, {left} to Go!" if left > 0 else "🎉 Final Leg Cleared!"
                hit_embed = discord.Embed(
                    title=f"✅ Target Locked In! — {headline}",
                    description=(
                        f"**Leg {idx+1}:** {res['score']} ({minute}')\n"
                        f"**Cleared Target:** `{leg.get('label')}`\n\n"
                        f"**Series Progress:**\n{render_leg_progress(done, total_legs)}"
                    ),
                    color=0x2ECC71
                )
                hit_embed.set_footer(text=now_bst_str())
                await channel.send(embed=hit_embed, view=LiveControlView())

            is_ht = res["is_halftime"] or (45 <= minute <= 48 and idx not in NOTIFIED_HALFTIME)
            is_late = (70 <= minute <= 75 and idx not in NOTIFIED_LATE)

            # 4. Mid-Match Read (Half-Time 45')
            if is_ht and idx not in NOTIFIED_HALFTIME and not res["achieved"]:
                NOTIFIED_HALFTIME.add(idx)
                comb = 1.0
                for r in leg_results:
                    comb *= (r["likelihood_pct"] / 100.0)
                insight = generate_ai_match_insight(leg, res)

                ht_embed = discord.Embed(
                    title=f"⏱️ Half-Time Check (45') — {h_name} vs {a_name}",
                    description=(
                        f"**Current Score:** `{res['score']}`\n"
                        f"**What's Needed:** **{res['requirement']}**\n\n"
                        f"**Target Chance:** {render_likelihood_gauge(res['likelihood_pct'])}\n"
                        f"**Easy Read:** 💬 *{insight}*\n"
                        + (f"\n**Combined Series Chance:** {render_likelihood_gauge(int(round(comb*100)))}" if total_legs > 1 else "")
                    ),
                    color=0xF39C12
                )
                ht_embed.set_footer(text=now_bst_str())
                await channel.send(embed=ht_embed, view=LiveControlView())

            # 5. Late-Match Crunch Read (~72' — 20 mins left)
            elif is_late and idx not in NOTIFIED_LATE and not res["achieved"]:
                NOTIFIED_LATE.add(idx)
                comb = 1.0
                for r in leg_results:
                    comb *= (r["likelihood_pct"] / 100.0)
                insight = generate_ai_match_insight(leg, res)

                late_embed = discord.Embed(
                    title=f"🔥 Final 20 Mins Check ({minute}') — {h_name} vs {a_name}",
                    description=(
                        f"**Current Score:** `{res['score']}`\n"
                        f"**What's Needed:** **{res['requirement']}**\n\n"
                        f"**Target Chance:** {render_likelihood_gauge(res['likelihood_pct'])}\n"
                        f"**Easy Read:** 💬 *{insight}*\n"
                        + (f"\n**Combined Series Chance:** {render_likelihood_gauge(int(round(comb*100)))}" if total_legs > 1 else "")
                    ),
                    color=0xE67E22
                )
                late_embed.set_footer(text=now_bst_str())
                await channel.send(embed=late_embed, view=LiveControlView())

            if res["is_final"] and idx not in NOTIFIED_FINAL:
                NOTIFIED_FINAL.add(idx)

        # 6. Immediate Series Settlement (When all finish OR as soon as any leg misses!)
        all_cleared = (len(NOTIFIED_ACHIEVED) == total_legs)
        if any_failed or all_cleared or (all_finished and len(NOTIFIED_FINAL) == total_legs):
            success = all_cleared and not any_failed
            reward = float(ACTIVE_SERIES.get("target_reward", 100))
            alloc = float(ACTIVE_SERIES.get("allocated_score", 100))
            net_change = (reward - alloc) if success else -alloc

            ledger = load_ledger()
            ledger["total_series"] = ledger.get("total_series", 0) + 1
            ledger["total_legs_tracked"] = ledger.get("total_legs_tracked", 0) + total_legs
            ledger["total_legs_cleared"] = ledger.get("total_legs_cleared", 0) + len(NOTIFIED_ACHIEVED)

            if success:
                ledger["cleared_series"] = ledger.get("cleared_series", 0) + 1
                ledger["current_streak"] = ledger.get("current_streak", 0) + 1
                if ledger["current_streak"] > ledger.get("best_streak", 0):
                    ledger["best_streak"] = ledger["current_streak"]
            else:
                ledger["missed_series"] = ledger.get("missed_series", 0) + 1
                ledger["current_streak"] = 0

            ledger["net_score"] = ledger.get("net_score", 0.0) + net_change
            ledger["history"].append({
                "date": today_bst_date(),
                "title": ACTIVE_SERIES.get("title", "Series"),
                "cleared": success,
                "legs_hit": len(NOTIFIED_ACHIEVED),
                "legs_total": total_legs,
                "net": net_change
            })
            save_ledger(ledger)
            s = get_active_day_stats(ledger)

            if success:
                end_title = "🏆 SERIES CLEARED! All Targets Hit!"
                end_sub = f"Every single leg in **{ACTIVE_SERIES.get('title')}** was achieved!"
                end_color = 0x2ECC71
            else:
                leg_num, f_leg, f_res = failed_leg_info if failed_leg_info else (1, legs[0], leg_results[0])
                end_title = f"🛑 Series Concluded — Leg {leg_num} Missed"
                end_sub = (
                    f"**{f_res['score']} (Full Time)**\n"
                    f"Target `{f_leg.get('label')}` was not reached. Tracking stopped automatically."
                )
                end_color = 0xE74C3C

            final_embed = discord.Embed(
                title=end_title,
                description=(
                    f"{end_sub}\n\n"
                    f"**Leg Breakdown:** {render_leg_progress(len(NOTIFIED_ACHIEVED), total_legs)}\n"
                    f"**Session Impact:** `{net_change:+.1f} pts`"
                ),
                color=end_color
            )
            final_embed.add_field(
                name=f"📊 Today's Active-Day Stats ({today_bst_date()})",
                value=(
                    f"• **Today's Series:** `{s['today_cleared']} Cleared / {s['today_played']} Played`\n"
                    f"• **Today's Legs Hit:** `{s['today_legs']}`\n"
                    f"• **Today's Net Score:** `{s['today_net']:+.1f} pts`"
                ),
                inline=True
            )
            final_embed.add_field(
                name="📈 Overall Career Stats",
                value=(
                    f"• **Win Rate:** `{s['series_acc']}%` (Legs: `{s['leg_acc']}%`)\n"
                    f"• **Streak:** 🔥 `{s['streak']}` (Best: `{s['best_streak']}`)\n"
                    f"• **All-Time Net:** `{s['net_score']:+.1f} pts`"
                ),
                inline=True
            )
            final_embed.set_footer(text=f"Settled at {now_bst_str()} • Drop your next screenshot anytime")
            await channel.send(embed=final_embed, view=LiveControlView())
            ACTIVE_SERIES = None

    except Exception as e:
        print(f"Loop error: {e}", flush=True)

# =====================================================================
# 9. KEEP-ALIVE SERVER FOR 24/7 CLOUD HOSTING
# =====================================================================
async def health_check(request):
    return web.Response(text="⚡ Zero-Type Match Intelligence Engine is 24/7 Online!")

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
