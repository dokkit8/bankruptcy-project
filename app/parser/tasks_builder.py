import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import requests

SEARCH_URL = "https://bo.nalog.gov.ru/advanced-search/organizations/search"
TARGET_YEARS = ["2022", "2023", "2024"]

OKOPF_OOO_ID = 12300  # ООО
# АО будем ловить по слову "акционер" в названии OKOPF (если доступно)

TIMEOUT_S = 30
SLEEP_S = 0.6
RETRIES = 4


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def norm_inn(s) -> str:
    """Только цифры. Безопасно для pd.NA/NaN/None."""
    if s is None:
        return ""
    # pd.NA / NaN
    try:
        if pd.isna(s):
            return ""
    except Exception:
        pass

    s = str(s)
    return re.sub(r"\D+", "", s).strip()



def is_ooo_or_ao(okopf_obj) -> bool:
    if okopf_obj is None:
        return False

    if isinstance(okopf_obj, int):
        return okopf_obj == OKOPF_OOO_ID

    if isinstance(okopf_obj, dict):
        okopf_id = okopf_obj.get("id")
        okopf_name = (okopf_obj.get("name") or "").lower()
        if okopf_id == OKOPF_OOO_ID:
            return True
        if "акционер" in okopf_name:
            return True
        return False

    return False


def request_json(session, url, params=None):
    last_err = None

    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT_S)

            # 404 — просто нет ресурса
            if r.status_code == 404:
                return None

            # Временные ошибки — ретраим
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.8 * attempt)
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            last_err = e
            time.sleep(0.5 * attempt)

    raise RuntimeError(f"Failed request after {RETRIES} retries: {last_err}")


def pick_exact_match(content: List[Dict[str, Any]], inn: str) -> Optional[Dict[str, Any]]:
    inn = norm_inn(inn)
    for item in content:
        item_inn = norm_inn(strip_html(str(item.get("inn", ""))))
        if item_inn == inn:
            return item
    return None


@dataclass
class BuildStats:
    processed: int = 0
    found_exact: int = 0
    passed_active: int = 0
    passed_okopf: int = 0
    passed_years: int = 0
    tasks_rows: int = 0
    skipped: int = 0


def get_org_info(session: requests.Session, company_id: int) -> Dict[str, Any]:
    url = f"https://bo.nalog.gov.ru/nbo/organizations/{company_id}"
    return request_json(session, url)


def get_org_bfo_list(session: requests.Session, company_id: int) -> List[Dict[str, Any]]:
    url = f"https://bo.nalog.gov.ru/nbo/organizations/{company_id}/bfo/"
    data = request_json(session, url)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected bfo response type: {type(data)}")
    return data


def build_period_map(bfo_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Возвращает map period -> {
        details_id: int,
        okved2: str|None,
        published: bool,
    }

    Берём typeCorrections.type == 12 (годовая отчетность) и correction.id как details_id.
    """
    out: Dict[str, Dict[str, Any]] = {}

    for entry in bfo_list:
        period = str(entry.get("period", "")).strip()
        if not period:
            continue

        published = bool(entry.get("published") is True)

        # okved2 по конкретному периоду — это важно
        org_info = entry.get("organizationInfo") or {}
        okved2_id = org_info.get("okved2_id")
        if okved2_id is None and isinstance(org_info.get("okved2"), dict):
            okved2_id = org_info["okved2"].get("id")

        # ищем корректировку годового типа
        type_corrections = entry.get("typeCorrections") or []
        details_id = None

        for tc in type_corrections:
            if not isinstance(tc, dict):
                continue
            if tc.get("type") != 12:
                continue
            corr = tc.get("correction") or {}
            # В твоём JSON corr.id == balance.id == financialResult.id
            if isinstance(corr.get("id"), int):
                details_id = int(corr["id"])
                break

        out[period] = {
            "details_id": details_id,
            "okved2": okved2_id,
            "published": published,
        }

    return out


def build_tasks(
    inns_csv_path: str,
    out_tasks_path: str = "data/tasks.csv",
    limit: int = 1000,
) -> Tuple[pd.DataFrame, BuildStats]:
    # ВАЖНО: читаем только одну колонку, иначе на больших файлах ты убиваешь память
    df_in = pd.read_csv(
        inns_csv_path,
        sep=";",
        usecols=["inn"],
        dtype={"inn": "string"},
        encoding="utf-8",
        engine="c",
    )

    inns = df_in["inn"].map(norm_inn)
    inns = [x for x in inns.tolist() if x]
    inns = inns[:limit]

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://bo.nalog.gov.ru/",
        "Origin": "https://bo.nalog.gov.ru",
        "Connection": "keep-alive",
    })

    # прогрев cookies
    warm = session.get("https://bo.nalog.gov.ru/", timeout=TIMEOUT_S)
    warm.raise_for_status()
    time.sleep(0.8)

    rows = []
    stats = BuildStats()

    for inn in inns:
        stats.processed += 1

        data = request_json(session, SEARCH_URL, params={"query": inn, "page": 0, "size": 20})
        content = data.get("content", []) or []

        item = pick_exact_match(content, inn)
        if item is None:
            stats.skipped += 1
            time.sleep(SLEEP_S)
            continue
        stats.found_exact += 1

        if item.get("statusCode") != "ACTIVE":
            stats.skipped += 1
            time.sleep(SLEEP_S)
            continue
        stats.passed_active += 1

        company_id = int(item["id"])

        # карточка организации
        org = get_org_info(session, company_id)
        if org is None:
            stats.skipped += 1
            continue
        if not is_ooo_or_ao(org.get("okopf")):
            stats.skipped += 1
            time.sleep(SLEEP_S)
            continue
        stats.passed_okopf += 1

        # bfo список (главный источник details_id)
        bfo_list = get_org_bfo_list(session, company_id)
        pmap = build_period_map(bfo_list)

        # строгая проверка: 2022-2024 должны быть и details_id должен существовать
        ok_years = True
        for y in TARGET_YEARS:
            rec = pmap.get(y)
            if not rec or rec.get("details_id") is None:
                ok_years = False
                break

        if not ok_years:
            stats.skipped += 1
            time.sleep(SLEEP_S)
            continue
        stats.passed_years += 1

        region = org.get("region")  # обычно строка типа "ВОЛОГОДСКАЯ" / "АДЫГЕЯ"

        okopf_obj = org.get("okopf") or {}
        okopf_id = okopf_obj.get("id") if isinstance(okopf_obj, dict) else okopf_obj

        for y in TARGET_YEARS:
            rec = pmap[y]
            rows.append({
                "inn": inn,
                "company_id": company_id,
                "period": int(y),
                "region": region,
                "okved2": rec.get("okved2"),
                "okopf_id": okopf_id,
                "details_id": rec.get("details_id"),
            })
            stats.tasks_rows += 1

        time.sleep(SLEEP_S)

    out_df = pd.DataFrame(rows)
    out_df.to_csv(out_tasks_path, index=False, sep=";", encoding="utf-8-sig")
    return out_df, stats


if __name__ == "__main__":
    df, st = build_tasks(
        inns_csv_path="data/balanced_inn.csv",
        out_tasks_path="data/tasks.csv",
        limit=15000,
    )
    print(df.head(10))
    print(st)
    print(f"Saved: data/tasks.csv (rows={len(df)})")
