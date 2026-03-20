import os
import time
import zipfile
from pathlib import Path
import requests

BASE = "https://bo.nalog.gov.ru"
TIMEOUT_S = 60

def download_bfo_zip(session: requests.Session, company_id: int, details_id: int, period: int, out_zip: Path) -> None:
    url = f"{BASE}/download/bfo/{company_id}"
    params = {
        "auditReport": "false",
        "balance": "true",
        "capitalChange": "false",
        "clarification": "false",
        "targetedFundsUsing": "false",
        "financialResult": "true",
        "fundsMovement": "false",
        "type": "XLS",
        "period": str(period),
        "detailsId": str(details_id),   # <-- важное имя параметра
    }
    r = session.get(url, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(r.content)

def unzip_first_xlsx(zip_path: Path, out_xlsx: Path) -> None:
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        xlsx_names = [n for n in z.namelist() if n.lower().endswith(".xlsx")]
        if not xlsx_names:
            raise RuntimeError(f"No .xlsx inside zip: {zip_path}")
        name = xlsx_names[0]
        with z.open(name) as src, open(out_xlsx, "wb") as dst:
            dst.write(src.read())
