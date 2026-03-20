# details_resolver.py
import re
import time
from dataclasses import dataclass
from typing import Dict, Any, Optional, List, Tuple

import pandas as pd
import requests


TIMEOUT_S = 30
SLEEP_S = 0.6
RETRIES = 4


DETAILS_TEST_URL = "https://bo.nalog.gov.ru/nbo/details/financial_result"
ORG_JSON_URL_TMPL = "https://bo.nalog.gov.ru/nbo/organizations/{company_id}"

# ВАЖНО: Это HTML-страница организации (именно она часто содержит initial state)
ORG_HTML_URLS_TMPL = [
    "https://bo.nalog.gov.ru/organizations/{company_id}",   # чаще всего UI
    "https://bo.nalog.gov.ru/nbo/organizations/{company_id}",  # иногда редирект/HTML
]


def request_text(session: requests.Session, url: str) -> str:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT_S)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(0.4 * attempt)
    raise RuntimeError(f"Failed request_text after {RETRIES} retries: {last_err}")


def request_json(session: requests.Session, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT_S)
            r.raise_for_status()
            # Иногда сайт отдаёт пусто/HTML при блокировке — ловим это явно
            if not r.text or r.text.strip() == "":
                raise ValueError("Empty response body")
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.4 * attempt)
    raise RuntimeError(f"Failed request_json after {RETRIES} retries: {last_err}")


def looks_like_details_json(obj: Any) -> bool:
    """
    Пытаемся понять, что это похоже на JSON детали отчёта,
    а не на какую-то заглушку.
    """
    if not isinstance(obj, (dict, list)):
        return False
    if isinstance(obj, list):
        return len(obj) > 0
    # dict
    if len(obj.keys()) == 0:
        return False
    # иногда там бывают поля типа "lines" / "items" / "period" / "form" — но не гарантируем
    return True


def validate_details_id(session: requests.Session, details_id: int) -> bool:
    """
    Проверяем candidate id через реальный endpoint.
    """
    try:
        data = request_json(session, DETAILS_TEST_URL, params={"id": details_id})
        return looks_like_details_json(data)
    except Exception:
        return False


def extract_candidates_from_html(html: str, year: str) -> List[int]:
    """
    Достаём числа, которые выглядят как id рядом с нужным period/year.
    Евристика: ищем "period":"2024" (или 'period':'2024') и берём числа id вокруг.
    """
    candidates: List[int] = []
    if not html:
        return candidates

    # 1) Окно после period
    # берем кусок текста после упоминания года и вытаскиваем оттуда числа 6-9 знаков
    # (detailsId обычно довольно большой)
    for m in re.finditer(rf'period"\s*:\s*"{re.escape(year)}"|period\'\s*:\s*\'{re.escape(year)}\'|period\s*:\s*"{re.escape(year)}"', html):
        start = m.start()
        window = html[start:start + 600]  # дальше обычно лежат поля этой записи
        # ищем "id": 51994870 или id=51994870
        for mm in re.finditer(r'("id"\s*:\s*([0-9]{6,10}))|(id\s*=\s*([0-9]{6,10}))|(detailsId"\s*:\s*([0-9]{6,10}))', window):
            nums = [g for g in mm.groups() if g and g.isdigit()]
            for n in nums:
                candidates.append(int(n))

        # просто числа 6-10 знаков, если вдруг ключи минифицированы
        for mm in re.finditer(r'([0-9]{6,10})', window):
            candidates.append(int(mm.group(1)))

    # 2) Окно до period (иногда id стоит раньше)
    for m in re.finditer(rf'"{re.escape(year)}"', html):
        start = max(0, m.start() - 600)
        window = html[start:m.start() + 200]
        for mm in re.finditer(r'("id"\s*:\s*([0-9]{6,10}))|(detailsId"\s*:\s*([0-9]{6,10}))', window):
            nums = [g for g in mm.groups() if g and g.isdigit()]
            for n in nums:
                candidates.append(int(n))

    # Уникализируем, отсортируем по “правдоподобию” (больше = чаще details id)
    uniq = sorted(set(candidates), reverse=True)
    return uniq


def fetch_org_html(session: requests.Session, company_id: int) -> Optional[str]:
    for tmpl in ORG_HTML_URLS_TMPL:
        url = tmpl.format(company_id=company_id)
        try:
            html = request_text(session, url)
            # Грубая проверка, что это HTML страницы (а не JSON карточки)
            if "<html" in html.lower() or "DOCTYPE html".lower() in html.lower() or "window" in html.lower():
                return html
            # иногда это всё равно HTML без <html> (минифицировано) — оставим шанс
            if "static/js" in html or "main." in html:
                return html
        except Exception:
            continue
    return None


@dataclass
class ResolveStats:
    total_pairs: int = 0
    resolved: int = 0
    unresolved: int = 0
    validated_hits: int = 0


def resolve_details_ids(tasks_in_path: str, tasks_out_path: str) -> Tuple[pd.DataFrame, ResolveStats]:
    df = pd.read_csv(tasks_in_path, sep=";", dtype={"inn": "string"})
    need_cols = {"company_id", "period"}
    missing = need_cols - set(df.columns)
    if missing:
        raise ValueError(f"tasks.csv missing columns: {missing}. Have: {list(df.columns)}")

    # нормализуем типы
    df["company_id"] = df["company_id"].astype("int64")
    df["period"] = df["period"].astype("int64")

    # уникальные пары
    pairs = df[["company_id", "period"]].drop_duplicates().sort_values(["company_id", "period"])
    stats = ResolveStats(total_pairs=len(pairs))

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://bo.nalog.gov.ru/",
        "Origin": "https://bo.nalog.gov.ru",
        "Connection": "keep-alive",
    })

    # прогрев
    warm = session.get("https://bo.nalog.gov.ru/", timeout=TIMEOUT_S)
    warm.raise_for_status()
    time.sleep(1.0)

    cache: Dict[Tuple[int, int], Optional[int]] = {}

    for i, row in pairs.iterrows():
        company_id = int(row["company_id"])
        period = int(row["period"])
        key = (company_id, period)

        # кеш
        if key in cache:
            continue

        year = str(period)

        # 1) тянем HTML страницы организации
        html = fetch_org_html(session, company_id)
        if not html:
            cache[key] = None
            stats.unresolved += 1
            time.sleep(SLEEP_S)
            continue

        # 2) кандидаты
        candidates = extract_candidates_from_html(html, year)
        if not candidates:
            cache[key] = None
            stats.unresolved += 1
            time.sleep(SLEEP_S)
            continue

        # 3) проверка кандидатов (ограничим, чтобы не DDOS)
        found: Optional[int] = None
        for cand in candidates[:60]:
            if validate_details_id(session, cand):
                found = cand
                stats.validated_hits += 1
                break

        cache[key] = found
        if found is None:
            stats.unresolved += 1
        else:
            stats.resolved += 1

        if stats.resolved + stats.unresolved <= 10:
            print(f"[debug] company_id={company_id} period={period} -> details_id={found} (cands={len(candidates)})")

        time.sleep(SLEEP_S)

    # проставляем в df
    df["details_id"] = df.apply(lambda r: cache.get((int(r["company_id"]), int(r["period"]))) , axis=1)

    # сохраняем (UTF-8-SIG, чтобы Excel не уродовал русский)
    df.to_csv(tasks_out_path, index=False, sep=";", encoding="utf-8-sig")
    return df, stats


if __name__ == "__main__":
    out_df, st = resolve_details_ids("data/tasks.csv", "data/tasks_with_details.csv")
    print(out_df.head(20))
    print(st)
    print(f"Saved: data/tasks_with_details.csv (rows={len(out_df)})")
