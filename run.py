import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict

API_KEY = os.getenv("API_BASKETBALL_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://v1.basketball.api-sports.io"

# ====== НАСТРОЙКИ ======
COUNTRIES = ["Spain", "Turkey", "Italy"]
TOP_N = 10
LAST_GAMES = 15

# Условие серии: total 1Q < 2Q
def check_condition(q1_total: int, q2_total: int) -> bool:
    return q1_total < q2_total


# ====== API HELPERS ======
def api_get(endpoint: str, params=None):
    if params is None:
        params = {}

    headers = {
        "x-apisports-key": API_KEY
    }

    url = f"{BASE_URL}/{endpoint}"
    r = requests.get(url, headers=headers, params=params, timeout=30)

    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"API error: can't decode JSON: {r.text[:200]}")

    if j.get("errors"):
        raise RuntimeError(f"API error: {j['errors']}")

    return j["response"]


def get_leagues(country: str):
    # Берем только активные сезоны
    data = api_get("leagues", params={
        "country": country,
        "type": "League",
        "current": "true"
    })

    leagues = []
    for item in data:
        league = item.get("league", {})
        seasons = item.get("seasons", [])

        # выбираем текущий сезон
        current_season = None
        for s in seasons:
            if s.get("current") is True:
                current_season = s.get("season")
                break

        if not current_season:
            continue

        leagues.append({
            "league_id": league.get("id"),
            "league_name": league.get("name"),
            "season": current_season
        })

    return leagues


def get_team_games(league_id: int, season: int, team_id: int, n=15):
    # последние игры команды
    games = api_get("games", params={
        "league": league_id,
        "season": season,
        "team": team_id,
        "timezone": "Europe/Moscow"
    })

    # сортировка по дате: свежие сверху
    def game_dt(g):
        # формат вида 2025-01-13T20:00:00+00:00
        dt_str = g["date"]
        return dt_str

    games_sorted = sorted(games, key=game_dt, reverse=True)
    return games_sorted[:n]


def get_teams_in_league(league_id: int, season: int):
    data = api_get("teams", params={
        "league": league_id,
        "season": season
    })

    teams = []
    for item in data:
        t = item.get("team", {})
        teams.append({
            "id": t.get("id"),
            "name": t.get("name")
        })
    return teams


def calc_streak(team_games):
    """
    Считает АКТИВНУЮ серию подряд (начиная с самого последнего матча)
    где total 1Q < 2Q
    """
    streak = 0
    checked = 0

    for g in team_games:
        scores = g.get("scores", {})
        q1 = scores.get("quarter_1", {})
        q2 = scores.get("quarter_2", {})

        # если данных по четвертям нет — пропускаем матч
        if not q1 or not q2:
            continue

        try:
            q1_total = int(q1.get("home", 0)) + int(q1.get("away", 0))
            q2_total = int(q2.get("home", 0)) + int(q2.get("away", 0))
        except Exception:
            continue

        checked += 1

        if check_condition(q1_total, q2_total):
            streak += 1
        else:
            break

        if checked >= LAST_GAMES:
            break

    return streak, checked


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.text[:200]}")


# ====== MAIN ======
def main():
    if not API_KEY:
        raise RuntimeError("Missing API_BASKETBALL_KEY secret")
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN secret")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID secret")

    all_rows = []

    for country in COUNTRIES:
        leagues = get_leagues(country)

        for lg in leagues:
            league_id = lg["league_id"]
            league_name = lg["league_name"]
            season = lg["season"]

            if not league_id or not season:
                continue

            teams = get_teams_in_league(league_id, season)

            for t in teams:
                team_id = t["id"]
                team_name = t["name"]

                team_games = get_team_games(league_id, season, team_id, n=LAST_GAMES)
                streak, checked = calc_streak(team_games)

                if checked == 0:
                    continue

                all_rows.append({
                    "country": country,
                    "league": league_name,
                    "team": team_name,
                    "streak": streak,
                    "checked": checked
                })

    # сортируем по серии
    all_rows.sort(key=lambda x: x["streak"], reverse=True)

    top = all_rows[:TOP_N]

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    msg_lines = [
        f"🏀 <b>ТОП-{TOP_N} активных серий (условие: total 1Q &lt; 2Q)</b>",
        f"🕒 {now}",
        ""
    ]

    if not top:
        msg_lines.append("Нет данных 😢")
    else:
        for i, row in enumerate(top, start=1):
            msg_lines.append(
                f"{i}) <b>{row['team']}</b> — streak: <b>{row['streak']}</b> "
                f"(из {row['checked']}) | {row['country']} — {row['league']}"
            )

    send_telegram("\n".join(msg_lines))


if __name__ == "__main__":
    main()
