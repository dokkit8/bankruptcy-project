from json_parser import parse_company_year_json
from pathlib import Path
import os
import csv
import time
from pathlib import Path
from typing import Optional, Dict, Any
import requests
import pandas as pd
from tqdm import tqdm

# ====== 0) Настройки папок ======
BASE_DIR = Path(__file__).resolve().parent   # папка, где лежит pipeline.py
DATA_DIR = BASE_DIR / "data"

LOG_DIR = DATA_DIR / "logs"

OUT_CSV = DATA_DIR / "dataset_v1.csv"
TASKS_CSV = DATA_DIR / "tasks.csv"
LOG_FILE = LOG_DIR / "pipeline_log.csv"

for d in [LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "pipeline_log.csv"


# ====== 1) Построение URL скачивания ======
def build_download_url(company_id: int, details_id: int, period: int) -> str:
    # CFO (fundsMovement) откладываем
    return (
        f"https://bo.nalog.gov.ru/download/bfo/{company_id}"
        f"?auditReport=false"
        f"&balance=true"
        f"&capitalChange=false"
        f"&clarification=false"
        f"&targetedFundsUsing=false"
        f"&detailsId={details_id}"
        f"&financialResult=true"
        f"&fundsMovement=false"
        f"&type=XLS"
        f"&period={period}"
    )


# ====== 2) Скачать ZIP ======
def download_zip(session: requests.Session, url: str, out_path, timeout: int = 240, retries: int = 3) -> None:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with session.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            f.write(chunk)
            return
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.HTTPError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise last_err



# ====== 3) Распаковать ZIP и найти XLSX ======
def extract_xlsx(zip_path: Path, extract_dir: Path) -> Path:
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extract_dir)

    # Ищем первый .xlsx в распакованном
    xlsx_files = list(extract_dir.rglob("*.xlsx"))
    if not xlsx_files:
        raise FileNotFoundError("В ZIP не найден .xlsx")
    return xlsx_files[0]


# ====== 4) Запись одной строки в CSV (дописывание) ======
def append_row_to_csv(row: Dict[str, Any], csv_path: Path) -> None:
    file_exists = csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ====== 5) Логирование результата ======
def log_status(company_id: int, details_id: Optional[int], period: int, status: str, message: str = "") -> None:
    row = {
        "company_id": company_id,
        "detailsId": details_id,
        "period": period,
        "status": status,
        "message": message[:500],
    }

    try:
        file_exists = LOG_FILE.exists()
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except PermissionError:
        # Файл лога занят (обычно Excel). Не валим весь пайплайн.
        # Пишем аварийный лог в отдельный файл.
        fallback = LOG_DIR / "pipeline_log_fallback.txt"
        with open(fallback, "a", encoding="utf-8") as f:
            f.write(f"{row}\n")


def okved_section(okved2_group: Optional[str]) -> Optional[str]:
    if not okved2_group:
        return None
    try:
        n = int(str(okved2_group)[:2])
    except:
        return None

    if 1 <= n <= 3:   return "A"
    if 5 <= n <= 9:   return "B"
    if 10 <= n <= 33: return "C"
    if 35 <= n <= 39: return "D"
    if 41 <= n <= 43: return "F"
    if 45 <= n <= 47: return "G"
    if 49 <= n <= 53: return "H"
    if 55 <= n <= 56: return "I"
    if 58 <= n <= 63: return "J"
    if 64 <= n <= 66: return "K"
    if 68 <= n <= 68: return "L"
    if 69 <= n <= 75: return "M"
    if 77 <= n <= 82: return "N"
    if 84 <= n <= 84: return "O"
    if 85 <= n <= 85: return "P"
    if 86 <= n <= 88: return "Q"
    if 90 <= n <= 93: return "R"
    if 94 <= n <= 96: return "S"
    if 97 <= n <= 98: return "T"
    if 99 <= n <= 99: return "U"
    return None


# ====== 6) Обработка одной задачи ======
def process_one(session: requests.Session,
                company_id: int,
                period: int,
                region: Optional[str],
                okved2_group: Optional[str]) -> Optional[Dict[str, Any]]:
    try:
        row = parse_company_year_json(session, company_id=company_id, period=period)

        if row is None:
            log_status(company_id, None, period, "SKIP", "parse_company_year_json returned None")
            return None

        # --- добавляем регион и оквэд из tasks ---
        row["region"] = region
        row["okved2"] = okved2_group
        row["okved_section"] = okved_section(okved2_group)

        # --- считаем коэффициенты ---
        def safe_div(a, b):
            if a is None or b is None or b == 0:
                return None
            return a / b

        rev = row.get("revenue_t")
        rev1 = row.get("revenue_t_1")
        np = row.get("net_profit_t")
        eq = row.get("equity_t")
        debt = row.get("debt_t")
        inv = row.get("inventory_t")

        row["margin"] = safe_div(np, rev)
        row["debt_ratio"] = safe_div(debt, eq)
        row["inv_turnover"] = safe_div(rev, inv)
        if rev is not None and rev1 is not None:
            row["growth_rev"] = safe_div(rev - rev1, rev1)
        else:
            row["growth_rev"] = None

        log_status(company_id, None, period, "OK", "")
        return row

    except Exception as e:
        log_status(company_id, None, period, "ERROR", repr(e))
        return None



# ====== 7) Главный цикл пайплайна ======
def run_pipeline(limit: int = 1000, sleep_s: float = 0.3) -> None:
    tasks = pd.read_csv(TASKS_CSV, sep=";", encoding="utf-8-sig").head(limit)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://bo.nalog.gov.ru/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    session.get("https://bo.nalog.gov.ru/", timeout=30)

    ok = 0
    skip = 0

    for _, t in tqdm(tasks.iterrows(), total=len(tasks)):
        company_id = int(t["company_id"])
        period = int(t["period"])

        region = str(t["region"]).strip().upper() if pd.notna(t["region"]) else None
        okved2_group = str(t["okved2"])[:2] if pd.notna(t["okved2"]) else None

        row = process_one(session, company_id, period, region, okved2_group)
        if row is not None:
            append_row_to_csv(row, OUT_CSV)
            ok += 1
        else:
            skip += 1

        time.sleep(sleep_s)

    print(f"Done. OK={ok}, skipped_or_error={skip}. Output: {OUT_CSV}")


