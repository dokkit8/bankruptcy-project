# json_parser.py
import time
from typing import Any, Dict, Optional
import requests


BFO_URL = "https://bo.nalog.gov.ru/nbo/organizations/{company_id}/bfo/"
TIMEOUT_S = 30
RETRIES = 3

SCALE_DEFAULT = 1000  # как и в Excel ("тыс. руб." -> рубли)

def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None

def _request_json(session: requests.Session, url: str) -> Any:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT_S)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.5 * attempt)
    raise RuntimeError(f"Failed request after {RETRIES} retries: {last_err}")

def _pick_period_block(bfo_list: list, period: int) -> Optional[dict]:
    p = str(period)
    for item in bfo_list:
        if str(item.get("period")) == p:
            return item
    return None

def _pick_annual_correction(period_item: dict) -> Optional[dict]:
    """
    Ищем type=12 (годовая). Внутри лежит correction.balance и correction.financialResult.
    """
    tcs = period_item.get("typeCorrections") or []
    for tc in tcs:
        if tc.get("type") == 12:
            corr = tc.get("correction") or {}
            return corr
    return None

def parse_company_year_json(session: requests.Session, company_id: int, period: int) -> Optional[Dict[str, Any]]:
    url = BFO_URL.format(company_id=company_id)
    bfo_list = _request_json(session, url)
    if not isinstance(bfo_list, list) or not bfo_list:
        return None

    period_item = _pick_period_block(bfo_list, period)
    if period_item is None:
        return None

    corr = _pick_annual_correction(period_item)
    if corr is None:
        return None

    bal = corr.get("balance") or {}
    fin = corr.get("financialResult") or {}

    # мета (может пригодиться; если в tasks уже есть — ок, но дублирование не ломает)
    org = period_item.get("organizationInfo") or {}
    inn = org.get("inn")
    okved2 = (org.get("okved2") or {}).get("id") if isinstance(org.get("okved2"), dict) else org.get("okved2_id")
    # нормализация okved до 2 цифр
    okved2_full = str(okved2 or "").strip()
    if okved2_full:
        okved2_group = okved2_full[:2]
    else:
        okved2_group = None
    okopf_id = org.get("okopf_id") or (org.get("okopf") or {}).get("id")

    # helper: взять current/previous по коду строки
    def get_pair(prefix: str, code: str):
        cur = _num(prefix == "fin" and fin.get(f"current{code}") or bal.get(f"current{code}"))
        prev = _num(prefix == "fin" and fin.get(f"previous{code}") or bal.get(f"previous{code}"))
        cur = None if cur is None else cur * SCALE_DEFAULT
        prev = None if prev is None else prev * SCALE_DEFAULT
        return cur, prev

    revenue_t, revenue_t_1 = get_pair("fin", "2110")
    net_profit_t, net_profit_t_1 = get_pair("fin", "2400")

    inventory_t, inventory_t_1 = get_pair("bal", "1210")
    payables_t, payables_t_1 = get_pair("bal", "1520")
    equity_t, equity_t_1 = get_pair("bal", "1300")

    debt_long_t, debt_long_t_1 = get_pair("bal", "1410")
    debt_short_t, debt_short_t_1 = get_pair("bal", "1510")

    def sum_nullable(a, b):
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    debt_t = sum_nullable(debt_long_t, debt_short_t)
    debt_t_1 = sum_nullable(debt_long_t_1, debt_short_t_1)

    # ключевые поля (как и раньше): если нет — считаем строку неполной
    # можешь ужесточить/ослабить правило, но сейчас оставим как было.
    if revenue_t is None or net_profit_t is None or equity_t is None:
        return None

    return {
        "company_id": company_id,
        "inn": inn,
        "okved2": okved2_group,
        "okopf_id": okopf_id,
        "period": period,
        "scale": SCALE_DEFAULT,

        "revenue_t": revenue_t,
        "revenue_t_1": revenue_t_1,

        "net_profit_t": net_profit_t,
        "net_profit_t_1": net_profit_t_1,

        "inventory_t": inventory_t,
        "inventory_t_1": inventory_t_1,

        "payables_t": payables_t,
        "payables_t_1": payables_t_1,

        "equity_t": equity_t,
        "equity_t_1": equity_t_1,

        "debt_t": debt_t,
        "debt_t_1": debt_t_1,
    }
