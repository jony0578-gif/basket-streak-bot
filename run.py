import os
import requests
from datetime import datetime, timezone

API_HOST = "https://v1.basketball.api-sports.io"

COUNTRIES = ["Spain", "Turkey", "Italy"]
LAST_N = 15
TOP_N = 10

# Фильтры (как и раньше)
EXCLUDE_CHINA = True
EXCLUDE_WOMEN = True


def api_get(endpoint: str, params: dict | None = None):
    key = os.getenv("API_BASKETBALL_KEY")
    if not key:
        raise RuntimeError("Missing API_BASKETBALL_KEY secret")

    headers = {"x-apisports-key": key}
    url = f"{API_HOST}/{endpoint}"

    r = requests.get(url, headers=headers, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(f"API error: {data['errors']}")

    return data.get("response", [])


def send_telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN secret")
    if not chat_id:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID secret")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    r = requests.post(url, data=payload, timeout=60)
    r.raise_for_status()


def is_excluded(country_name: str, league_name: str) -> bool:
    name = f"{country_name} {league_name}".lower()

    if EXCLUDE_CHINA and ("china" in name or "cba" in name):
        return True

    if EXCLUDE_WOMEN:
        # мягкий фильтр по словам
        women_words = ["women", "womens", "woman", "fem", "femen", "femenino", "femin", "women's"]
        if any(w in name for w in women_words):
            return True

    return False


def get_leagues_for_country(country: str):
    # В basketball API параметр type обязателен: league или cup
    leagues = api_get("leagues", params={"country": country, "type": "league"})
    out = []
    for item in leagues:
        lg = item.get("league", {})
        league_id = lg.get("id")
        league_name = lg.get("name")
        if not league_id or not league_name:
            continue
        if is_excluded(country, league_name):
            continue
        out.append((league_id, league_name))
    return out


def get_latest_season_for_league(league_id: int):
    seasons = api_get("seasons", params={"league": league_id})
    seasons_int = []
    for s in seasons:
        try:
            seasons_int.append(int(s))
        except Exception:
            pass
    return max(seasons_int) if seasons_int else None


def get_teams(league_id: int, season: int):
    teams = api_get("teams", params={"league": league_id, "season": season})
    out = []
    for item in teams:
        team = item.get("team", {})
        tid = team.get("id")
        name = team.get("name")
        if tid and name:
            out.append((tid, name))
    return out


def get_last_games(team_id: int, league_id: int, season: int):
    # last=15 уже вернёт последние матчи
    games = api_get(
        "games",
        params={
            "team": team_id,
            "league": league_id,
            "season": season,
            "status": "FT",
            "last": LAST_N,
        },
    )
    # На всякий случай сортировка по date
    games_sorted = sorted(games, key=lambda g: g.get("date", ""), reverse=True)
    return games_sorted


def extract_q1_q2_totals(game):
    """
    Правильное чтение четвертей для API-Basketball:
    scores.home.quarter_1, scores.away.quarter_1, scores.home.quarter_2, scores.away.quarter_2
    """
    scores = game.get("scores", {})
    home = scores.get("home", {}) or {}
    away = scores.get("away", {}) or {}

    h1 = home.get("quarter_1")
    a1 = away.get("quarter_1")
    h2 = home.get("quarter_2")
    a2 = away.get("quarter_2")

    if None in (h1, a1, h2, a2):
        return None

    try:
        q1_total = int(h1) + int(a1)
        q2_total = int(h2) + int(a2)
    except Exception:
        return None

    return q1_total, q2_total


def calc_active_streak_and_freq(games):
    """
    Условие: Total 1Q < Total 2Q
    - streak: активная серия подряд от последнего матча назад
    - freq: сколько раз условие выполнено в окне (из матчей, где есть Q1/Q2)
    - usable: сколько матчей имели данные Q1/Q2
    """
    usable = 0
    hits = 0
    flags = []

    for g in games:
        q = extract_q1_q2_totals(g)
        if not q:
            continue
        q1, q2 = q
        ok = q1 < q2
        usable += 1
        hits += 1 if ok else 0
        flags.append(ok)

    # активная серия: идём по flags от самого свежего
    streak = 0
    for ok in flags:
        if ok:
            streak += 1
        else:
            break

    return streak, hits, usable


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = []  # (streak, hits, usable, team, league_label)
    diag = {
        "leagues_used": 0,
        "teams_scanned": 0,
        "teams_with_usable": 0,
        "total_usable_games": 0,
    }

    for country in COUNTRIES:
        leagues = get_leagues_for_country(country)

        for league_id, league_name in leagues:
            season = get_latest_season_for_league(league_id)
            if not season:
                continue

            diag["leagues_used"] += 1

            teams = get_teams(league_id, season)
            for team_id, team_name in teams:
                diag["teams_scanned"] += 1

                games = get_last_games(team_id, league_id, season)
                if not games:
                    continue

                streak, hits, usable = calc_active_streak_and_freq(games)

                if usable > 0:
                    diag["teams_with_usable"] += 1
                    diag["total_usable_games"] += usable

                # ВАЖНО: берём только активные серии (streak>=1)
                if streak >= 1:
                    rows.append((streak, hits, usable, team_name, f"{country} — {league_name}"))

    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = rows[:TOP_N]

    msg = []
    msg.append(f"🏀 TOP-{TOP_N} ACTIVE STREAK — total 1Q < 2Q")
    msg.append(f"🕒 {now}")
    msg.append(f"Окно: последние {LAST_N} матчей")
    msg.append("")

    if not top:
        msg.append("⚠️ НЕ НАЙДЕНО активных серий (streak>=1).")
        msg.append("")
        msg.append("Диагностика данных:")
        msg.append(f"- Лиг просмотрено: {diag['leagues_used']}")
        msg.append(f"- Команд просмотрено: {diag['teams_scanned']}")
        msg.append(f"- Команд с данными Q1/Q2: {diag['teams_with_usable']}")
        msg.append(f"- Всего матчей с Q1/Q2: {diag['total_usable_games']}")
        msg.append("")
        msg.append("Если команд с данными Q1/Q2 почти нет — проблема в том, что API по этим лигам/сезонам не отдаёт четверти.")
    else:
        for i, (streak, hits, usable, team, league) in enumerate(top, start=1):
            msg.append(f"{i}) streak {streak} | freq {hits}/{usable} — {team} ({league})")

        msg.append("")
        msg.append("Диагностика данных:")
        msg.append(f"- Лиг просмотрено: {diag['leagues_used']}")
        msg.append(f"- Команд просмотрено: {diag['teams_scanned']}")
        msg.append(f"- Команд с данными Q1/Q2: {diag['teams_with_usable']}")
        msg.append(f"- Всего матчей с Q1/Q2: {diag['total_usable_games']}")

    send_telegram("\n".join(msg))


if __name__ == "__main__":
    main()
