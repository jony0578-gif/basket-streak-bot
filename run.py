import os
import requests
from telegram import Bot

API_SPORTS_KEY = os.environ["API_SPORTS_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://v1.basketball.api-sports.io"
HEADERS = {"x-apisports-key": API_SPORTS_KEY}

LAST_N_GAMES = 15
TOP_N = 10

REQUEST_COUNT = 0
REQUEST_LIMIT = 98

TARGET_LEAGUES = [
    ("Spain", "ACB"),
    ("Turkey", "Super Ligi"),
    ("Italy", "Lega A"),
]

EXCLUDE_WORDS = ["Women", "W", "CBA", "China"]


def api_get(path: str, params=None):
    global REQUEST_COUNT

    REQUEST_COUNT += 1
    if REQUEST_COUNT > REQUEST_LIMIT:
        Bot(BOT_TOKEN).send_message(
            chat_id=CHAT_ID,
            text=(
                f"⚠️ Лимит запросов почти исчерпан: {REQUEST_COUNT}>{REQUEST_LIMIT}.\n"
                f"Останавливаю скрипт, чтобы не сжечь 100 req/day."
            ),
        )
        raise SystemExit(0)

    r = requests.get(f"{BASE}/{path}", headers=HEADERS, params=params, timeout=40)
    r.raise_for_status()
    j = r.json()

    if isinstance(j, dict) and j.get("errors"):
        raise RuntimeError(f"API error: {j['errors']}")

    return j["response"]


def pick_target_leagues():
    leagues = api_get("leagues")
    found = []

    for country, league_name in TARGET_LEAGUES:
        best = None

        for l in leagues:
            cname = (l.get("country") or {}).get("name", "")
            lname = (l.get("name", "") or "")

            if cname != country:
                continue

            if any(w.lower() in lname.lower() for w in EXCLUDE_WORDS):
                continue

            if league_name.lower() in lname.lower():
                best = l
                break

        if not best:
            raise RuntimeError(
                f"❌ Не нашёл лигу: {country} - {league_name}. "
                f"Нужно проверить как она называется в API."
            )

        found.append(best)

    return found


def get_latest_season(league_id: int):
    seasons = api_get("seasons", params={"league": league_id})
    return max(seasons)


def list_teams(league_id: int, season):
    return api_get("teams", params={"league": league_id, "season": season})


def last_games(team_id: int, league_id: int, season):
    return api_get(
        "games",
        params={
            "league": league_id,
            "season": season,
            "team": team_id,
            "status": "FT",
            "last": LAST_N_GAMES,
        },
    )


def safe_quarter(game, side: str, quarter_key: str):
    try:
        return game["scores"][side][quarter_key]
    except Exception:
        return None


def calc_active_streak_and_freq(games):
    """
    ACTIVE streak:
    Count ONLY consecutive games from the most recent backwards,
    where (Total 1Q < Total 2Q)
    """
    freq = 0
    usable = 0

    # freq: how many hits among last N games
    for g in games:
        h1 = safe_quarter(g, "home", "quarter_1")
        a1 = safe_quarter(g, "away", "quarter_1")
        h2 = safe_quarter(g, "home", "quarter_2")
        a2 = safe_quarter(g, "away", "quarter_2")

        if None in (h1, a1, h2, a2):
            continue

        usable += 1
        # ✅ NEW CONDITION: 1Q < 2Q
        if (h1 + a1) < (h2 + a2):
            freq += 1

    # active streak: only consecutive from latest match
    streak = 0
    for g in games:
        h1 = safe_quarter(g, "home", "quarter_1")
        a1 = safe_quarter(g, "away", "quarter_1")
        h2 = safe_quarter(g, "home", "quarter_2")
        a2 = safe_quarter(g, "away", "quarter_2")

        if None in (h1, a1, h2, a2):
            break

        # ✅ NEW CONDITION: 1Q < 2Q
        if (h1 + a1) < (h2 + a2):
            streak += 1
        else:
            break

    return streak, freq, usable


def main():
    bot = Bot(BOT_TOKEN)

    leagues = pick_target_leagues()

    results = []

    for lg in leagues:
        league_id = lg["id"]
        league_name = lg.get("name", "")
        country = (lg.get("country") or {}).get("name", "")

        season = get_latest_season(league_id)

        teams = list_teams(league_id, season)
        for t in teams:
            team_id = t["id"]
            team_name = t.get("name", "Unknown")

            games = last_games(team_id, league_id, season)
            if not games:
                continue

            streak, freq, usable = calc_active_streak_and_freq(games)
            results.append((streak, freq, usable, team_name, f"{country} — {league_name}"))

    results.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = results[:TOP_N]

    lines = []
    lines.append("🏀 TOP-10 ACTIVE STREAK: 1Q total < 2Q total")
    lines.append("Лиги: Spain ACB / Turkey / Italy LBA")
    lines.append(f"Окно: последние {LAST_N_GAMES} матчей")
    lines.append("")

    for i, (streak, freq, usable, team, league) in enumerate(top, start=1):
        lines.append(f"{i}) {team} — streak {streak} | freq {freq}/{usable} ({league})")

    lines.append("")
    lines.append(f"📌 Requests used today: {REQUEST_COUNT}/{REQUEST_LIMIT}")

    bot.send_message(chat_id=CHAT_ID, text="\n".join(lines))


if __name__ == "__main__":
    main()
