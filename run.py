# run.py
# Basket Streak Bot: ищет активные серии по условию "total 1Q < total 2Q"
# Источник данных: API-Sports API-Basketball (v1.basketball.api-sports.io)
# Отправка: Telegram Bot API (через requests)

import os
import time
import requests
from datetime import datetime, timezone

API_HOST = "https://v1.basketball.api-sports.io"

# ВАЖНО: имена секретов должны совпадать 1-в-1
API_KEY = os.getenv("API_BASKETBALL_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Настройки
COUNTRIES = ["Spain", "Italy", "Turkey"]          # страны
LAST_GAMES_WINDOW = 15                           # окно последних матчей на команду
MIN_STREAK = 1                                   # серия считается, если >= MIN_STREAK
TOP_N = 10                                       # сколько команд показывать

# Таймауты
HTTP_TIMEOUT = 30


def api_get(endpoint: str, params: dict | None = None) -> dict:
    """GET к API-Sports с проверкой ошибок."""
    url = f"{API_HOST}/{endpoint.lstrip('/')}"
    headers = {
        "x-rapidapi-key": API_KEY,
        "x-rapidapi-host": "v1.basketball.api-sports.io",
    }
    r = requests.get(url, headers=headers, params=params or {}, timeout=HTTP_TIMEOUT)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"API error: non-JSON response ({r.status_code}): {r.text[:200]}")

    if r.status_code != 200:
        raise RuntimeError(f"API HTTP {r.status_code}: {data}")

    # API-Sports обычно кладёт ошибки сюда
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(f"API error: {data['errors']}")

    return data


def telegram_send(text: str) -> None:
    """Отправка сообщения в Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage failed: HTTP {r.status_code} {r.text[:300]}")


def get_latest_season() -> int:
    """
    В API-Basketball endpoint /seasons обычно НЕ принимает league.
    Поэтому берём просто максимальный год из списка сезонов.
    """
    data = api_get("seasons")
    seasons = data.get("response", [])

    # seasons может быть список int или str, приводим к int где можно
    ints = []
    for s in seasons:
        try:
            ints.append(int(s))
        except Exception:
            continue

    if not ints:
        raise RuntimeError("Не удалось получить список сезонов /seasons (пусто или неизвестный формат).")

    return max(ints)


def get_leagues(country: str) -> list[dict]:
    """
    ВАЖНО: чтобы API реально отдало лиги, часто нужно type=league
    (иначе может вернуться пусто).
    """
    data = api_get("leagues", params={"country": country, "type": "league"})
    leagues = data.get("response", [])
    return leagues


def get_teams(league_id: int, season: int) -> list[dict]:
    data = api_get("teams", params={"league": league_id, "season": season})
    return data.get("response", [])


def get_last_games_with_q(league_id: int, season: int, team_id: int, last_n: int) -> list[dict]:
    """
    Берём последние матчи команды.
    Не все лиги отдают разбивку по четвертям — тогда матч будет без Q1/Q2.
    """
    data = api_get(
        "games",
        params={
            "league": league_id,
            "season": season,
            "team": team_id,
            "last": last_n,
        },
    )
    games = data.get("response", [])

    # Оставляем только завершённые (на всякий)
    filtered = []
    for g in games:
        status = (g.get("status") or {}).get("short") or (g.get("status") or {}).get("long")
        # часто FT
        if status in ("FT", "AOT", "FTOT") or status is None:
            filtered.append(g)
    return filtered


def extract_q1_q2_totals(game: dict) -> tuple[int | None, int | None]:
    """
    Пытаемся достать total Q1 и total Q2 (home+away) из разных возможных форматов.
    Возвращает (q1_total, q2_total) или (None, None) если нет данных.
    """
    scores = game.get("scores") or {}
    home = scores.get("home") or {}
    away = scores.get("away") or {}

    # Формат 1: quarters как dict {"1": 20, "2": 25, ...} или {"1": {"total": 20}, ...}
    for side in (home, away):
        if isinstance(side, dict) and "quarters" in side and isinstance(side["quarters"], dict):
            pass
        else:
            # нет quarters в этом формате
            break
    else:
        def pick_q(side: dict, q: str) -> int | None:
            qv = (side.get("quarters") or {}).get(q)
            if qv is None:
                return None
            if isinstance(qv, (int, float)):
                return int(qv)
            if isinstance(qv, dict):
                # иногда {"total": 20}
                tv = qv.get("total")
                if isinstance(tv, (int, float)):
                    return int(tv)
            return None

        h1, h2 = pick_q(home, "1"), pick_q(home, "2")
        a1, a2 = pick_q(away, "1"), pick_q(away, "2")
        if all(v is not None for v in (h1, h2, a1, a2)):
            return int(h1 + a1), int(h2 + a2)

    # Формат 2: home/away могут быть как {"quarter_1": 20, "quarter_2": 25, ...}
    def pick_alt(side: dict, key: str) -> int | None:
        v = side.get(key)
        if isinstance(v, (int, float)):
            return int(v)
        return None

    h1 = pick_alt(home, "quarter_1")
    h2 = pick_alt(home, "quarter_2")
    a1 = pick_alt(away, "quarter_1")
    a2 = pick_alt(away, "quarter_2")
    if all(v is not None for v in (h1, h2, a1, a2)):
        return int(h1 + a1), int(h2 + a2)

    return None, None


def condition_total_q1_lt_q2(game: dict) -> bool | None:
    q1, q2 = extract_q1_q2_totals(game)
    if q1 is None or q2 is None:
        return None
    return q1 < q2


def compute_active_streak(games_latest_first: list[dict]) -> tuple[int, int, int]:
    """
    Считает активную серию по условию на последних матчах (начиная с самого свежего).
    Возвращает (streak_len, games_with_q, total_games)
    """
    streak = 0
    games_with_q = 0
    total_games = len(games_latest_first)

    for g in games_latest_first:
        ok = condition_total_q1_lt_q2(g)
        if ok is None:
            # матч без Q1/Q2 — просто пропускаем, но учитываем отдельно
            continue

        games_with_q += 1
        if ok:
            if streak == games_with_q - 1:
                # продолжаем серию, если все предыдущие "Q-матчи" были успешны
                streak += 1
        else:
            # серия обрывается на первом же "проваленном" матче с квартерами
            break

    return streak, games_with_q, total_games


def main() -> None:
    # Проверка секретов
    if not API_KEY:
        raise RuntimeError("Missing API_BASKETBALL_KEY secret")
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN secret")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID secret")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 1) сезон
    season = get_latest_season()

    leagues_checked = 0
    teams_checked = 0
    teams_with_q_data = 0
    games_total_with_q = 0

    results = []  # (streak, games_with_q, country, league_name, team_name, league_id, team_id)

    # 2) по странам -> лиги -> команды
    for country in COUNTRIES:
        leagues = get_leagues(country)

        # если API не возвращает лиги — это ключевая проблема
        if not leagues:
            continue

        for league_obj in leagues:
            league = league_obj.get("league") or {}
            league_id = league.get("id")
            league_name = league.get("name") or "Unknown League"

            if not isinstance(league_id, int):
                continue

            leagues_checked += 1

            # команды
            try:
                teams = get_teams(league_id, season)
            except Exception:
                # иногда по текущему сезону команд нет — пропускаем
                continue

            for t in teams:
                team = t.get("team") or {}
                team_id = team.get("id")
                team_name = team.get("name") or "Unknown Team"
                if not isinstance(team_id, int):
                    continue

                teams_checked += 1

                # последние матчи
                try:
                    games = get_last_games_with_q(league_id, season, team_id, LAST_GAMES_WINDOW)
                except Exception:
                    continue

                # сортируем от свежего к старому по дате, если есть
                def game_ts(g: dict) -> float:
                    dt = g.get("date")
                    if isinstance(dt, str):
                        # API-Sports обычно ISO: "2026-02-22T10:00:00+00:00"
                        try:
                            return datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            return 0.0
                    return 0.0

                games_sorted = sorted(games, key=game_ts, reverse=True)

                streak, games_with_q, total_games = compute_active_streak(games_sorted)

                if games_with_q > 0:
                    teams_with_q_data += 1
                    games_total_with_q += games_with_q

                if streak >= MIN_STREAK:
                    results.append(
                        (streak, games_with_q, country, league_name, team_name, league_id, team_id)
                    )

                # чуть притормозим, чтобы API не ругалось на лимиты
                time.sleep(0.15)

    # 3) формируем TOP
    results.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = results[:TOP_N]

    header = (
        f"<b>TOP-{TOP_N} ACTIVE STREAK — total 1Q &lt; total 2Q</b>\n"
        f"🕒 {now_utc}\n"
        f"Сезон: <b>{season}</b>\n"
        f"Окно: последние <b>{LAST_GAMES_WINDOW}</b> матчей\n\n"
    )

    diag = (
        f"Диагностика данных:\n"
        f"— Лиг просмотрено: <b>{leagues_checked}</b>\n"
        f"— Команд просмотрено: <b>{teams_checked}</b>\n"
        f"— Команд с данными Q1/Q2: <b>{teams_with_q_data}</b>\n"
        f"— Всего матчей с Q1/Q2: <b>{games_total_with_q}</b>\n"
    )

    if not top:
        text = (
            header
            + "⚠️ <b>НЕ НАЙДЕНО активных серий</b> (streak ≥ "
            + str(MIN_STREAK)
            + ").\n\n"
            + diag
            + "\nЕсли <b>лиг/команд = 0</b> — проблема в запросе лиг (страна/type).\n"
            + "Если <b>Q1/Q2 почти нет</b> — API по этим лигам/сезонам не отдаёт четверти."
        )
        telegram_send(text)
        return

    lines = []
    for i, (streak, games_with_q, country, league_name, team_name, league_id, team_id) in enumerate(top, start=1):
        lines.append(
            f"{i}) <b>{streak}</b> / {games_with_q} — {team_name} — {country} — {league_name}"
        )

    text = header + "\n".join(lines) + "\n\n" + diag
    telegram_send(text)


if __name__ == "__main__":
    main()
