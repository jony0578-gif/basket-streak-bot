import os
import sys
import time
import requests
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

# =========================
# CONFIG
# =========================
API_HOST = os.getenv("API_HOST", "https://v1.basketball.api-sports.io").rstrip("/")
API_KEY = os.getenv("API_BASKETBALL_KEY")  # GitHub Secret
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # GitHub Secret
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # GitHub Secret

# Страны (можно переопределить переменной COUNTRIES="Spain,Turkey,Italy")
COUNTRIES = [c.strip() for c in os.getenv("COUNTRIES", "Spain,Turkey,Italy").split(",") if c.strip()]

# Окно последних матчей (по умолчанию 15)
WINDOW_LAST = int(os.getenv("WINDOW_LAST", "15"))

# Минимальная серия, чтобы попасть в выборку (по умолчанию 1 = хотя бы 1 матч подряд)
MIN_STREAK = int(os.getenv("MIN_STREAK", "1"))

# Лимит команд в итоговом топе
TOP_N = int(os.getenv("TOP_N", "10"))

# Тип лиг в API-Sports: "league" или "cup"
LEAGUE_TYPE = os.getenv("LEAGUE_TYPE", "league").strip().lower()

# Можно принудительно задать сезон: SEASON=2024
SEASON_ENV = os.getenv("SEASON")

# Если хочешь принудительно задать конкретные ID лиг:
# LEAGUE_IDS="45,46,120"
LEAGUE_IDS_ENV = os.getenv("LEAGUE_IDS")


# =========================
# HELPERS
# =========================
def require_env(name: str, value: Optional[str]) -> str:
    if not value or not str(value).strip():
        raise RuntimeError(f"Missing {name} secret/env")
    return str(value).strip()


def guess_season() -> int:
    """
    У API-Sports по баскетболу 'season' чаще всего — год начала сезона.
    Чтобы не попадать в будущий год, берём:
    - если сейчас август+ (08..12) => season = текущий год
    - иначе => season = текущий год - 1
    Можно override через SEASON.
    """
    if SEASON_ENV and SEASON_ENV.strip().isdigit():
        return int(SEASON_ENV.strip())

    now = datetime.now(timezone.utc)
    y = now.year
    if now.month >= 8:
        return y
    return y - 1


def api_get(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_HOST}/{endpoint.lstrip('/')}"
    headers = {
        "x-apisports-key": API_KEY,
    }

    # Небольшие ретраи, чтобы не падать на кратких сбоях/лимитах
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {data}")
            # API-Sports обычно кладёт ошибки сюда:
            if isinstance(data, dict) and data.get("errors"):
                raise RuntimeError(f"API error: {data['errors']}")
            return data
        except Exception as e:
            last_err = e
            time.sleep(1.5 * attempt)
    raise RuntimeError(str(last_err))


def tg_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram send error HTTP {r.status_code}: {r.text}")


def extract_quarters_total(game: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """
    Возвращает (total_q1, total_q2) для матча, если есть данные четвертей.
    """
    scores = game.get("scores") or {}
    home = scores.get("home") or {}
    away = scores.get("away") or {}

    # В API-Sports по баскетболу четверти часто лежат в:
    # scores.home.quarter_1 / quarter_2
    # scores.away.quarter_1 / quarter_2
    q1h = home.get("quarter_1")
    q2h = home.get("quarter_2")
    q1a = away.get("quarter_1")
    q2a = away.get("quarter_2")

    if q1h is None or q2h is None or q1a is None or q2a is None:
        return None

    try:
        total_q1 = int(q1h) + int(q1a)
        total_q2 = int(q2h) + int(q2a)
        return total_q1, total_q2
    except Exception:
        return None


def compute_team_streak(games: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    """
    Считает серию подряд по условию: total Q1 < total Q2
    Идём от самых свежих матчей к старым.
    Возвращает:
      (streak_len, games_with_q12, total_games_checked)
    """
    streak = 0
    games_with_q12 = 0
    checked = 0

    for g in games:
        checked += 1
        q = extract_quarters_total(g)
        if q is None:
            # если у матча нет четвертей — просто пропускаем его (не ломаем серию)
            continue

        games_with_q12 += 1
        total_q1, total_q2 = q

        if total_q1 < total_q2:
            streak += 1
        else:
            # серия прерывается на первом же "неподходящем" матче с Q1/Q2
            break

    return streak, games_with_q12, checked


# =========================
# API FUNCTIONS
# =========================
def get_leagues_for_country(country: str, season: int) -> List[Dict[str, Any]]:
    """
    /leagues?country=...&season=...&type=league
    """
    params = {"country": country, "season": season}
    if LEAGUE_TYPE:
        params["type"] = LEAGUE_TYPE  # "league" или "cup"

    data = api_get("leagues", params=params)
    return data.get("response") or []


def get_teams(league_id: int, season: int) -> List[Dict[str, Any]]:
    """
    /teams?league=...&season=...
    """
    data = api_get("teams", params={"league": league_id, "season": season})
    return data.get("response") or []


def get_last_games_for_team(team_id: int, season: int, last_n: int) -> List[Dict[str, Any]]:
    """
    /games?team=...&season=...&last=...
    Сортировка обычно от свежих к старым (если нет — перевернём по дате).
    """
    data = api_get("games", params={"team": team_id, "season": season, "last": last_n})
    games = data.get("response") or []

    # на всякий случай отсортируем по дате убыванию
    def key_dt(g: Dict[str, Any]) -> float:
        d = (g.get("date") or g.get("time") or g.get("timestamp"))
        # timestamp может быть int
        if isinstance(d, (int, float)):
            return float(d)
        # date строка ISO
        try:
            dt = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0

    games.sort(key=key_dt, reverse=True)
    return games


# =========================
# MAIN
# =========================
def main() -> None:
    global API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    API_KEY = require_env("API_BASKETBALL_KEY", API_KEY)
    TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    TELEGRAM_CHAT_ID = require_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    season = guess_season()
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Если вручную заданы ID лиг — используем их
    forced_league_ids: List[int] = []
    if LEAGUE_IDS_ENV:
        for x in LEAGUE_IDS_ENV.split(","):
            x = x.strip()
            if x.isdigit():
                forced_league_ids.append(int(x))

    leagues: List[Dict[str, Any]] = []
    leagues_seen = 0

    if forced_league_ids:
        # Сформируем фейковые записи лиг только по ID
        for lid in forced_league_ids:
            leagues.append({"league": {"id": lid, "name": f"League {lid}"}, "country": {"name": "FORCED"}})
        leagues_seen = len(leagues)
    else:
        # Собираем лиги по странам
        for c in COUNTRIES:
            resp = get_leagues_for_country(c, season)
            for item in resp:
                lid = ((item.get("league") or {}).get("id"))
                if lid:
                    leagues.append(item)
            leagues_seen += len(resp)

    # Уникализируем по league.id
    uniq = {}
    for item in leagues:
        lid = ((item.get("league") or {}).get("id"))
        if lid:
            uniq[int(lid)] = item
    leagues = list(uniq.values())

    # Диагностика
    diag_leagues_used = 0
    diag_teams_seen = 0
    diag_teams_with_q12 = 0
    diag_total_games_with_q12 = 0

    results: List[Dict[str, Any]] = []

    for item in leagues:
        league = item.get("league") or {}
        country = item.get("country") or {}
        league_id = league.get("id")
        league_name = league.get("name", "Unknown league")
        country_name = country.get("name", "Unknown country")

        if not league_id:
            continue

        league_id = int(league_id)

        try:
            teams = get_teams(league_id, season)
        except Exception:
            # если по лиге нет команд — пропускаем
            continue

        if not teams:
            continue

        diag_leagues_used += 1

        for t in teams:
            team = t.get("team") or t  # иногда структура разная
            team_id = team.get("id")
            team_name = team.get("name", "Unknown team")
            if not team_id:
                continue

            team_id = int(team_id)
            diag_teams_seen += 1

            try:
                games = get_last_games_for_team(team_id, season, WINDOW_LAST)
            except Exception:
                continue

            streak, games_with_q12, checked = compute_team_streak(games)
            diag_total_games_with_q12 += games_with_q12
            if games_with_q12 > 0:
                diag_teams_with_q12 += 1

            if streak >= MIN_STREAK:
                results.append(
                    {
                        "streak": streak,
                        "team": team_name,
                        "league": league_name,
                        "country": country_name,
                        "team_id": team_id,
                        "league_id": league_id,
                    }
                )

    # Сортировка по длине серии
    results.sort(key=lambda x: x["streak"], reverse=True)
    top = results[:TOP_N]

    # Сообщение
    title = f"🏀 <b>TOP-{TOP_N} ACTIVE STREAK</b> — <b>total 1Q &lt; total 2Q</b>\n" \
            f"🕒 <code>{utc_now}</code>\n" \
            f"Сезон: <b>{season}</b>\n" \
            f"Окно: последние <b>{WINDOW_LAST}</b> матчей\n"

    if not top:
        body = (
            f"\n⚠️ <b>НЕ НАЙДЕНО активных серий</b> (streak ≥ {MIN_STREAK}).\n\n"
            f"<b>Диагностика данных:</b>\n"
            f"— Лиг найдено (сырых): <b>{leagues_seen}</b>\n"
            f"— Лиг использовано (с командами): <b>{diag_leagues_used}</b>\n"
            f"— Команд просмотрено: <b>{diag_teams_seen}</b>\n"
            f"— Команд с данными Q1/Q2: <b>{diag_teams_with_q12}</b>\n"
            f"— Всего матчей с Q1/Q2: <b>{diag_total_games_with_q12}</b>\n\n"
            f"Если «Лиг использовано = 0» — проблема в запросе лиг (country/season/type).\n"
            f"Если «Команд с Q1/Q2 почти нет» — API по этим лигам/сезону не отдаёт четверти.\n"
            f"Попробуй сезон вручную: <code>SEASON=2024</code> или укажи конкретные лиги: <code>LEAGUE_IDS=...</code>."
        )
        tg_send(title + body)
        return

    lines = []
    for i, r in enumerate(top, start=1):
        lines.append(
            f"{i}) <b>{r['team']}</b> — серия: <b>{r['streak']}</b>\n"
            f"   {r['country']} • {r['league']}"
        )

    footer = (
        f"\n\n<b>Диагностика:</b>\n"
        f"— Лиг найдено (сырых): <b>{leagues_seen}</b>\n"
        f"— Лиг использовано (с командами): <b>{diag_leagues_used}</b>\n"
        f"— Команд просмотрено: <b>{diag_teams_seen}</b>\n"
        f"— Команд с Q1/Q2: <b>{diag_teams_with_q12}</b>\n"
        f"— Всего матчей с Q1/Q2: <b>{diag_total_games_with_q12}</b>\n"
    )

    tg_send(title + "\n" + "\n\n".join(lines) + footer)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Падение с понятным текстом в логах Actions
        print(f"FATAL: {e}", file=sys.stderr)
        raise
