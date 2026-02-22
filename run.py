# run.py
# Basket Streak Bot — finds active streaks where (total Q1 < total Q2) in recent games
# Works with API-BASKETBALL (API-SPORTS) and sends a Telegram message.
#
# Required secrets/env:
#   API_BASKETBALL_KEY
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#
# Optional env:
#   SEASON="2024"                 (free plan supports 2022-2024; default 2024)
#   COUNTRIES="Spain,Turkey,Italy"
#   WINDOW="15"                   (how many last games to analyze per team)
#   MIN_STREAK="1"                (min streak length to include in top list)
#   TOP_N="10"
#   DEBUG="1"                     (adds extra diagnostics)

import os
import time
import requests
from datetime import datetime, timezone

API_HOST = "https://v1.basketball.api-sports.io"

ALLOWED_SEASONS = {2022, 2023, 2024}
DEFAULT_SEASON = 2024


def env_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


def pick_season() -> int:
    """Pick season from env, otherwise DEFAULT_SEASON. Free plan: 2022-2024."""
    v = os.getenv("SEASON", "").strip()
    if v:
        try:
            s = int(v)
            if s in ALLOWED_SEASONS:
                return s
        except Exception:
            pass
    return DEFAULT_SEASON


def api_get(endpoint: str, params: dict | None = None) -> dict:
    key = os.getenv("API_BASKETBALL_KEY", "").strip()
    if not key:
        raise RuntimeError("Missing API_BASKETBALL_KEY secret")

    url = f"{API_HOST}/{endpoint.lstrip('/')}"
    headers = {"x-apisports-key": key}

    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"API non-JSON response: HTTP {r.status_code} {r.text[:200]}")

    # API-SPORTS errors are often inside j["errors"]
    if r.status_code >= 400:
        raise RuntimeError(f"API HTTP {r.status_code}: {j}")

    errors = j.get("errors")
    if errors:
        raise RuntimeError(f"API error: {errors}")

    return j


def tg_send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN secret")
    if not chat_id:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID secret")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram send failed: HTTP {r.status_code} {r.text[:200]}")


def safe_name(x: dict, keys: list[str], default: str = "") -> str:
    for k in keys:
        if isinstance(x, dict) and x.get(k):
            return str(x.get(k))
    return default


def extract_q1_q2_from_game(game: dict) -> tuple[int | None, int | None]:
    """
    Try to extract TOTAL Q1 and TOTAL Q2 (both teams combined) from various API shapes.
    Returns (q1_total, q2_total) or (None, None) if not found.
    """
    scores = game.get("scores") or {}

    # Most common shape (API-BASKETBALL):
    # scores: { "home": {"quarter_1":..,"quarter_2":..}, "away": {...} }
    home = scores.get("home") or {}
    away = scores.get("away") or {}

    def to_int(v):
        if v is None:
            return None
        try:
            return int(v)
        except Exception:
            return None

    # Try quarter_1 / quarter_2
    h1 = to_int(home.get("quarter_1"))
    h2 = to_int(home.get("quarter_2"))
    a1 = to_int(away.get("quarter_1"))
    a2 = to_int(away.get("quarter_2"))
    if h1 is not None and h2 is not None and a1 is not None and a2 is not None:
        return (h1 + a1, h2 + a2)

    # Alternative shape sometimes appears as "quarters": {"q1":..,"q2":..} or list
    quarters = scores.get("quarters") or game.get("quarters") or {}
    if isinstance(quarters, dict):
        q1 = to_int(quarters.get("quarter_1") or quarters.get("q1"))
        q2 = to_int(quarters.get("quarter_2") or quarters.get("q2"))
        if q1 is not None and q2 is not None:
            return (q1, q2)

    return (None, None)


def get_countries() -> list[str]:
    raw = os.getenv("COUNTRIES", "Spain,Turkey,Italy")
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def get_leagues_for_country(country: str) -> list[dict]:
    # IMPORTANT: do NOT pass "current" (API returns "Current field do not exist")
    # type must be league/cup; we only want leagues
    j = api_get("leagues", params={"country": country, "type": "league"})
    resp = j.get("response") or []
    return resp if isinstance(resp, list) else []


def get_teams(league_id: int, season: int) -> list[dict]:
    j = api_get("teams", params={"league": league_id, "season": season})
    resp = j.get("response") or []
    return resp if isinstance(resp, list) else []


def get_last_games(team_id: int, league_id: int, season: int, last_n: int) -> list[dict]:
    j = api_get("games", params={"team": team_id, "league": league_id, "season": season, "last": last_n})
    resp = j.get("response") or []
    return resp if isinstance(resp, list) else []


def calc_streak_for_team(games: list[dict]) -> tuple[int, int, int]:
    """
    Streak definition:
      Walk games from most recent to older, count consecutive games where totalQ1 < totalQ2.
    Returns: (streak_len, used_games_with_q, total_games_checked)
    """
    streak = 0
    used_with_q = 0
    checked = 0

    for g in games:
        checked += 1
        q1, q2 = extract_q1_q2_from_game(g)
        if q1 is None or q2 is None:
            continue  # skip games without quarter data

        used_with_q += 1
        if q1 < q2:
            # keep streak going
            streak += 1
        else:
            # break streak on first failure (only after we met a game with Q data)
            break

    return streak, used_with_q, checked


def build_message(results: list[dict], season: int, window: int, diag: dict) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"<b>TOP-{len(results)} ACTIVE STREAK — total 1Q &lt; total 2Q</b>\n"
        f"🕒 {now_utc}\n"
        f"Сезон: {season}\n"
        f"Окно: последние {window} матчей\n"
    )

    if not results:
        body = "\n⚠️ <b>НЕ НАЙДЕНО активных серий</b> (streak ≥ 1).\n"
    else:
        lines = []
        for i, r in enumerate(results, 1):
            # Example:
            # 1) Real Madrid — streak 4 (Spain / ACB)
            lines.append(
                f"{i}) <b>{r['team_name']}</b> — серия: <b>{r['streak']}</b> "
                f"({r['country']} / {r['league_name']})"
            )
        body = "\n" + "\n".join(lines) + "\n"

    d = (
        "\n<b>Диагностика данных:</b>\n"
        f"— Лиг просмотрено: {diag.get('leagues_seen', 0)}\n"
        f"— Команд просмотрено: {diag.get('teams_seen', 0)}\n"
        f"— Команд с данными Q1/Q2: {diag.get('teams_with_q', 0)}\n"
        f"— Всего матчей с Q1/Q2: {diag.get('games_with_q', 0)}\n"
    )

    hint = (
        "\nЕсли лиг/команд = 0 — проблема в запросе лиг (страна/type).\n"
        "Если Q1/Q2 почти нет — по этим лигам/сезону API не отдаёт четверти "
        "(или у free-плана ограничение на сезон — используй 2022–2024).\n"
    )
    return header + body + d + hint


def main() -> None:
    season = pick_season()
    window = env_int("WINDOW", 15)
    min_streak = env_int("MIN_STREAK", 1)
    top_n = env_int("TOP_N", 10)
    debug = os.getenv("DEBUG", "").strip() == "1"

    countries = get_countries()

    diag = {
        "leagues_seen": 0,
        "teams_seen": 0,
        "teams_with_q": 0,
        "games_with_q": 0,
    }

    streak_rows: list[dict] = []

    # Light throttle to avoid rate limits
    def nap():
        time.sleep(0.35)

    for country in countries:
        leagues = get_leagues_for_country(country)
        nap()
        for L in leagues:
            league = L.get("league") or {}
            league_id = league.get("id")
            league_name = league.get("name") or "League"

            if not league_id:
                continue

            diag["leagues_seen"] += 1

            # Teams for league+season
            try:
                teams = get_teams(int(league_id), season)
            except Exception as e:
                # If free plan blocks season, it will error here or in games.
                if debug:
                    print(f"[DEBUG] teams error for {country}/{league_name} season={season}: {e}")
                continue

            nap()

            for T in teams:
                team = T.get("team") or {}
                team_id = team.get("id")
                team_name = team.get("name") or "Team"
                if not team_id:
                    continue

                diag["teams_seen"] += 1

                try:
                    games = get_last_games(int(team_id), int(league_id), season, window)
                except Exception as e:
                    if debug:
                        print(f"[DEBUG] games error team={team_name} league={league_name}: {e}")
                    continue

                nap()

                streak, used_with_q, _checked = calc_streak_for_team(games)

                if used_with_q > 0:
                    diag["teams_with_q"] += 1
                    diag["games_with_q"] += used_with_q

                if streak >= min_streak:
                    streak_rows.append(
                        {
                            "team_name": team_name,
                            "team_id": int(team_id),
                            "league_name": league_name,
                            "league_id": int(league_id),
                            "country": country,
                            "streak": streak,
                        }
                    )

    # Sort by streak desc, then team name
    streak_rows.sort(key=lambda x: (-x["streak"], x["team_name"].lower()))
    top = streak_rows[:top_n]

    msg = build_message(top, season=season, window=window, diag=diag)
    tg_send(msg)


if __name__ == "__main__":
    main()
