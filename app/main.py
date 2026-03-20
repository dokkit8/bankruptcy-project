from typing import Dict, Optional, Tuple, List
from datetime import timedelta, datetime, timezone
import json
import math
import hashlib

from pathlib import Path
try:
    import requests
except ImportError:
    raise RuntimeError(
        "The 'requests' library is not installed. Install it with: pip install requests"
    )

from pandas import DataFrame
from joblib import load
from fastapi import FastAPI, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Base, engine
from app.deps import get_db, get_current_user, get_optional_user
from app.models import User, Prediction
from app.schemas import UserCreate, UserLogin
from app.security import create_access_token, hash_password, verify_password
from app.parser.json_parser import parse_company_year_json

app = FastAPI(title="Система прогнозирования банкротства")

# Serve static assets from the /app/static directory
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Configure Jinja2 templates directory
templates = Jinja2Templates(directory="app/templates")

settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent

# primary location (parser dataset folder)
DATA_DIR = BASE_DIR / "parser" / "data"
NORM_TABLE_PATH = DATA_DIR / "norm_table.csv"

# fallback locations (when parser/dataset folder was copied differently)
ALT_NORM_PATHS = [
    BASE_DIR.parent / "data" / "norm_table.csv",          # project_root/data
    BASE_DIR / "parser" / "data" / "norm_table.csv",     # app/parser/data
    BASE_DIR.parent / "parser" / "data" / "norm_table.csv",  # project_root/parser/data
]

SEARCH_URL = "https://bo.nalog.gov.ru/advanced-search/organizations/search"
BFO_REFERER = "https://bo.nalog.gov.ru/"

analysis_norms: Optional[DataFrame] = None


def load_analysis_sources() -> Optional[DataFrame]:
    global analysis_norms

    if analysis_norms is not None:
        return analysis_norms

    import pandas as pd

    debug_messages = []
    paths_to_try = [NORM_TABLE_PATH] + ALT_NORM_PATHS

    for path in paths_to_try:
        path = Path(path)
        debug_messages.append(f"try path={path} exists={path.exists()}")
        if not path.exists():
            continue

        for enc in ("utf-8-sig", "utf-8", "cp1251"):
            try:
                # try both common separators because many Russian CSV exports use ';'
                try:
                    df = pd.read_csv(path, encoding=enc, sep=";")
                    if len(df.columns) == 1:
                        # fallback if separator actually comma
                        df = pd.read_csv(path, encoding=enc)
                except Exception:
                    df = pd.read_csv(path, encoding=enc)
                debug_messages.append(f"loaded path={path} encoding={enc} columns={list(df.columns)}")

                required_cols = {"level", "period", "key"}
                if not required_cols.issubset(set(df.columns)):
                    debug_messages.append(
                        f"path={path} encoding={enc} missing required columns {required_cols - set(df.columns)}"
                    )
                    continue

                analysis_norms = df
                print(f"Loaded norm table from: {path} (encoding={enc})")
                return analysis_norms
            except Exception as e:
                debug_messages.append(f"failed path={path} encoding={enc} error={repr(e)}")

    load_analysis_sources.last_error = "\n".join(debug_messages)
    print("norm_table.csv not found or could not be parsed")
    print(load_analysis_sources.last_error)
    analysis_norms = None
    return None 


def normalize_inn(value: Optional[str]) -> str:
    if value is None:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())



def safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def safe_div(a, b):
    a = safe_float(a)
    b = safe_float(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def robust_z(value, median, mad):
    value = safe_float(value)
    median = safe_float(median)
    mad = safe_float(mad)
    if value is None or median is None or mad is None:
        return None
    if mad == 0:
        return 0.0
    return (value - median) / mad


def okved_to_section(okved2: str) -> Optional[str]:
    try:
        n = int(str(okved2)[:2])
    except Exception:
        return None

    if 1 <= n <= 3:
        return "A"
    if 5 <= n <= 9:
        return "B"
    if 10 <= n <= 33:
        return "C"
    if 35 <= n <= 39:
        return "D"
    if 41 <= n <= 43:
        return "F"
    if 45 <= n <= 47:
        return "G"
    if 49 <= n <= 53:
        return "H"
    if 55 <= n <= 56:
        return "I"
    if 58 <= n <= 63:
        return "J"
    if 64 <= n <= 66:
        return "K"
    if n == 68:
        return "L"
    if 69 <= n <= 75:
        return "M"
    if 77 <= n <= 82:
        return "N"
    if n == 84:
        return "O"
    if n == 85:
        return "P"
    if 86 <= n <= 88:
        return "Q"
    if 90 <= n <= 93:
        return "R"
    if 94 <= n <= 96:
        return "S"
    if 97 <= n <= 98:
        return "T"
    if n == 99:
        return "U"
    return None

# Human readable OKVED section names
OKVED_SECTION_MAP = {
    "A": "Сельское, лесное хозяйство, охота, рыболовство",
    "B": "Добыча полезных ископаемых",
    "C": "Обрабатывающие производства",
    "D": "Электроэнергия, газ, пар",
    "E": "Водоснабжение и утилизация отходов",
    "F": "Строительство",
    "G": "Оптовая и розничная торговля",
    "H": "Транспортировка и хранение",
    "I": "Гостиницы и общественное питание",
    "J": "Информация и связь",
    "K": "Финансовая и страховая деятельность",
    "L": "Операции с недвижимостью",
    "M": "Профессиональная, научная и техническая деятельность",
    "N": "Административная деятельность",
    "O": "Госуправление и обеспечение военной безопасности",
    "P": "Образование",
    "Q": "Здравоохранение и социальные услуги",
    "R": "Культура, спорт, досуг",
    "S": "Прочие услуги",
    "T": "Домашние хозяйства",
    "U": "Экстерриториальные организации",
}


def find_norm_row(norms_df: DataFrame, period: int, okved2: str, section: str):
    rows = norms_df[
        (norms_df["level"] == "okved2")
        & (norms_df["period"] == period)
        & (norms_df["key"].astype(str) == str(okved2))
    ]
    if len(rows) > 0:
        return rows.iloc[0], "okved2"

    rows = norms_df[
        (norms_df["level"] == "section")
        & (norms_df["period"] == period)
        & (norms_df["key"].astype(str) == str(section))
    ]
    if len(rows) > 0:
        return rows.iloc[0], "section"

    rows = norms_df[
        (norms_df["level"] == "period")
        & (norms_df["period"] == period)
    ]
    if len(rows) > 0:
        return rows.iloc[0], "period"

    return None, None


def make_bfo_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": BFO_REFERER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    return session


def request_json(session: requests.Session, url: str, params=None):
    r = session.get(url, params=params, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def extract_search_items(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ["items", "content", "results", "data"]:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def search_company_by_inn(session: requests.Session, inn: str):
    payload = request_json(session, SEARCH_URL, params={"query": inn, "page": 0, "size": 20})
    items = extract_search_items(payload)
    if not items:
        raise HTTPException(status_code=404, detail="ИНН не найден или компания отсутствует в БФО")

    inn_norm = normalize_inn(inn)
    best = None
    for item in items:
        item_inn = normalize_inn(item.get("inn"))
        if item_inn == inn_norm:
            best = item
            break

    if best is None:
        best = items[0]

    company_id = best.get("id") or best.get("company_id")
    if not company_id:
        raise HTTPException(status_code=404, detail="Не удалось определить company_id по ИНН")

    return int(company_id), best


def fetch_company_metrics_by_inn(inn: str):
    session = make_bfo_session()
    try:
        session.get(BFO_REFERER, timeout=30)
    except Exception:
        pass

    company_id, org_info = search_company_by_inn(session, inn)

    periods_to_try = [2024, 2023, 2022, 2021]

    parsed = None
    history = []

    for period in periods_to_try:
        try:
            data = parse_company_year_json(session, company_id=company_id, period=period)

            if data is None:
                continue

            revenue = safe_float(data.get("revenue_t"))
            profit = safe_float(data.get("net_profit_t"))
            equity = safe_float(data.get("equity_t"))
            debt = safe_float(data.get("debt_t"))
            inventory = safe_float(data.get("inventory_t"))

            margin_year = safe_div(profit, revenue)
            debt_ratio_year = safe_div(debt, equity)
            inv_turnover_year = safe_div(revenue, inventory)

            if revenue is not None:
                history.append({
                    "year": period,
                    "revenue": revenue,
                    "profit": profit,
                    "margin": margin_year,
                    "debt_ratio": debt_ratio_year,
                    "inv_turnover": inv_turnover_year
                })

            if parsed is None:
                parsed = data

        except Exception:
            continue

    if parsed is None:
        raise HTTPException(status_code=404, detail="По данному ИНН не удалось получить финансовую отчетность")

    period = int(parsed.get("period") or periods_to_try[0])
    okved2 = str(parsed.get("okved2") or "")
    section = okved_to_section(okved2)

    margin = safe_div(parsed.get("net_profit_t"), parsed.get("revenue_t"))
    debt_ratio = safe_div(parsed.get("debt_t"), parsed.get("equity_t"))
    inv_turnover = safe_div(parsed.get("revenue_t"), parsed.get("inventory_t"))

    rev_t = safe_float(parsed.get("revenue_t"))
    rev_t_1 = safe_float(parsed.get("revenue_t_1"))

    growth_rev = None
    if rev_t is not None and rev_t_1 not in (None, 0):
        growth_rev = (rev_t - rev_t_1) / rev_t_1

    history_sorted = sorted(history, key=lambda x: x["year"])

    return {
        "company_id": company_id,
        "inn": normalize_inn(inn),
        "period": period,
        "okved2": okved2,
        "okved_section": section,
        "okved_section_name": OKVED_SECTION_MAP.get(section),
        "industry": OKVED_SECTION_MAP.get(section) or "Неизвестная отрасль",
        "region": org_info.get("region"),
        "margin": margin,
        "debt_ratio": debt_ratio,
        "inv_turnover": inv_turnover,
        "growth_rev": growth_rev,
        "history": history_sorted,
    }


def analyze_company_by_inn(inn: str):
    norms_df = load_analysis_sources()
    if norms_df is None:
        debug_info = getattr(load_analysis_sources, "last_error", "no debug info")
        raise HTTPException(status_code=500, detail=f"norm_table.csv not loaded\n{debug_info}")

    inn_norm = normalize_inn(inn)
    if not inn_norm:
        raise HTTPException(status_code=400, detail="Введите корректный ИНН")

    company_metrics = fetch_company_metrics_by_inn(inn_norm)

    period = int(company_metrics["period"])
    okved2 = str(company_metrics["okved2"])
    section = str(company_metrics["okved_section"] or "")
    norm_row, norm_level = find_norm_row(norms_df, period, okved2, section)
    if norm_row is None:
        raise HTTPException(status_code=500, detail="Не удалось найти отраслевые нормы")

    company_metrics["z_margin"] = robust_z(company_metrics["margin"], norm_row.get("median_margin"), norm_row.get("mad_margin"))
    company_metrics["z_debt_ratio"] = robust_z(company_metrics["debt_ratio"], norm_row.get("median_debt_ratio"), norm_row.get("mad_debt_ratio"))
    company_metrics["z_inv_turnover"] = robust_z(company_metrics["inv_turnover"], norm_row.get("median_inv_turnover"), norm_row.get("mad_inv_turnover"))
    company_metrics["z_growth"] = robust_z(company_metrics["growth_rev"], norm_row.get("median_growth"), norm_row.get("mad_growth"))

    comparison_rows = [
        {
            "name": "Маржа",
            "value": company_metrics["margin"],
            "median": safe_float(norm_row.get("median_margin")),
            "ratio": safe_div(company_metrics["margin"], safe_float(norm_row.get("median_margin"))),
            "z": company_metrics["z_margin"],
            "status": None,
        },
        {
            "name": "Долговая нагрузка",
            "value": company_metrics["debt_ratio"],
            "median": safe_float(norm_row.get("median_debt_ratio")),
            "ratio": safe_div(company_metrics["debt_ratio"], safe_float(norm_row.get("median_debt_ratio"))),
            "z": company_metrics["z_debt_ratio"],
            "status": None,
        },
        {
            "name": "Оборачиваемость запасов",
            "value": company_metrics["inv_turnover"],
            "median": safe_float(norm_row.get("median_inv_turnover")),
            "ratio": safe_div(company_metrics["inv_turnover"], safe_float(norm_row.get("median_inv_turnover"))),
            "z": company_metrics["z_inv_turnover"],
            "status": None,
        },
        {
            "name": "Рост выручки",
            "value": company_metrics["growth_rev"],
            "median": safe_float(norm_row.get("median_growth")),
            "ratio": safe_div(company_metrics["growth_rev"], safe_float(norm_row.get("median_growth"))),
            "z": company_metrics["z_growth"],
            "status": None,
        },
    ]

    strengths = []
    risks = []
    neutral = []

    for row in comparison_rows:
        z = row.get("z")

        if z is None:
            row["status"] = "mid"
            neutral.append(row)
            continue

        if z > 0.5:
            row["status"] = "good"
            strengths.append(row)
        elif z < -0.5:
            row["status"] = "bad"
            risks.append(row)
        else:
            row["status"] = "mid"
            neutral.append(row)

    # determine the main risk metric (largest negative deviation)
    key_risk = None
    if risks:
        try:
            key_risk = min(
                risks,
                key=lambda r: (r.get("z") if r.get("z") is not None else 0)
            )
        except Exception:
            key_risk = risks[0]

    history = company_metrics.get("history", [])

    company_history = {
        "labels": [str(x["year"]) for x in history],
        "revenue": [x.get("revenue") for x in history],
        "profit": [x.get("profit") for x in history],
        "margin": [x.get("margin") for x in history],
        "debt_ratio": [x.get("debt_ratio") for x in history],
        "inventory_turnover": [x.get("inv_turnover") for x in history],
    }

    return {
        "inn": inn_norm,
        "company": company_metrics,
        "okved_section_name": company_metrics.get("okved_section_name"),
        "industry": company_metrics.get("industry"),
        "rows": comparison_rows,
        "strengths": strengths,
        "risks": risks,
        "neutral": neutral,
        "key_risk": key_risk,
        "norm_level": norm_level,
        "company_history": company_history,
    }

def compute_request_hash(payload: dict) -> str:
    try:
        normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    except Exception:
        normalized = str(payload)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    # добавить недостающие столбцы (для SQLite) без миграций
    with engine.begin() as conn:
        try:
            conn.execute(text("ALTER TABLE predictions ADD COLUMN model_type VARCHAR(50) DEFAULT 'bankruptcy'"))
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE predictions ADD COLUMN request_hash VARCHAR(128)"))
        except Exception:
            pass


try:
    model = load('app/model.pkl')
except Exception:
    model = None

def process(args):
    args = list(map(float, args ))
    new_data = DataFrame({
        'Interest Expense Ratio': [args[0]],
        "Net Income to Stockholder's Equity": [args[1]],
        'Tax rate (A)': [args[2]],
        'Persistent EPS in the Last Four Seasons': [args[3]],
        'Working Capital to Total Assets': [args[4]],
        'Cash Flow Per Share': [args[5]],
        'Contingent liabilities/Net worth': [args[6]],
    })

    prediction = model.predict(new_data)
    probability = model.predict_proba(new_data)

    return int(prediction[0]), round(float(probability[0][1]), 3)

def set_auth_cookie(response: Response, token: str):
    secure = settings.environment not in {"local", "dev"}
    response.set_cookie(
        "access_token",
        value=f"Bearer {token}",
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )


def format_money(value: float) -> str:
    try:
        return f"{value:,.0f}".replace(",", " ")
    except Exception:
        return str(value)


@app.post("/models/breakeven", response_class=HTMLResponse)
async def calculate_breakeven(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    lang = pick_lang(request.query_params.get("lang"))
    form_data = dict(await request.form())
    copy = BREAKEVEN_COPY[lang]
    errors = []

    def parse_num(field: str, *, required: bool = False, non_negative: bool = False, positive: bool = False):
        raw = form_data.get(field)
        label = next((f["label"][lang] for f in BREAKEVEN_FIELDS if f["key"] == field), field)
        if raw is None or str(raw).strip() == "":
            if required:
                errors.append(f"{label}: {'Must be provided' if lang == 'en' else 'Заполните поле'}")
            return None
        try:
            num = float(str(raw).replace(",", "."))
        except Exception:
            errors.append(f"{label}: {'Must be a number' if lang == 'en' else 'Должно быть числом'}")
            return None
        if non_negative and num < 0:
            errors.append(f"{label}: {'Must be >= 0' if lang == 'en' else 'Должно быть ≥ 0'}")
        if positive and num <= 0:
            errors.append(f"{label}: {'Must be > 0' if lang == 'en' else 'Должно быть > 0'}")
        return num

    marketing = parse_num("marketing_costs", required=True, non_negative=True)
    rent = parse_num("rent_costs", required=True, non_negative=True)
    salary_fixed = parse_num("salary_fixed", required=True, non_negative=True)
    salary_piece = parse_num("salary_piece_per_unit", required=True, non_negative=True)
    cogs = parse_num("cogs_per_unit", required=True, non_negative=True)
    direct = parse_num("direct_costs_per_unit", required=True, non_negative=True)
    price = parse_num("price_per_unit", required=True, positive=True)
    avg_check = parse_num("avg_check", required=False, positive=True)

    # Если средний чек указан корректно, используем его вместо цены
    price_effective = avg_check if avg_check and avg_check > 0 else price

    if errors:
        tooltips = [field.get("tooltips", {}).get(lang, "") for field in INN_FORM_FIELDS]
        return templates.TemplateResponse(
            "form.html",
            {
                "request": request,
                "fields": INN_FORM_FIELDS,
                "be_fields": BREAKEVEN_FIELDS,
                "lang": lang,
                "content": FORM_COPY[lang],
                "be_copy": copy,
                "tooltips": tooltips,
                "user": current_user,
                "nav": NAV_COPY[lang],
                "next_url": f"{request.url.path}?lang={lang}",
                "initial_mode": "breakeven",
                "errors": errors,
            },
            status_code=400,
        )

    fc = sum(v or 0 for v in [marketing, rent, salary_fixed])
    vc = sum(v or 0 for v in [salary_piece, cogs, direct])
    cm = (price_effective or 0) - vc

    be_units = None
    be_units_ceil = None
    be_rev = None
    be_rev_ceil = None
    if cm > 0:
        be_units = fc / cm
        be_units_ceil = math.ceil(be_units)
        be_rev = be_units * price_effective
        be_rev_ceil = be_units_ceil * price_effective

    # Подготовка данных для графика
    if cm > 0 and be_units:
        x_max = max(be_units_ceil * 2, 10)
    else:
        x_max = 100
    step = max(1, int(x_max / 40))
    labels = list(range(0, int(x_max) + step, step))
    profits = []
    for x in labels:
        profit = (price_effective or 0) * x - (fc + vc * x)
        profits.append(round(profit, 2))

    # Вертикальная линия для BE
    be_line = []
    if be_units_ceil:
        min_y = min(profits)
        max_y = max(profits)
        be_line = [
            {"x": be_units_ceil, "y": min_y},
            {"x": be_units_ceil, "y": max_y},
        ]

    # Таблица сценариев
    scenarios = []
    scenario_points = []
    if be_units_ceil:
        for ratio in [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5]:
            scenario_points.append(int(be_units_ceil * ratio))
        scenario_points.extend([be_units_ceil + 1, be_units_ceil + 2, be_units_ceil + 3])
    else:
        scenario_points = [0, 10, 25, 50, 75, 100]
    # уникальные возрастающие
    scenario_points = sorted(set(scenario_points))
    for units in scenario_points:
        revenue = units * (price_effective or 0)
        costs = fc + vc * units
        profit = revenue - costs
        scenarios.append(
            {
                "units": units,
                "revenue": revenue,
                "costs": costs,
                "profit": profit,
                "is_be": be_units_ceil is not None and units == be_units_ceil,
            }
        )

    if current_user:
        try:
            payload = {
                "marketing_costs": marketing,
                "rent_costs": rent,
                "salary_fixed": salary_fixed,
                "salary_piece_per_unit": salary_piece,
                "cogs_per_unit": cogs,
                "direct_costs_per_unit": direct,
                "price_per_unit": price,
                "avg_check": avg_check,
                "price_effective": price_effective,
            }
            req_hash = compute_request_hash(payload)
            now_ts = datetime.now(timezone.utc)
            recent = db.scalars(
                select(Prediction)
                .where(
                    Prediction.user_id == current_user.id,
                    Prediction.model_type == "breakeven",
                    Prediction.request_hash == req_hash,
                )
                .order_by(Prediction.created_at.desc())
            ).first()
            if recent:
                created = recent.created_at
                if created and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created and (now_ts - created) <= timedelta(seconds=3):
                    payload = None

            if payload is not None:
                record = Prediction(
                    user_id=current_user.id,
                    model_type="breakeven",
                    input_payload=payload,
                    request_hash=req_hash,
                    result_payload={
                        "fc": fc,
                        "vc": vc,
                        "price": price_effective,
                        "cm": cm,
                        "be_units": be_units,
                        "be_units_ceil": be_units_ceil,
                        "be_rev": be_rev,
                        "be_rev_ceil": be_rev_ceil,
                        "summary": BREAKEVEN_COPY[lang]["summary_be"].format(
                            units=be_units_ceil if be_units_ceil is not None else "—",
                            revenue=format_money(be_rev_ceil) if be_rev_ceil is not None else "—",
                            cm=format_money(cm) if cm is not None else "—",
                        ),
                    },
                )
                db.add(record)
                db.commit()
        except Exception as e:
            print(f"Failed to save breakeven run: {e}")

    return templates.TemplateResponse(
        "breakeven_result.html",
        {
            "request": request,
            "lang": lang,
            "nav": NAV_COPY[lang],
            "user": current_user,
            "copy": copy,
            "fmt": format_money,
            "fc": fc,
            "vc": vc,
            "price": price_effective,
            "cm": cm,
            "be_units": be_units,
            "be_units_ceil": be_units_ceil,
            "be_rev": be_rev,
            "be_rev_ceil": be_rev_ceil,
            "labels": labels,
            "profits": profits,
            "be_line": be_line,
            "scenarios": scenarios,
        },
    )



def pick_lang(value: Optional[str]) -> str:
    lang = (value or "ru").lower()
    return lang if lang in {"ru", "en"} else "ru"

def validate_form_data(form_data: dict) -> list:
    """Валидация данных формы. Возвращает список ошибок."""
    errors = []

    for key, value in form_data.items():
        # Пропускаем параметр lang и inn
        if key in {'lang', 'inn'}:
            continue

        # Проверяем, что значение не пустое
        if not value or value.strip() == '':
            continue  # Пустые поля разрешены
            
        try:
            # Пытаемся преобразовать в число
            normalized = str(value).replace(',', '.')
            num_value = float(normalized)
            if not math.isfinite(num_value):
                raise ValueError("not finite")
        except (ValueError, TypeError):
            errors.append(f"Поле '{key}': '{value}' не является числом")
            
    return errors


@app.get("/simple", response_class=HTMLResponse)
async def simple_test(request: Request):
    return HTMLResponse(content="<h1>Simple test works!</h1>")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, current_user: Optional[User] = Depends(get_optional_user)):
    """Render the main landing page with RU/EN toggle via ?lang=en."""
    lang = pick_lang(request.query_params.get("lang"))
    
    translations = {
        "ru": {
            "page_title": "Ваш финансовый директор прямо в браузере",
            "eyebrow": "Fintech analytics",
            "hero_title": "Ваш финансовый директор прямо в браузере",
            "hero_subtitle": "Прогнозирование рисков и быстрые финансовые модели для принятия решений.",
            "cta": "Перейти к прогнозу",
            "features_bankruptcy_title": "Прогнозирование банкротства",
            "features_bankruptcy_desc": "Оцените вероятность банкротства компании на основе финансовых показателей и получите интерпретируемый результат.",
            "features_models_title": "Построение финансовой модели",
            "features_models_desc": "Рассчитывайте ключевые финансовые модели, включая точку безубыточности, и анализируйте прибыльность бизнеса.",
            "features": [
                {"title": "Мгновенная оценка", "text": "Модель анализирует финансовые показатели и вычисляет вероятность банкротства."},
                {"title": "Понятный отчёт", "text": "Объяснение факторов риска и ключевых метрик."},
                {"title": "Поддержка решений", "text": "Подходит для аналитиков, инвесторов и компаний."},
            ],
            "technology": "Система использует современные методы машинного обучения для точного прогнозирования вероятности банкротства и выявления рисковых факторов.",
            "footer": "© 2026 Финансовая аналитическая система",
            "lang_label": "RU",
            "hero_cta_save_history": "Сохранить в историю",
            "hero_cta_dashboard": "Перейти в кабинет",
            "for_whom_title": "Для кого этот сервис",
            "for_whom_subtitle": "Кому пригодится FinTechAnalytics",
            "for_whom_cards": [
                {"title": "Предпринимателям", "text": "Быстрая оценка финансовых рисков перед принятием решений."},
                {"title": "Студентам и исследователям", "text": "Учебный и аналитический инструмент для работы с финансовыми моделями."},
                {"title": "Финансовым аналитикам", "text": "Наглядные расчёты и сценарный анализ без сложных таблиц."},
            ],
            "why_title": "Почему это удобно",
            "why_items": [
                "Не требует установки",
                "Работает прямо в браузере",
                "Понятные и интерпретируемые результаты",
                "Интерактивный What-if анализ"
            ],
            "preview_title": "Как выглядит результат",
            "preview_subtitle": "Пример аналитического вывода",
            "preview_risk_label": "Вероятность банкротства",
            "preview_status": "Не банкрот",
            "preview_disclaimer": "Пример данных",
            "preview_lines": [
                "Риск оценивается по введённым показателям.",
                "What-if анализ помогает понять влияние критериев.",
                "Данные для демонстрации, не являются советом.",
            ],
            "for_whom_badges": ["Бизнес", "Обучение", "Аналитика"],
            "hero_badge_risk": "риск",
            "hero_badge_model": "модель",
            "hero_badge_whatif": "what-if",
        },
        "en": {
            "page_title": "Your financial director right in the browser",
            "eyebrow": "Fintech analytics",
            "hero_title": "Your financial director right in the browser",
            "hero_subtitle": "Risk forecasting and fast financial models to support decision-making.",
            "cta": "Go to prediction",
            "features_bankruptcy_title": "Bankruptcy risk forecasting",
            "features_bankruptcy_desc": "Estimate a company’s bankruptcy risk based on financial indicators and get an interpretable result.",
            "features_models_title": "Financial model building",
            "features_models_desc": "Calculate key financial models, including break-even analysis, and assess business profitability.",
            "features": [
                {"title": "Instant assessment", "text": "The model analyzes financial indicators and estimates bankruptcy probability."},
                {"title": "Clear report", "text": "Explains risk factors and key metrics."},
                {"title": "Decision support", "text": "Suitable for analysts, investors, and companies."},
            ],
            "technology": "The system applies modern machine learning methods to accurately predict bankruptcy probability and reveal risk drivers.",
            "footer": "© 2026 Financial Analytics System",
            "lang_label": "EN",
            "hero_cta_save_history": "Save to history",
            "hero_cta_dashboard": "Go to dashboard",
            "for_whom_title": "Who this service is for",
            "for_whom_subtitle": "See if FinTechAnalytics fits your workflow",
            "for_whom_cards": [
                {"title": "For entrepreneurs", "text": "Quick financial risk assessment before making decisions."},
                {"title": "For students and researchers", "text": "An educational and analytical tool for financial models."},
                {"title": "For financial analysts", "text": "Clear calculations and scenario analysis without complex spreadsheets."},
            ],
            "why_title": "Why it’s convenient",
            "why_items": [
                "No installation required",
                "Works directly in the browser",
                "Clear and interpretable results",
                "Interactive what-if analysis"
            ],
            "preview_title": "What the result looks like",
            "preview_subtitle": "Example of analytical output",
            "preview_risk_label": "Bankruptcy probability",
            "preview_status": "Not bankrupt",
            "preview_disclaimer": "Example data",
            "preview_lines": [
                "Risk is estimated from the provided metrics.",
                "What-if analysis shows sensitivity to criteria.",
                "Sample figures for demonstration only, not advice.",
            ],
            "for_whom_badges": ["Business", "Education", "Analytics"],
            "hero_badge_risk": "risk",
            "hero_badge_model": "model",
            "hero_badge_whatif": "what if",
        },
    }

    content = translations[lang]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "content": content,
            "lang": lang,
            "user": current_user,
            "nav": NAV_COPY[lang],
        },
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, current_user: Optional[User] = Depends(get_optional_user)):
    if current_user:
        return RedirectResponse(url="/account", status_code=303)
    lang = pick_lang(request.query_params.get("lang"))
    return templates.TemplateResponse("register.html", {"request": request, "lang": lang, "user": None, "error": None, "nav": NAV_COPY[lang]})


@app.post("/register")
async def register_action(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
):
    lang = pick_lang(request.query_params.get("lang"))
    try:
        payload = UserCreate(email=email, password=password)
    except Exception as e:
        error_text = "Некорректные данные формы" if lang == "ru" else "Invalid form data"
        return templates.TemplateResponse("register.html", {"request": request, "lang": lang, "user": None, "error": error_text, "nav": NAV_COPY[lang]})

    existing = db.scalar(select(User).where(User.email == payload.email.lower()))
    if existing:
        error_text = "Email уже зарегистрирован" if lang == "ru" else "Email already exists"
        return templates.TemplateResponse("register.html", {"request": request, "lang": lang, "user": None, "error": error_text, "nav": NAV_COPY[lang]})

    new_user = User(email=payload.email.lower(), hashed_password=hash_password(payload.password))
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    token = create_access_token(str(new_user.id), expires_delta=timedelta(minutes=settings.access_token_expire_minutes))
    next_target = request.query_params.get("next")
    redirect_url = next_target or f"/account?lang={lang}"
    response = RedirectResponse(url=redirect_url, status_code=303)
    set_auth_cookie(response, token)
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, current_user: Optional[User] = Depends(get_optional_user)):
    if current_user:
        return RedirectResponse(url="/account", status_code=303)
    lang = pick_lang(request.query_params.get("lang"))
    return templates.TemplateResponse("login.html", {"request": request, "lang": lang, "user": None, "error": None, "nav": NAV_COPY[lang]})


@app.post("/login")
async def login_action(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
):
    lang = pick_lang(request.query_params.get("lang"))
    try:
        payload = UserLogin(email=email, password=password)
    except Exception:
        return templates.TemplateResponse("login.html", {"request": request, "lang": lang, "user": None, "error": "Некорректные данные", "nav": NAV_COPY[lang]})

    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.hashed_password):
        return templates.TemplateResponse("login.html", {"request": request, "lang": lang, "user": None, "error": "Неверные учетные данные", "nav": NAV_COPY[lang]})

    token = create_access_token(str(user.id), expires_delta=timedelta(minutes=settings.access_token_expire_minutes))
    next_target = request.query_params.get("next")
    redirect_url = next_target or f"/account?lang={lang}"
    response = RedirectResponse(url=redirect_url, status_code=303)
    set_auth_cookie(response, token)
    return response


@app.post("/logout")
async def logout(request: Request):
    lang = pick_lang(request.query_params.get("lang"))
    response = RedirectResponse(url=f"/?lang={lang}", status_code=303)
    response.delete_cookie("access_token")
    return response


@app.get("/account", response_class=HTMLResponse)
async def account_dashboard(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = pick_lang(request.query_params.get("lang"))
    account_copy = ACCOUNT_COPY[lang]
    msk_tz = timezone(timedelta(hours=3))
    # читаем язык так же, как на остальных страницах: query-param имеет приоритет, иначе cookie handled upstream
    records = db.scalars(
        select(Prediction).where(Prediction.user_id == current_user.id).order_by(Prediction.created_at.desc())
    ).all()

    label_map = {
        "ru": {
            "interest_expense_ratio": "Доля расходов",
            "net_profit_to_equity": "Рентабельность капитала",
            "tax_rate_a": "Эффективная ставка налога",
            "stable_profit_per_share": "Прибыль на акцию (за год)",
            "working_capital_to_total_assets": "Оборотный капитал к общим активам",
            "cash_flow_per_share": "Денежный поток на акцию",
            "contingent_liabilities_to_net_worth": "Условные обязательства / Капитал",
        },
        "en": {
            "interest_expense_ratio": "Interest / Revenue",
            "net_profit_to_equity": "ROE",
            "tax_rate_a": "Tax Rate",
            "stable_profit_per_share": "EPS (Annual)",
            "working_capital_to_total_assets": "Working Capital Ratio",
            "cash_flow_per_share": "Cash Flow / Share",
            "contingent_liabilities_to_net_worth": "Liabilities / Equity",
        },
    }[lang]
    be_label_map = {
        "ru": {
            "marketing_costs": "Маркетинг и реклама",
            "rent_costs": "Аренда",
            "salary_fixed": "Фиксированная ЗП",
            "salary_piece_per_unit": "Сдельная ЗП / единицу",
            "cogs_per_unit": "Себестоимость / единицу",
            "direct_costs_per_unit": "Прямые расходы / единицу",
            "price_per_unit": "Цена за единицу",
            "avg_check": "Средний чек",
            "price_effective": "Используемая цена",
        },
        "en": {
            "marketing_costs": "Marketing & Ads",
            "rent_costs": "Rent",
            "salary_fixed": "Fixed payroll",
            "salary_piece_per_unit": "Piece-rate payroll / unit",
            "cogs_per_unit": "COGS / unit",
            "direct_costs_per_unit": "Direct costs / unit",
            "price_per_unit": "Price per unit",
            "avg_check": "Avg. check",
            "price_effective": "Effective price",
        },
    }[lang]

    view_records = []
    for rec in records:
        if isinstance(rec.input_payload, str):
            try:
                rec.input_payload = json.loads(rec.input_payload)
            except Exception:
                rec.input_payload = {}
        if isinstance(rec.result_payload, str):
            try:
                rec.result_payload = json.loads(rec.result_payload)
            except Exception:
                rec.result_payload = {}

        created = rec.created_at
        formatted_date = ""
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except Exception:
                created = None
        if isinstance(created, datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            created_local = created.astimezone(msk_tz)
            if lang == "ru":
                formatted_date = created_local.strftime("%d.%m.%Y, %H:%M")
            else:
                formatted_date = created_local.strftime("%b %d, %Y, %H:%M")

        items = []
        summary_text = None
        model_type = rec.model_type or "bankruptcy"

        if model_type == "breakeven":
            # inputs
            if rec.input_payload:
                for key, label in be_label_map.items():
                    val = rec.input_payload.get(key)
                    if val not in (None, "", []):
                        items.append({"label": label, "value": val})
            rp = rec.result_payload or {}
            cm_val = rp.get("cm")
            be_units_val = rp.get("be_units_ceil") or rp.get("be_units")
            be_rev_val = rp.get("be_rev_ceil") or rp.get("be_rev")
            summary_text = BREAKEVEN_COPY[lang]["summary_be"].format(
                units=be_units_val if be_units_val is not None else "—",
                revenue=format_money(be_rev_val) if be_rev_val is not None else "—",
                cm=format_money(cm_val) if cm_val is not None else "—",
            )
            items.append({"label": be_label_map.get("price_effective", "Price"), "value": format_money(rp.get("price")) if rp.get("price") is not None else rp.get("price")})
            if cm_val is not None:
                items.append({"label": BREAKEVEN_COPY[lang]["cm"], "value": format_money(cm_val)})
            if be_units_val is not None:
                items.append({"label": BREAKEVEN_COPY[lang]["be_units"], "value": be_units_val})
            if be_rev_val is not None:
                items.append({"label": BREAKEVEN_COPY[lang]["be_revenue"], "value": format_money(be_rev_val)})

            status_text = ACCOUNT_COPY[lang].get("type_breakeven", "Финмодель: Безубыточность")
        else:
            if rec.input_payload:
                for key, label in label_map.items():
                    val = rec.input_payload.get(key)
                    if val not in (None, "", []):
                        items.append({"label": label, "value": val})
            raw_label = (rec.result_payload or {}).get("risk_label") if rec.result_payload else None
            label_norm = (raw_label or "").strip().lower()
            status_text = raw_label or ACCOUNT_COPY[lang].get("type_bankruptcy", "Прогноз банкротства")
            if label_norm in {"банкрот", "bankrupt"}:
                status_text = ACCOUNT_COPY[lang].get("risk_bankrupt", "Банкрот")
            elif label_norm in {"не банкрот", "небанкрот", "not bankrupt", "not_bankrupt", "notbankrupt"}:
                status_text = ACCOUNT_COPY[lang].get("risk_not_bankrupt", "Не банкрот")
            prob = (rec.result_payload or {}).get("probability")
            if prob is not None:
                summary_text = f"{prob}%"

        view_records.append(
            {
                "record": rec,
                "formatted_date": formatted_date,
                "status_text": status_text,
                "summary_text": summary_text,
                "all_items": items,
            }
        )

    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": current_user,
            "records": view_records,
            "lang": lang,
            "nav": NAV_COPY[lang],
            "message": request.query_params.get("msg"),
            "content": account_copy,
        },
    )


@app.post("/account/history/clear", name="account_history_clear")
async def clear_history(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = pick_lang(request.query_params.get("lang"))
    db.query(Prediction).filter(Prediction.user_id == current_user.id).delete()
    db.commit()
    return RedirectResponse(url=f"/account?lang={lang}&msg={NAV_COPY[lang]['deleted_msg']}", status_code=303)


@app.post("/account/history/{prediction_id}/delete", name="account_history_delete")
async def delete_history_record(
    prediction_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = pick_lang(request.query_params.get("lang"))
    prediction = db.get(Prediction, prediction_id)
    if not prediction or prediction.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.delete(prediction)
    db.commit()
    return RedirectResponse(url=f"/account?lang={lang}&msg={NAV_COPY[lang]['deleted_msg']}", status_code=303)


INN_FORM_FIELDS = [
    {
        "key": "inn",
        "labels": {
            "ru": "ИНН компании",
            "en": "Company INN",
        },
        "tooltips": {
            "ru": "Введите ИНН компании для получения финансового анализа",
            "en": "Enter the company INN to get financial analysis",
        },
    },
]

FORM_FIELDS = [
    {
        "key": "interest_expense_ratio",
        "labels": {
            "ru": "Доля расходов",
            "en": "Interest / Revenue",
        },
        "tooltips": {
            "ru": "Расходы компании ÷ выручка за период",
            "en": "Total expenses ÷ total revenue",
        },
    },
    {
        "key": "net_profit_to_equity",
        "labels": {
            "ru": "Рентабельность капитала",
            "en": "ROE",
        },
        "tooltips": {
            "ru": "Чистая прибыль ÷ собственный капитал",
            "en": "Net income ÷ shareholders' equity",
        },
    },
    {
        "key": "tax_rate_a",
        "labels": {
            "ru": "Эффективная ставка налога",
            "en": "Tax Rate",
        },
        "tooltips": {
            "ru": "Расходы на налоги ÷ прибыль до налогообложения",
            "en": "Income tax expense ÷ pre-tax income",
        },
    },
    {
        "key": "stable_profit_per_share",
        "labels": {
            "ru": "Прибыль на акцию (за год)",
            "en": "EPS (Annual)",
        },
        "tooltips": {
            "ru": "Чистая прибыль ÷ количество акций в обращении",
            "en": "Net income ÷ number of shares outstanding",
        },
    },
    {
        "key": "working_capital_to_total_assets",
        "labels": {
            "ru": "Оборотный капитал к общим активам",
            "en": "Working Capital Ratio",
        },
        "tooltips": {
            "ru": "(Оборотные активы − краткосрочные обязательства) ÷ общие активы",
            "en": "(Current assets − current liabilities) ÷ total assets",
        },
    },
    {
        "key": "cash_flow_per_share",
        "labels": {
            "ru": "Денежный поток на акцию",
            "en": "Cash Flow / Share",
        },
        "tooltips": {
            "ru": "Операционный денежный поток ÷ количество акций",
            "en": "Operating cash flow ÷ shares outstanding",
        },
    },
    {
        "key": "contingent_liabilities_to_net_worth",
        "labels": {
            "ru": "Условные обязательства / Капитал",
            "en": "Liabilities / Equity",
        },
        "tooltips": {
            "ru": "Условные обязательства ÷ собственный капитал",
            "en": "Contingent liabilities ÷ shareholders' equity",
        },
    },
]

FORM_COPY = {
    "ru": {
        "page_title": "Введите финансовые показатели | Система прогнозирования",
        "eyebrow": "Fintech analytics",
        "hero_title": "Введите ИНН компании",
        "hero_subtitle": "Сервис найдет компанию по ИНН и покажет финансовый анализ показателей относительно отрасли.",
        "criterion_col": "Поле",
        "value_col": "Значение",
        "placeholder": "Введите ИНН",
        "submit": "Показать анализ",
        "back_home": "← На главную",
        "footer": "© 2025 Финансовая аналитическая система",
    },
    "en": {
        "page_title": "Enter financial metrics | Bankruptcy prediction",
        "eyebrow": "Fintech analytics",
        "hero_title": "Enter company INN",
        "hero_subtitle": "The service will find the company by INN and show a financial analysis relative to industry norms.",
        "criterion_col": "Field",
        "value_col": "Value",
        "placeholder": "Enter INN",
        "submit": "Show analysis",
        "back_home": "← Back to home",
        "footer": "© 2025 Financial Analytics System",
    },
}

RESULT_COPY = {
    "ru": {
        "page_title": "Результат прогнозирования | Система прогнозирования",
        "eyebrow": "Fintech analytics",
        "hero_title": "Результат прогнозирования",
        "hero_subtitle": "Оценка вероятности банкротства по введённым показателям.",
        "result_label": "Вероятность банкротства",
        "interpretation_title": "Пояснение",
        "interpretation_text": (
            "Риск рассчитан по введённым показателям. Чем выше риск, "
            "тем внимательнее стоит проверить долговую нагрузку и ликвидность."
        ),
        "table_title": "Введённые данные",
        "criterion_col": "Критерий",
        "value_col": "Коэффициент",
        "table_helper": "Убедитесь, что все показатели переданы корректно.",
        "back_form": "Вернуться к вводу данных",
        "back_home": "На главную",
        "footer": "© 2025 Финансовая аналитическая система",
        "bankruptcy_status": ("Не Банкрот", "Банкрот"),
        "whatif_title": "What-if анализ: как изменится риск при других значениях",
        "whatif_desc": "Меняйте значения критериев вручную или ползунками и смотрите, как это влияет на риск банкротства.",
        "whatif_reset_one": "Вернуть",
        "whatif_reset_one_tip": "Вернуть исходное значение",
        "whatif_reset_all": "Сбросить все изменения",
        "whatif_recalc": "Пересчёт…",
        "whatif_ready": "Готово",
        "whatif_error": "Введите число от 0 до 10",
    },
    "en": {
        "page_title": "Prediction result | Bankruptcy prediction",
        "eyebrow": "Fintech analytics",
        "hero_title": "Prediction result",
        "hero_subtitle": "Bankruptcy probability based on the provided metrics.",
        "result_label": "Bankruptcy probability",
        "interpretation_title": "Explanation",
        "interpretation_text": (
            "Risk is calculated from the provided metrics. Higher risk suggests "
            "a closer review of leverage and liquidity."
        ),
        "table_title": "Submitted data",
        "criterion_col": "Criterion",
        "value_col": "Coefficient",
        "table_helper": "Please verify all metrics are captured correctly.",
        "back_form": "Back to form",
        "back_home": "Back to home",
        "footer": "© 2025 Financial Analytics System",
        "bankruptcy_status": ("Not Bankrupt", "Bankrupt"),
        "whatif_title": "What-if analysis: see how risk changes with different values",
        "whatif_desc": "Adjust criteria values manually or with sliders to see how the bankruptcy risk changes.",
        "whatif_reset_one": "Reset",
        "whatif_reset_one_tip": "Reset to original value",
        "whatif_reset_all": "Reset all changes",
        "whatif_recalc": "Recalculating…",
        "whatif_ready": "Ready",
        "whatif_error": "Enter a number between 0 and 10",
    },
}

ACCOUNT_COPY = {
    "ru": {
        "page_title": "Личный кабинет | История прогнозов",
        "eyebrow": "Личный кабинет",
        "title": "История прогнозов",
        "helper": "Отображаются только ваши записи.",
        "new_prediction": "Новый прогноз",
        "date_col": "Дата",
        "result_col": "Результат",
        "details_col": "Детали",
        "empty": "Записей пока нет. Выполните прогноз, чтобы сохранить результат.",
        "risk_bankrupt": "Банкрот",
        "risk_not_bankrupt": "Не банкрот",
        "type_bankruptcy": "Прогноз банкротства",
        "type_breakeven": "Финмодель: Безубыточность",
    },
    "en": {
        "page_title": "Account | Prediction history",
        "eyebrow": "Account",
        "title": "Prediction history",
        "helper": "Only your records are visible.",
        "new_prediction": "New prediction",
        "date_col": "Date",
        "result_col": "Result",
        "details_col": "Details",
        "empty": "No records yet. Run a prediction to save it here.",
        "risk_bankrupt": "Bankrupt",
        "risk_not_bankrupt": "Not bankrupt",
        "type_bankruptcy": "Bankruptcy forecast",
        "type_breakeven": "Fin. model: Break-even",
    },
}

# Тексты и поля для финансовой модели "точка безубыточности"
BREAKEVEN_FIELDS = [
    {
        "key": "marketing_costs",
        "label": {"ru": "Маркетинг и реклама", "en": "Marketing & Ads"},
        "tooltip": {
            "ru": "Постоянные расходы на продвижение за период",
            "en": "Fixed marketing spend for the period",
        },
        "unit": {"ru": "₽ за период", "en": "₽ per period"},
    },
    {
        "key": "rent_costs",
        "label": {"ru": "Аренда", "en": "Rent"},
        "tooltip": {
            "ru": "Постоянные арендные платежи за период",
            "en": "Fixed rent costs for the period",
        },
        "unit": {"ru": "₽ за период", "en": "₽ per period"},
    },
    {
        "key": "salary_fixed",
        "label": {"ru": "Фиксированная ЗП", "en": "Fixed payroll"},
        "tooltip": {
            "ru": "Постоянная часть фонда оплаты труда за период",
            "en": "Fixed payroll for the period",
        },
        "unit": {"ru": "₽ за период", "en": "₽ per period"},
    },
    {
        "key": "salary_piece_per_unit",
        "label": {"ru": "Сдельная ЗП / единицу", "en": "Piece-rate payroll / unit"},
        "tooltip": {
            "ru": "Переменная часть ЗП на одну единицу товара/заказа",
            "en": "Variable payroll per unit/order",
        },
        "unit": {"ru": "₽ на 1 ед.", "en": "₽ per unit"},
    },
    {
        "key": "cogs_per_unit",
        "label": {"ru": "Себестоимость / единицу", "en": "COGS / unit"},
        "tooltip": {
            "ru": "Себестоимость товара или услуги на одну единицу",
            "en": "Cost of goods sold per unit",
        },
        "unit": {"ru": "₽ на 1 ед.", "en": "₽ per unit"},
    },
    {
        "key": "direct_costs_per_unit",
        "label": {"ru": "Прямые расходы / единицу", "en": "Direct costs / unit"},
        "tooltip": {
            "ru": "Прочие прямые расходы на единицу (логистика и т.п.)",
            "en": "Other direct per-unit costs (logistics etc.)",
        },
        "unit": {"ru": "₽ на 1 ед.", "en": "₽ per unit"},
    },
    {
        "key": "price_per_unit",
        "label": {"ru": "Цена за единицу", "en": "Price per unit"},
        "tooltip": {
            "ru": "Средняя цена реализации одной единицы",
            "en": "Average selling price per unit",
        },
        "unit": {"ru": "₽ на 1 ед.", "en": "₽ per unit"},
    },
    {
        "key": "avg_check",
        "label": {"ru": "Средний чек (опционально)", "en": "Avg. check (optional)"},
        "tooltip": {
            "ru": "Если указано, используется вместо цены за единицу.",
            "en": "If set, used instead of price per unit.",
        },
        "unit": {"ru": "₽ за заказ", "en": "₽ per order"},
    },
]

BREAKEVEN_COPY = {
    "ru": {
        "hero_title": "Финансовая модель: точка безубыточности",
        "hero_subtitle": "Оцените, при каких объёмах продаж бизнес станет прибыльным.",
        "segment_bankruptcy": "Прогноз банкротства",
        "segment_breakeven": "Фин. модель: Безубыточность",
        "form_title": "Критерии",
        "note_price": "Если заполнен средний чек, он используется вместо цены за единицу.",
        "submit": "Подсчитать",
        "result_title": "Результат финансовой модели",
        "be_units": "Точка безубыточности, шт",
        "be_revenue": "Выручка в точке Б/У",
        "cm": "Маржа на единицу",
        "unreachable": "При текущих параметрах безубыточность недостижима: маржа ≤ 0. Увеличьте цену или снизьте переменные расходы.",
        "chart_title": "Прибыль vs Продажи",
        "table_title": "Сценарии продаж",
        "col_units": "Продажи, шт",
        "col_revenue": "Выручка",
        "col_costs": "Расходы",
        "col_profit": "Прибыль",
        "profit_before_be": "Прибыль до н/о",
        "badge_be": "Б/У",
        "criteria_col": "Критерии",
        "value_col": "Значение",
        "summary_label": "Финмодель: Безубыточность",
        "summary_be": "Точка Б/У: {units} шт; Выручка: {revenue}; Маржа: {cm}",
    },
    "en": {
        "hero_title": "Financial model: Break-even point",
        "hero_subtitle": "See at which sales volumes your business becomes profitable.",
        "segment_bankruptcy": "Bankruptcy forecast",
        "segment_breakeven": "Fin. model: Break-even",
        "form_title": "Criteria",
        "note_price": "If Avg. check is filled, it overrides price per unit.",
        "submit": "Calculate",
        "result_title": "Financial model result",
        "be_units": "Break-even point, units",
        "be_revenue": "Revenue at B/E point",
        "cm": "Contribution margin per unit",
        "unreachable": "With current inputs break-even is not reachable: margin ≤ 0. Increase price or reduce variable costs.",
        "chart_title": "Profit vs Sales",
        "table_title": "Sales scenarios",
        "col_units": "Units",
        "col_revenue": "Revenue",
        "col_costs": "Costs",
        "col_profit": "Profit",
        "profit_before_be": "EBITDA",
        "badge_be": "B/E",
        "criteria_col": "Criteria",
        "value_col": "Value",
        "summary_label": "Fin. model: Break-even",
        "summary_be": "B/E: {units} units; Revenue: {revenue}; CM: {cm}",
    },
}

NAV_COPY = {
    "ru": {
        "brand": "FinTechAnalytics",
        "home": "Главная",
        "predict": "Прогноз",
        "account": "Кабинет",
        "login": "Войти",
        "register": "Регистрация",
        "logout": "Выйти",
        "save_history": "Сохранить в истории",
        "clear_history": "Очистить историю",
        "delete": "Удалить",
        "show_all": "Показать все",
        "deleted_msg": "Удалено",
        "delete_all_confirm": "Удалить всю историю?",
        "delete_one_confirm": "Удалить запись?",
    },
    "en": {
        "brand": "FinTechAnalytics",
        "home": "Home",
        "predict": "Predict",
        "account": "Account",
        "login": "Login",
        "register": "Register",
        "logout": "Logout",
        "save_history": "Save to history",
        "clear_history": "Clear history",
        "delete": "Delete",
        "show_all": "Show all",
        "deleted_msg": "Deleted",
        "delete_all_confirm": "Clear all history?",
        "delete_one_confirm": "Delete this record?",
    },
}


@app.get("/form", response_class=HTMLResponse)
async def show_form(request: Request, current_user: Optional[User] = Depends(get_optional_user)):
    """Render the financial indicators form."""
    lang = pick_lang(request.query_params.get("lang"))
    initial_mode = request.query_params.get("mode", "bankruptcy")
    copy = FORM_COPY[lang]
    
    # Подготавливаем tooltips для текущего языка
    tooltips = [field.get("tooltips", {}).get(lang, "") for field in INN_FORM_FIELDS]
    
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "fields": INN_FORM_FIELDS,
            "be_fields": BREAKEVEN_FIELDS,
            "lang": lang,
            "content": copy,
            "be_copy": BREAKEVEN_COPY[lang],
            "tooltips": tooltips,
            "user": current_user,
            "nav": NAV_COPY[lang],
            "next_url": f"{request.url.path}?lang={lang}",
            "initial_mode": initial_mode,
        },
    )

@app.get("/models/breakeven", response_class=HTMLResponse)
async def breakeven_form(request: Request, current_user: Optional[User] = Depends(get_optional_user)):
    """Shortcut to open the form in break-even mode."""
    lang = pick_lang(request.query_params.get("lang"))
    tooltips = [field.get("tooltips", {}).get(lang, "") for field in INN_FORM_FIELDS]
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "fields": INN_FORM_FIELDS,
            "be_fields": BREAKEVEN_FIELDS,
            "lang": lang,
            "content": FORM_COPY[lang],
            "be_copy": BREAKEVEN_COPY[lang],
            "tooltips": tooltips,
            "user": current_user,
            "nav": NAV_COPY[lang],
            "next_url": f"{request.url.path}?lang={lang}",
            "initial_mode": "breakeven",
        },
    )

"""def compute_risk_probability(values: Dict[str, str]) -> int:
    Simple placeholder risk model based on numeric input.
    numeric_values = []
    for val in values.values():
        try:
            numeric_values.append(float(val))
        except (TypeError, ValueError):
            continue

    if not numeric_values:
        return 30

    # Lightweight heuristic: combine magnitude and variability, clamp to 1..95
    avg = sum(numeric_values) / len(numeric_values)
    spread = max(numeric_values) - min(numeric_values)
    score = (abs(avg) * 4 + spread * 1.5) % 96
    return max(5, min(95, round(score)))"""


def risk_bucket(prediction: int, lang: str) -> Tuple[str, str]:
    """Return (label, color) for bankruptcy status."""
    labels = RESULT_COPY[lang]["bankruptcy_status"]
    if prediction == 1:
        return labels[1], "#D9534F"  # Банкрот (красный)
    return labels[0], "#36CFC9"      # Не Банкрот (родной акцентный цвет)


@app.post("/result", response_class=HTMLResponse)
async def show_result(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    try:
        try:
            form_data = dict(await request.form())
        except AssertionError as e:
            if "python-multipart" in str(e):
                raw_data = await request.body()
                form_data = {}
                if raw_data:
                    try:
                        raw_str = raw_data.decode("utf-8")
                        for pair in raw_str.split("&"):
                            if "=" in pair:
                                key, value = pair.split("=", 1)
                                form_data[key] = value
                    except Exception:
                        form_data = {}
            else:
                raise e
        except Exception:
            form_data = {}

        lang = pick_lang(request.query_params.get("lang", form_data.get("lang")))
        inn_value = normalize_inn(form_data.get("inn"))

        if not inn_value:
            return templates.TemplateResponse(
                "form.html",
                {
                    "request": request,
                    "fields": INN_FORM_FIELDS,
                    "be_fields": BREAKEVEN_FIELDS,
                    "lang": lang,
                    "content": FORM_COPY[lang],
                    "be_copy": BREAKEVEN_COPY[lang],
                    "tooltips": [field.get("tooltips", {}).get(lang, "") for field in INN_FORM_FIELDS],
                    "user": current_user,
                    "nav": NAV_COPY[lang],
                    "next_url": f"{request.url.path}?lang={lang}",
                    "errors": ["Введите корректный ИНН" if lang == "ru" else "Enter a valid INN"],
                    "initial_mode": "bankruptcy",
                },
                status_code=400,
            )

        analysis = analyze_company_by_inn(inn_value)

        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "probability": "83%",
                "risk_label": "Анализ компании" if lang == "ru" else "Company analysis",
                "risk_color": "#36CFC9",
                "interpretation": "Сравнение показателей компании с отраслевыми медианами" if lang == "ru" else "Comparison of company metrics with industry medians",
                "rows": analysis["rows"],
                "strengths": analysis.get("strengths"),
                "risks": analysis.get("risks"),
                "neutral": analysis.get("neutral"),
                "lang": lang,
                "content": RESULT_COPY[lang],
                "form_data": {"inn": inn_value},
                "nav": NAV_COPY[lang],
                "user": current_user,
                "next_url": str(request.url),
                "whatif_fields": [],
                "whatif_baseline": {},
                "whatif_baseline_result": {},
                "analysis_mode": "company",
                "analysis_company": analysis["company"],
                "analysis_inn": analysis["inn"],
                "analysis_norm_level": analysis["norm_level"],
                "company_history": analysis.get("company_history"),
                "key_risk": analysis.get("key_risk"),
            },
        )

    except HTTPException as exc:
        return HTMLResponse(
            content=f"""
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h1 style="color: #ff4757;">Ошибка</h1>
                <p>{exc.detail}</p>
                <a href="/form?lang=ru" style="color: #2E5AAC;">← Вернуться к форме</a>
            </body>
            </html>
            """,
            status_code=exc.status_code,
        )
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"Error in show_result: {error_msg}")
        return HTMLResponse(
            content=f"""
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h1 style="color: #ff4757;">Ошибка обработки</h1>
                <p><b>Сообщение:</b> {str(e)}</p>
                <h3>Traceback:</h3>
                <pre style='background:#f5f5f5;padding:10px;border-radius:6px;white-space:pre-wrap;'>{error_msg}</pre>
                <a href="/form?lang=ru" style="color: #2E5AAC;">← Вернуться к форме</a>
            </body>
            </html>
            """,
            status_code=500,
        )


@app.get("/result", response_class=HTMLResponse)
async def result_get(request: Request, current_user: Optional[User] = Depends(get_optional_user)):
    lang = pick_lang(request.query_params.get("lang"))
    inn_value = normalize_inn(request.query_params.get("inn"))

    if not inn_value:
        return RedirectResponse(url=f"/form?lang={lang}", status_code=303)

    try:
        analysis = analyze_company_by_inn(inn_value)

        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "probability": "83%",
                "risk_label": "Анализ компании" if lang == "ru" else "Company analysis",
                "risk_color": "#36CFC9",
                "interpretation": "Сравнение показателей компании с отраслевыми медианами" if lang == "ru" else "Comparison of company metrics with industry medians",
                "rows": analysis["rows"],
                "strengths": analysis.get("strengths"),
                "risks": analysis.get("risks"),
                "neutral": analysis.get("neutral"),
                "lang": lang,
                "content": RESULT_COPY[lang],
                "form_data": {"inn": inn_value},
                "nav": NAV_COPY[lang],
                "user": current_user,
                "next_url": str(request.url),
                "whatif_fields": [],
                "whatif_baseline": {},
                "whatif_baseline_result": {},
                "analysis_mode": "company",
                "analysis_company": analysis["company"],
                "analysis_inn": analysis["inn"],
                "analysis_norm_level": analysis["norm_level"],
                "company_history": analysis.get("company_history"),
                "key_risk": analysis.get("key_risk"),
            },
        )

    except HTTPException as exc:
        return HTMLResponse(
            content=f"""
            <html>
            <body style='font-family: Arial, sans-serif; padding: 20px;'>
                <h1 style='color:#ff4757;'>Ошибка</h1>
                <p>{exc.detail}</p>
                <a href='/form?lang={lang}' style='color:#2E5AAC;'>← Вернуться к форме</a>
            </body>
            </html>
            """,
            status_code=exc.status_code
        )
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"Error in result_get: {error_msg}")
        return HTMLResponse(
            content=f"<html><body><h1>Ошибка обработки данных</h1><pre>{error_msg}</pre><a href='/form?lang={lang}'>Вернуться к форме</a></body></html>",
            status_code=500
        )


@app.post("/api/bankruptcy/predict")
async def api_predict(request: Request):
    lang = pick_lang(request.query_params.get("lang"))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    errors = []
    values = []
    for field in FORM_FIELDS:
        key = field["key"]
        if key not in payload:
            errors.append(f"{key}: required")
            continue
        raw = payload.get(key)
        try:
            num = float(str(raw).replace(",", "."))
        except Exception:
            errors.append(f"{key}: must be a number")
            continue
        if num < 0 or num > 10:
            errors.append(f"{key}: must be between 0 and 10")
            continue
        values.append(num)

    if errors:
        return JSONResponse({"detail": errors}, status_code=422)

    try:
        prediction, probability = process(values)
        probability = round(probability * 100, 2)
        label, color = risk_bucket(prediction, lang)
        return {"probability": probability, "risk_label": label, "risk_color": color}
    except Exception as e:
        return JSONResponse({"detail": "Failed to predict"}, status_code=500)
