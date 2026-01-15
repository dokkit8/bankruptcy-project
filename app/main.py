from typing import Dict, Optional, Tuple
from datetime import timedelta, datetime, timezone
import json
import math
import hashlib

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

app = FastAPI(title="Система прогнозирования банкротства")

# Serve static assets from the /app/static directory
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Configure Jinja2 templates directory
templates = Jinja2Templates(directory="app/templates")

settings = get_settings()


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    # добавить модельный тип, если столбца нет (для SQLite)
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
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_predictions_request_hash ON predictions(request_hash)"))
        except Exception:
            pass


model = load('app/model.pkl')

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
        tooltips = [field.get("tooltips", {}).get(lang, "") for field in FORM_FIELDS]
        return templates.TemplateResponse(
            "form.html",
            {
                "request": request,
                "fields": FORM_FIELDS,
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
            record = Prediction(
                user_id=current_user.id,
                model_type="breakeven",
                input_payload={
                    "marketing_costs": marketing,
                    "rent_costs": rent,
                    "salary_fixed": salary_fixed,
                    "salary_piece_per_unit": salary_piece,
                    "cogs_per_unit": cogs,
                    "direct_costs_per_unit": direct,
                    "price_per_unit": price,
                    "avg_check": avg_check,
                    "price_effective": price_effective,
                },
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
                request_hash=None,
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
        # Пропускаем параметр lang
        if key == 'lang':
            continue

        # Проверяем, что значение не пустое
        if not value or value.strip() == '':
            continue  # Пустые поля разрешены
            
        try:
            # Пытаемся преобразовать в число
            normalized = str(value).replace(',', '.')
            num_value = float(normalized)
            num_value = float(value)

            # Проверяем разумные границы для финансовых показателей
            if num_value < -10000 or num_value > 10000000:
                errors.append(f"Поле '{key}': значение {num_value} вне допустимого диапазона")
                
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
        "hero_title": "Введите финансовые показатели компании",
        "hero_subtitle": "Эти данные используются для расчёта вероятности банкротства.",
        "criterion_col": "Критерий",
        "value_col": "Коэффициент",
        "placeholder": "Введите значение",
        "submit": "Рассчитать прогноз",
        "back_home": "← На главную",
        "footer": "© 2025 Финансовая аналитическая система",
    },
    "en": {
        "page_title": "Enter financial metrics | Bankruptcy prediction",
        "eyebrow": "Fintech analytics",
        "hero_title": "Enter the company's financial metrics",
        "hero_subtitle": "These inputs are used to estimate bankruptcy probability.",
        "criterion_col": "Criterion",
        "value_col": "Coefficient",
        "placeholder": "Enter a value",
        "submit": "Calculate forecast",
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
    tooltips = [field.get("tooltips", {}).get(lang, "") for field in FORM_FIELDS]
    
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "fields": FORM_FIELDS,
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
    tooltips = [field.get("tooltips", {}).get(lang, "") for field in FORM_FIELDS]
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "fields": FORM_FIELDS,
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
        lang = "ru"
        
        # Надежное получение данных формы с fallback
        try:
            # Сначала пробуем получить form-data
            form_data = dict(await request.form())
        except AssertionError as e:
            if "python-multipart" in str(e):
                # Если ошибка связана с python-multipart, пробуем альтернативный способ
                print("python-multipart not available, trying alternative method...")
                # Получаем raw данные и парсим вручную
                raw_data = await request.body()
                if raw_data:
                    # Простой парсинг key=value&key2=value2
                    form_data = {}
                    try:
                        raw_str = raw_data.decode('utf-8')
                        for pair in raw_str.split('&'):
                            if '=' in pair:
                                key, value = pair.split('=', 1)
                                form_data[key] = value
                    except:
                        form_data = {"error": "Failed to parse form data"}
                else:
                    form_data = {}
            else:
                raise e
        except Exception as e:
            print(f"Error parsing form data: {e}")
            form_data = {"error": "Failed to parse form data"}
        
        # Определяем язык
        lang = pick_lang(request.query_params.get("lang", form_data.get("lang")))
        
        # Валидация данных формы
        if "error" not in form_data:
            validation_errors = validate_form_data(form_data)
            if validation_errors:
                copy = FORM_COPY[lang]
                tooltips = [field.get("tooltips", {}).get(lang, "") for field in FORM_FIELDS]
                return templates.TemplateResponse(
                    "form.html",
                    {
                        "request": request,
                        "fields": FORM_FIELDS,
                        "be_fields": BREAKEVEN_FIELDS,
                        "lang": lang,
                        "content": copy,
                        "be_copy": BREAKEVEN_COPY[lang],
                        "tooltips": tooltips,
                        "user": current_user,
                        "nav": NAV_COPY[lang],
                        "next_url": f"{request.url.path}?lang={lang}",
                        "errors": validation_errors,
                        "initial_mode": "bankruptcy",
                    },
                    status_code=400,
                )
        
        # Исключаем параметр lang из числовых данных
        numeric_data = {}
        errors = []
        for k, v in form_data.items():
            if k in {'lang', 'error'}:
                continue
            if v is None or v == '':
                continue
            try:
                numeric_data[k] = float(v.replace(',', '.'))
            except Exception:
                field_label = next((f["labels"][lang] for f in FORM_FIELDS if f["key"] == k), k)
                errors.append(f"Поле '{field_label}' должно быть числом")
        
        if errors:
            copy = FORM_COPY[lang]
            tooltips = [field.get("tooltips", {}).get(lang, "") for field in FORM_FIELDS]
            return templates.TemplateResponse(
                "form.html",
                {
                    "request": request,
                    "fields": FORM_FIELDS,
                    "be_fields": BREAKEVEN_FIELDS,
                    "lang": lang,
                    "content": copy,
                    "be_copy": BREAKEVEN_COPY[lang],
                    "tooltips": tooltips,
                    "user": current_user,
                    "nav": NAV_COPY[lang],
                    "next_url": f"{request.url.path}?lang={lang}",
                    "errors": errors,
                    "initial_mode": "bankruptcy",
                },
                status_code=400,
            )
        
        # Проверяем, есть ли данные для обработки
        if not numeric_data:
            return HTMLResponse(
                content=f"""
                <html>
                <body style="font-family: Arial, sans-serif; padding: 20px;">
                    <h1 style="color: #ff4757;">Ошибка</h1>
                    <p>Не получены данные формы</p>
                    <a href="/form?lang={lang}" style="color: #2E5AAC;">← Вернуться к форме</a>
                </body>
                </html>
                """,
                status_code=400
            )
        
        try:
            prediction, probability = process(list(numeric_data.values()))
            probability *= 100
            probability = round(probability, 2)
            label, color = risk_bucket(prediction, lang)

            if current_user:
                record = Prediction(
                    user_id=current_user.id,
                    model_type="bankruptcy",
                    input_payload=numeric_data,
                    result_payload={"probability": probability, "risk_label": label, "risk_color": color},
                    request_hash=None,
                )
                db.add(record)
                db.commit()
        except Exception as e:
            print(f"Error in prediction: {e}")
            return HTMLResponse(
                content=f"""
                <html>
                <body style="font-family: Arial, sans-serif; padding: 20px;">
                    <h1 style="color: #ff4757;">Ошибка обработки</h1>
                    <p>Не удалось обработать данные: {str(e)}</p>
                    <a href="/form?lang={lang}" style="color: #2E5AAC;">← Вернуться к форме</a>
                </body>
                </html>
                """,
                status_code=500
            )
        
        copy = RESULT_COPY[lang]

        rows = []
        form_lookup = {k: v for k, v in form_data.items()}
        for field in FORM_FIELDS:
            key = field["key"]
            label_text = field["labels"][lang]
            rows.append({"name": label_text, "value": form_lookup.get(key, "")})

        def to_float_safe(val):
            try:
                return float(str(val).replace(",", "."))
            except Exception:
                return 0.0

        whatif_fields = [
            {"key": f["key"], "label": f["labels"][lang], "value": to_float_safe(form_lookup.get(f["key"], 0))}
            for f in FORM_FIELDS
        ]
        whatif_baseline = {f["key"]: to_float_safe(form_lookup.get(f["key"], 0)) for f in FORM_FIELDS}
        whatif_baseline_result = {"probability": probability, "risk_label": label, "risk_color": color}

        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "probability": probability,
                "risk_label": label,
                "risk_color": color,
                "interpretation": copy["interpretation_text"],
                "rows": rows,
                "lang": lang,
                "content": copy,
                "form_data": form_data,
                "nav": NAV_COPY[lang],
                "user": current_user,
                "next_url": str(request.url),
                "whatif_fields": whatif_fields,
                "whatif_baseline": whatif_baseline,
                "whatif_baseline_result": whatif_baseline_result,
            },
        )
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"Error in show_result: {error_msg}")
        return HTMLResponse(
            content=f"""
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h1 style="color: #ff4757;">Внутренняя ошибка сервера</h1>
                <p>Произошла ошибка при обработке запроса.</p>
                <p>Попробуйте позже или обратитесь к администратору.</p>
                <a href="/form?lang=ru" style="color: #2E5AAC;">← Вернуться к форме</a>
            </body>
            </html>
            """,
            status_code=500
        )


@app.get("/result", response_class=HTMLResponse)
async def result_get(request: Request, current_user: Optional[User] = Depends(get_optional_user)):
    """Handle GET requests to /result - either with form parameters or redirect to form."""
    lang = pick_lang(request.query_params.get("lang"))
    
    # Получаем все параметры формы из query string
    form_data = {}
    for key, value in request.query_params.items():
        if key != "lang":
            form_data[key] = value
    
    # Валидация данных формы
    validation_errors = validate_form_data(form_data)
    if validation_errors:
        error_message = "Ошибки в данных: " + "; ".join(validation_errors)
        return HTMLResponse(
            content=f"""
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h1 style="color: #ff4757;">Ошибка валидации данных</h1>
                <p>{error_message}</p>
                <a href="/form?lang={lang}" style="color: #2E5AAC;">← Вернуться к форме</a>
            </body>
            </html>
            """,
            status_code=400
        )

    # Если нет параметров формы, перенаправляем на форму
    if not form_data:
        return RedirectResponse(url=f"/form?lang={lang}", status_code=303)

    try:
        # Исключаем параметр lang из числовых данных
        numeric_data = {k: v for k, v in form_data.items() if k != 'lang'}
        prediction, probability = process(list(numeric_data.values()))
        probability *= 100
        probability = round(probability, 2)
        label, color = risk_bucket(prediction, lang)
        copy = RESULT_COPY[lang]

        rows = []
        form_lookup = {k: v for k, v in form_data.items()}
        for field in FORM_FIELDS:
            key = field["key"]
            label_text = field["labels"][lang]
            rows.append({"name": label_text, "value": form_lookup.get(key, "")})

        whatif_fields = [
            {"key": f["key"], "label": f["labels"][lang], "value": float(form_lookup.get(f["key"], 0) or 0)}
            for f in FORM_FIELDS
        ]
        whatif_baseline = {f["key"]: float(form_lookup.get(f["key"], 0) or 0) for f in FORM_FIELDS}
        whatif_baseline_result = {"probability": probability, "risk_label": label, "risk_color": color}

        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "probability": probability,
                "risk_label": label,
                "risk_color": color,
                "interpretation": copy["interpretation_text"],
                "rows": rows,
                "lang": lang,
                "content": copy,
                "form_data": form_data,
                "nav": NAV_COPY[lang],
                "user": current_user,
                "next_url": str(request.url),
                "whatif_fields": whatif_fields,
                "whatif_baseline": whatif_baseline,
                "whatif_baseline_result": whatif_baseline_result,
            },
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
