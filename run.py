import os
import sys
import time
import json
import requests
from datetime import datetime, timezone
from collections import defaultdict

API_HOST = os.getenv("API_HOST", "https://v1.basketball.api-sports.io").rstrip("/")
API_KEY = os.getenv("API_BASKETBALL_KEY") or os.getenv("API_KEY")  # запасной вариант
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Сезон (на free-плане часто доступно 2022-2024)
SEASON = os.getenv("SEASON", "2024").strip()

# Страны (можно менять через env COUNTRIES="Spain,Turkey,Italy")
COUNTRIES = [c.strip() for c in os.getenv("COUNTRIES", "Spain,Turkey,Italy").split(",") if c.strip()]

# Окно матчей
WINDOW_LAST_GAMES = int(os.getenv("WINDOW_LAST_GAMES", "15"))

# Условие серии: total 1Q < total 2Q
CONDITION_NAME = "total 1Q < total 2Q"

# Диагностика
DEBUG = os.getenv("DEBUG", "0").strip() in ("1", "true", "True", "YES", "yes")


def die(msg: str, code: int = 1):
    print("FATAL:", msg)
    tg_send(f"❌ FATAL: {msg}")
    sys.exit(code)


def tg_send(text: str):
    """Отправка сообщения в Telegram (если задан токен и чат)."""
    if not TG_TOKEN or not TG_CHAT_ID:
        # не падаем — просто пишем в лог
        print("[TG] skip (no token/chat_id):", text[:200])
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text}
        r = requests.post(url, json=payload, timeout=30)
        if not r.ok:
            print("[TG] send failed:", r.status_code, r.text[:300])
    except Exception as e:
        print("[TG] exception:", repr(e))


def api_get(endpoint: str, params: dict | None = None, retries: int = 3, sleep_sec: float = 1.2):
    """GET к API-SPORTS basketball."""
    if not API_KEY:
        raise RuntimeError("Missing API_BASKETBALL_KEY secret")

    url = f"{API_HOST}/{endpoint.lstrip('/')}"
    headers = {
        "x-apisports-key": API_KEY,
        "Accept": "application/json",
    }

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=40)
            # Иногда API возвращает 200, но с errors внутри
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {str(data)[:300]}")

            # Нормализуем ошибки API-Sports
            errors = data.get("errors")
            if errors:
                raise RuntimeError(f"API error: {errors}")

            return data
        except Exception as e:
            last_err = e
            print(f"[api_get] attempt {attempt}/{retries} failed:", repr(e))
            time.sleep(sleep_sec * attempt)

    raise RuntimeError(str(last_err))


def pretty_dt_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def safe_str(x, max_len=200):
    s = str(x)
    return s if len(s) <= max_len else s[:max_len] + "…"


def debug_block(title: str, obj):
    """Печатает в логи и при DEBUG отправляет краткий блок в телеграм."""
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)
    print(safe_str(obj, 2000))

    if DEBUG:
        # В телеграм шлём только кратко
        if isinstance(obj, (dict, list)):
            snippet = safe_str(json.dumps(obj, ensure_ascii=False)[:1500], 1500)
        else:
            snippet = safe_str(obj, 1500)
        tg_send(f"🧪 DEBUG: {title}\n{snippet}")


def get_leagues_for_country(country: str, season: str):
    """
    В API-Basketball у /leagues параметры чувствительны.
    Обычно работает: country + season + type=league
    """
    params = {"country": country, "season": season, "type": "league"}
    data = api_get("leagues", params=params)
    resp = data.get("response", [])
    return resp, params


def get_leagues_any(season: str):
    """Пробуем получить хоть какие-то лиги по сезону (fallback)."""
    params = {"season": season}
    data = api_get("leagues", params=params)
    return data.get("response", []), params


def extract_league_info(item):
    """Нормализуем структуру league."""
    league = item.get("league") or {}
    country = item.get("country") or {}
    seasons = item.get("seasons") or []
    return {
        "league_id": league.get("id"),
        "league_name": league.get("name"),
        "league_type": league.get("type"),
        "country": country.get("name"),
        "season_count": len(seasons),
    }


def get_teams(league_id: int, season: str):
    params = {"league": league_id, "season": season}
    data = api_get("teams", params=params)
    return data.get("response", [])


def get_last_games(team_id: int, league_id: int, season: str, last_n: int):
    params = {"team": team_id, "league": league_id, "season": season, "last": last_n}
    data = api_get("games", params=params)
    return data.get("response", [])


def get_q_scores(game):
    """
    Пытаемся вытащить очки по четвертям.
    В API-Sports basketball обычно: game['scores']['home']['quarter_1'] / ['quarter_2'] и т.п.
    Но на разных лигах может быть иначе — делаем максимально устойчиво.
    """
    scores = game.get("scores") or {}
    home = scores.get("home") or {}
    away = scores.get("away") or {}

    # варианты ключей
    q1_keys = ["quarter_1", "q1", "1"]
    q2_keys = ["quarter_2", "q2", "2"]

    def pick(d, keys):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return None

    h1 = pick(home, q1_keys)
    h2 = pick(home, q2_keys)
    a1 = pick(away, q1_keys)
    a2 = pick(away, q2_keys)

    # Часть API может отдавать строками
    def to_int(v):
        if v is None:
            return None
        try:
            return int(v)
        except Exception:
            return None

    h1, h2, a1, a2 = map(to_int, (h1, h2, a1, a2))
    if None in (h1, h2, a1, a2):
        return None

    total_1q = h1 + a1
    total_2q = h2 + a2
    return total_1q, total_2q


def calc_streak(games):
    """
    Считаем активную серию: идём от самых последних матчей назад
    пока условие выполняется.
    """
    streak = 0
    used = 0
    with_quarters = 0

    for g in games:
        used += 1
        q = get_q_scores(g)
        if q is None:
            continue
        with_quarters += 1
        t1, t2 = q
        if t1 < t2:
            streak += 1
        else:
            break

    return streak, used, with_quarters


def main():
    # проверки секретов
    if not API_KEY:
        die("Missing API_BASKETBALL_KEY secret (Settings → Secrets and variables → Actions).")
    if not TG_TOKEN or not TG_CHAT_ID:
        # Не падаем, но предупреждаем
        print("WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing; Telegram send will be skipped.")

    # стартовый лог
    header = (
        f"🏀 TOP-10 ACTIVE STREAK — {CONDITION_NAME}\n"
        f"🕒 {pretty_dt_utc()}\n"
        f"Сезон: {SEASON}\n"
        f"Окно: последние {WINDOW_LAST_GAMES} матчей\n"
        f"Страны: {', '.join(COUNTRIES) if COUNTRIES else '(нет)'}"
    )
    print(header)
    tg_send(header)

    leagues = []
    leagues_debug = []

    # 1) Пытаемся получить лиги по странам
    for country in COUNTRIES:
        try:
            resp, used_params = get_leagues_for_country(country, SEASON)
            leagues_debug.append({"country": country, "params": used_params, "count": len(resp)})
            leagues.extend(resp)
        except Exception as e:
            leagues_debug.append({"country": country, "error": str(e)})
            print("Leagues error for country:", country, repr(e))

    debug_block("Leagues per country (attempt #1)", leagues_debug)

    # 2) Если лиг 0 — пробуем fallback (без country/type)
    if len(leagues) == 0:
        try:
            resp_any, params_any = get_leagues_any(SEASON)
            sample = [extract_league_info(x) for x in resp_any[:10]]
            debug_block(
                "Fallback leagues(any) — API returns these leagues for season",
                {"params": params_any, "count": len(resp_any), "sample_first_10": sample},
            )
        except Exception as e:
            debug_block("Fallback leagues(any) failed", str(e))

    # Уникализируем по id
    uniq = {}
    for item in leagues:
        li = item.get("league") or {}
        lid = li.get("id")
        if lid:
            uniq[lid] = item
    leagues = list(uniq.values())

    # Если лиг нет — отправляем понятную подсказку
    if len(leagues) == 0:
        msg = (
            "⚠️ НЕ НАЙДЕНО ЛИГ (leagues=0).\n\n"
            "Это почти всегда означает:\n"
            "1) По выбранным странам/сезону free-план не отдаёт лиги, или\n"
            "2) Неподдерживаемый сезон.\n\n"
            "Что сделать:\n"
            "• Поставь SEASON=2022 или 2023 или 2024\n"
            "• Попробуй COUNTRIES=USA,France,Germany\n"
            "• Или включи DEBUG=1 — я уже показываю fallback список в логах.\n"
        )
        tg_send(msg)
        print(msg)
        return

    # Отладочный список первых лиг
    leagues_info = [extract_league_info(x) for x in leagues[:10]]
    debug_block("Leagues found (first 10)", leagues_info)

    # Далее анализ команд
    leagues_seen = 0
    teams_seen = 0
    teams_with_q = 0
    total_games_with_q = 0

    results = []  # (streak, team_name, country, league_name, league_id)

    for item in leagues:
        league = item.get("league") or {}
        country = item.get("country") or {}
        lid = league.get("id")
        lname = league.get("name")
        cname = country.get("name")

        if not lid:
            continue

        leagues_seen += 1

        # Список команд
        try:
            teams = get_teams(lid, SEASON)
        except Exception as e:
            print("Teams error:", lid, lname, repr(e))
            continue

        for t in teams:
            team = t.get("team") or {}
            tid = team.get("id")
            tname = team.get("name")
            if not tid or not tname:
                continue

            teams_seen += 1

            # Последние матчи
            try:
                games = get_last_games(tid, lid, SEASON, WINDOW_LAST_GAMES)
            except Exception as e:
                print("Games error:", tid, tname, repr(e))
                continue

            streak, used_cnt, with_q = calc_streak(games)
            if with_q > 0:
                teams_with_q += 1
                total_games_with_q += with_q

            if streak >= 1:
                results.append((streak, tname, cname, lname, lid))

    results.sort(key=lambda x: x[0], reverse=True)
    top = results[:10]

    # Формируем итог
    lines = []
    if not top:
        lines.append(f"⚠️ НЕ НАЙДЕНО активных серий (streak>=1).")
    else:
        for i, (streak, tname, cname, lname, lid) in enumerate(top, 1):
            lines.append(f"{i}) {tname} — серия: {streak} | {cname} / {lname}")

    diag = (
        "\n\nДиагностика данных:\n"
        f"— Лиг просмотрено: {leagues_seen}\n"
        f"— Команд просмотрено: {teams_seen}\n"
        f"— Команд с данными Q1/Q2: {teams_with_q}\n"
        f"— Всего матчей с Q1/Q2: {total_games_with_q}\n\n"
        "Подсказка:\n"
        "Если лиг/команд = 0 — проблема в запросе лиг (country/season/type) или лимитах free-плана.\n"
        "Если Q1/Q2 почти нет — API по этим лигам/сезону не отдаёт четверти.\n"
    )

    final_msg = header + "\n\n" + "\n".join(lines) + diag
    tg_send(final_msg)
    print(final_msg)


if __name__ == "__main__":
    main()
