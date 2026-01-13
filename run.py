import os
import requests
from collections import defaultdict

API_HOST = "https://v1.basketball.api-sports.io"


def api_get(endpoint: str, params: dict | None = None):
    key = os.getenv("API_BASKETBALL_KEY")
    if not key:
        raise RuntimeError("Missing API_BASKETBALL_KEY secret")

    headers = {"x-apisports-key": key}
    url = f"{API_HOST}/{endpoint}"

    r = requests.get(url, headers=headers, params=params, timeout=60)
    data = r.json()

    # если API вернул ошибки
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(f"API error: {data['errors']}")

    return data.get("response", [])


def send_telegram(text: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secret")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()


def get_leagues(country: str):
    # ВАЖНО: type обязателен и current НЕ существует в basketball API
    leagues = api_get("leagues", params={"country": country, "type": "league"})
    result = []
    for item in leagues:
        league = item.get("league", {})
        if league.get("id") and league.get("name"):
            result.append({"id": league["id"], "name": league["name"]})
    return result


def get_latest_season(league_id: int):
    seasons = api_get("seasons", params={"league": league_id})
    if not seasons:
        return None

    # seasons может быть [2021, 2022, "2023"] → приводим к int
    seasons_int = []
    for s in seasons:
        try:
            seasons_int.append(int(s))
        except:
            pass

    if not seasons_int:
        return None

    return max(seasons_int)


def get_team_games(league_id: int, season: int, team_id: int):
    # берём последние 15 игр
    games = api_get("games", params={
        "league": league_id,
        "season": season,
        "team": team_id,
        "last": 15
    })
    return games


def get_teams(league_id: int, season: int):
    teams = api_get("teams", params={"league": league_id, "season": season})
    result = []
    for item in teams:
        team = item.get("team", {})
        if team.get("id") and team.get("name"):
            result.append({"id": team["id"], "name": team["name"]})
    return result


def streak_and_freq(games):
    """
    УСЛОВИЕ:
    total 1q < 2q

    streak = текущая активная серия подряд (начинаем с самой свежей игры)
    freq = сколько раз условие выполнилось в последних 15 матчах
    """

    def cond(game):
        scores = game.get("scores", {})
        q1 = scores.get("quarter_1", {})
        q2 = scores.get("quarter_2", {})

        # total 1q = home+away
        t1 = (q1.get("home") or 0) + (q1.get("away") or 0)
        t2 = (q2.get("home") or 0) + (q2.get("away") or 0)

        return t1 < t2

    # сортируем по дате (самые свежие первые)
    games_sorted = sorted(games, key=lambda g: g.get("date", ""), reverse=True)

    flags = [cond(g) for g in games_sorted]

    # active streak
    streak = 0
    for f in flags:
        if f:
            streak += 1
        else:
            break

    freq = sum(flags)
    return streak, freq


def main():
    countries = ["Spain", "Turkey", "Italy"]
    all_rows = []

    for country in countries:
        leagues = get_leagues(country)

        for lg in leagues:
            league_id = lg["id"]
            league_name = lg["name"]

            season = get_latest_season(league_id)
            if not season:
                continue

            teams = get_teams(league_id, season)
            if not teams:
                continue

            for team in teams:
                team_id = team["id"]
                team_name = team["name"]

                games = get_team_games(league_id, season, team_id)
                if not games:
                    continue

                streak, freq = streak_and_freq(games)

                # нужны только активные серии
                if streak > 0:
                    all_rows.append({
                        "streak": streak,
                        "freq": freq,
                        "team": team_name,
                        "league": f"{country} — {league_name}"
                    })

    # сортируем: сначала по streak, потом по freq
    all_rows.sort(key=lambda x: (x["streak"], x["freq"]), reverse=True)

    top = all_rows[:10]

    if not top:
        send_telegram("Сегодня нет команд с активной серией по условию: total 1q < 2q")
        return

    msg = "🏀 ТОП-10 active streak (total 1q < 2q)\n\n"
    for i, row in enumerate(top, start=1):
        msg += f"{i}) streak={row['streak']} | freq={row['freq']}/15 | {row['team']} | {row['league']}\n"

    send_telegram(msg)


if __name__ == "__main__":
    main()
