from parser import parse_company_year
from pathlib import Path
import os
import csv
import time
import shutil
import zipfile
from pathlib import Path
from typing import Optional, Dict, Any
import requests
import pandas as pd
from tqdm import tqdm

# ====== 0) Настройки папок ======
BASE_DIR = Path(__file__).resolve().parent   # папка, где лежит pipeline.py
DATA_DIR = BASE_DIR / "data"

TMP_DIR = DATA_DIR / "raw_tmp"
BAD_DIR = DATA_DIR / "bad_samples"
LOG_DIR = DATA_DIR / "logs"

OUT_CSV = DATA_DIR / "dataset_v1.csv"
TASKS_CSV = DATA_DIR / "tasks.csv"
LOG_FILE = LOG_DIR / "pipeline_log.csv"

for d in [TMP_DIR, BAD_DIR, LOG_DIR]:
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
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ====== 5) Логирование результата ======
def log_status(company_id: int, details_id: int, period: int, status: str, message: str = "") -> None:
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



# ====== 6) Обработка одной задачи ======
def process_one(session: requests.Session, company_id: int, details_id: int, period: int) -> Optional[Dict[str, Any]]:
    url = build_download_url(company_id, details_id, period)

    job_dir = TMP_DIR / f"{company_id}_{period}"
    job_dir.mkdir(parents=True, exist_ok=True)
    zip_path = job_dir / "bfo.zip"

    try:
        # 1) скачать ZIP
        download_zip(session, url, zip_path)

        # 2) распаковать и найти xlsx
        xlsx_path = extract_xlsx(zip_path, job_dir)

        # 3) распарсить
        row = parse_company_year(str(xlsx_path), company_id=company_id, period=period)

        if row is None:
            bad_target = BAD_DIR / f"{company_id}_{period}.xlsx"
            shutil.copy2(xlsx_path, bad_target)
            log_status(company_id, details_id, period, "SKIP", "parse_company_year returned None (missing key fields)")
            return None

        log_status(company_id, details_id, period, "OK", "")
        return row

    except Exception as e:
        try:
            xlsx_files = list(job_dir.rglob("*.xlsx"))
            if xlsx_files:
                shutil.copy2(xlsx_files[0], BAD_DIR / f"{company_id}_{period}_error.xlsx")
        except Exception:
            pass

        log_status(company_id, details_id, period, "ERROR", repr(e))
        return None

    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# ====== 7) Главный цикл пайплайна ======
def run_pipeline(limit: int = 1000, sleep_s: float = 0.3) -> None:
    tasks = pd.read_csv(TASKS_CSV, sep=";", encoding="utf-8-sig").head(limit)

    # нормализуем имя колонки details_id
    if "details_id" in tasks.columns:
        details_col = "details_id"
    elif "detailsId" in tasks.columns:
        details_col = "detailsId"
    else:
        raise ValueError(f"No details id column. Columns: {list(tasks.columns)}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://bo.nalog.gov.ru/",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    session.get("https://bo.nalog.gov.ru/", timeout=30)

    ok = 0
    skip = 0

    for _, t in tqdm(tasks.iterrows(), total=len(tasks)):
        company_id = int(t["company_id"])
        details_id = int(t[details_col])
        period = int(t["period"])

        row = process_one(session, company_id, details_id, period)  # <-- session
        if row is not None:
            append_row_to_csv(row, OUT_CSV)
            ok += 1
        else:
            skip += 1

        time.sleep(sleep_s)

    print(f"Done. OK={ok}, skipped_or_error={skip}. Output: {OUT_CSV}")

