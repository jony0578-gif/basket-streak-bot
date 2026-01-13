import os
import time
import requests
from datetime import datetime, timedelta

# =========================
# CONFIG
# =========================

API_SPORTS_KEY = os.getenv("API_SPORTS_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BASE_URL = "https://v1.basketball.api-sports.io"

# Лиги (как мы договорились: Испания, Турция, Италия)
# Важно: league_id должны существовать в API-Basketball
LEAGUES = [
    {"country": "Spain", "league_id": 117, "league_name": "ACB"},
    {"country": "Turkey", "league_id": 116, "league_name": "BSL"},
    {"country": "Italy", "league_id": 111, "league_name": "LBA"},
]

TOP_N = 10
LAST_GAMES_MAX = 15

# Условие серии: total 1q < 2q (как ты просил)
def condition_q1_lt_q2(game):
    q1 = game.get("scores", {}).get("home", {}).get("quarter_1")
    q1a = game.get("scores", {}).get("away", {}).get("quarter_1")
    q2 = game.get("scores", {}).get("home", {}).get("quarter_2")
    q2a = game.get("scores", {}).get("away", {}).get("quarter_2")
    if None in (q1, q1a, q2, q2a):
        return None
    total_q1 = q1 + q1a
    total_q2 = q2 + q2a
    return total_q1 < total_q2


# =========================
# HELPERS
# =========================

def api_get(endpoint, params=None, max_retries=3):
    """
    Universal GET to API-Sports Basketball
    """
    if not API_SPORTS_KEY:
        raise RuntimeError("API_SPORTS_KEY is empty. Add it to GitHub Secrets.")

    url = f"{BASE_URL}/{endpoint}"
    headers = {
        "x-apisports-key": API_SPORTS_KEY
    }

    for attempt in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=30)
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"API response is not JSON. Status={r.status_code}, Text={r.text[:200]}")

        # API-Sports standard: { "errors": {...}, "response": [...] }
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {j}")

        errors = j.get("errors")
        if errors:
            raise RuntimeError(f"API error: {errors}")

        return j.get("response", [])

        # retry fallback
        time.sleep(1 + attempt)

    raise RuntimeError("API request failed after retries")


def telegram_send(text):
    """
    Send message to Telegram
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty. Add it to GitHub Secrets.")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is empty. Add it to GitHub Secrets.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage error {r.status_code}: {r.text}")


def get_latest_season():
    """
    FIXED:
    Endpoint seasons does NOT support params={"league": ...}
    So we just fetch all seasons and take the latest.
    """
    seasons = api_get("seasons")
    if not seasons:
        raise RuntimeError("Seasons list is empty from API.")

    # seasons are usually numbers like 2018,2019,2020...
    seasons_sorted = sorted(seasons)
    return seasons_sorted[-1]


def get_team_games(league_id, season, team_id, last_n=15):
    """
    Load last N finished games for one team in league/season.
    """
    params = {
        "league": league_id,
        "season": season,
        "team": team_id,
        "status": "FT",
    }
    games = api_get("games", params=params)

    # Sort by date descending
    def parse_date(g):
        # example: "2024-01-13T19:00:00+00:00"
        ds = g.get("date")
        if not ds:
            return datetime.min
        # cut timezone part for safety
        try:
            return datetime.fromisoformat(ds.replace("Z", "+00:00"))
        except Exception:
            return datetime.min

    games_sorted = sorted(games, key=parse_date, reverse=True)
    return games_sorted[:last_n]


def get_teams(league_id, season):
    """
    Get list of teams for league & season
    """
    params = {"league": league_id, "season": season}
    teams = api_get("teams", params=params)
    return teams


def streak_and_freq(games):
    """
    Count ACTIVE streak at start of list (latest games first)
    returns:
      streak = how many recent games satisfy condition
      freq = percent within list that satisfy condition
      usable = how many games actually had quarter data
    """
    usable = 0
    ok_count = 0

    streak = 0
    streak_active = True

    for idx, g in enumerate(games):
        cond = condition_q1_lt_q2(g)
        if cond is None:
            continue

        usable += 1
        if cond:
            ok_count += 1

        # streak logic based on latest games order
        if streak_active:
            if cond:
                streak += 1
            else:
                streak_active = False

    freq = (ok_count / usable * 100.0) if usable > 0 else 0.0
    return streak, freq, usable


def format_report(top_list):
    """
    Build Telegram-friendly message
    """
    dt = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append(f"🏀 <b>TOP {TOP_N} активных серий</b>")
    lines.append(f"Условие: <b>Total 1Q &lt; Total 2Q</b>")
    lines.append(f"Обновлено: <b>{dt}</b>")
    lines.append("")

    for i, item in enumerate(top_list, start=1):
        streak = item["streak"]
        freq = item["freq"]
        usable = item["usable"]
        team = item["team_name"]
        league = item["league"]
        lines.append(
            f"{i}) 🔥 <b>{streak}</b> | {freq:.0f}% ({usable} игр) — <b>{team}</b>\n"
            f"   <i>{league}</i>"
        )

    return "\n".join(lines)


# =========================
# MAIN
# =========================

def main():
    season = get_latest_season()

    candidates = []

    for L in LEAGUES:
        country = L["country"]
        league_id = L["league_id"]
        league_name = L["league_name"]

        teams = get_teams(league_id, season)

        for t in teams:
            team_id = t.get("id")
            team_name = t.get("name")
            if not team_id or not team_name:
                continue

            games = get_team_games(league_id, season, team_id, last_n=LAST_GAMES_MAX)

            streak, freq, usable = streak_and_freq(games)
            if usable < 5:
                continue  # мало данных, пропускаем

            candidates.append({
                "country": country,
                "league": f"{country} — {league_name}",
                "team_id": team_id,
                "team_name": team_name,
                "streak": streak,
                "freq": freq,
                "usable": usable,
            })

    # Sort by streak desc, then frequency desc
    candidates_sorted = sorted(
        candidates,
        key=lambda x: (x["streak"], x["freq"], x["usable"]),
        reverse=True
    )

    top = candidates_sorted[:TOP_N]

    if not top:
        telegram_send("⚠️ Нет данных для TOP (проверь сезоны/лиги/лимиты API).")
        return

    msg = format_report(top)
    telegram_send(msg)


if __name__ == "__main__":
    main()
