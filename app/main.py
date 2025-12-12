from typing import Dict, Optional, Tuple

from pandas import DataFrame
from joblib import load
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Система прогнозирования банкротства")

# Serve static assets from the /app/static directory
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Configure Jinja2 templates directory
templates = Jinja2Templates(directory="app/templates")



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
async def read_root(request: Request):
    """Render the main landing page with RU/EN toggle via ?lang=en."""
    lang = pick_lang(request.query_params.get("lang"))
    
    translations = {
        "ru": {
            "page_title": "Система прогнозирования банкротства",
            "eyebrow": "Fintech analytics",
            "hero_title": "Система прогнозирования банкротства",
            "hero_subtitle": "Аналитика финансовых рисков с использованием моделей машинного обучения.",
            "cta": "Перейти к прогнозу",
            "features": [
                {
                    "title": "Мгновенная оценка",
                    "text": "Модель анализирует финансовые показатели и вычисляет вероятность банкротства.",
                },
                {
                    "title": "Понятный отчёт",
                    "text": "Объяснение факторов риска и ключевых метрик.",
                },
                {
                    "title": "Поддержка решений",
                    "text": "Подходит для аналитиков, инвесторов и компаний.",
                },
            ],
            "technology": "Система использует современные методы машинного обучения для точного прогнозирования вероятности банкротства и выявления рисковых факторов.",
            "footer": "© 2025 Финансовая аналитическая система",
            "lang_label": "RU",
        },
        "en": {
            "page_title": "Bankruptcy Prediction System",
            "eyebrow": "Fintech analytics",
            "hero_title": "Bankruptcy Prediction System",
            "hero_subtitle": "Financial risk analytics powered by machine learning models.",
            "cta": "Go to prediction",
            "features": [
                {
                    "title": "Instant assessment",
                    "text": "The model analyzes financial indicators and estimates bankruptcy probability.",
                },
                {
                    "title": "Clear report",
                    "text": "Explains risk factors and key metrics.",
                },
                {
                    "title": "Decision support",
                    "text": "Suitable for analysts, investors, and companies.",
                },
            ],
            "technology": "The system applies modern machine learning methods to accurately predict bankruptcy probability and reveal risk drivers.",
            "footer": "© 2025 Financial Analytics System",
            "lang_label": "EN",
        },
    }

    content = translations[lang]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "content": content,
            "lang": lang,
        },
    )


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
    },
}


@app.get("/form", response_class=HTMLResponse)
async def show_form(request: Request):
    """Render the financial indicators form."""
    lang = pick_lang(request.query_params.get("lang"))
    copy = FORM_COPY[lang]
    
    # Подготавливаем tooltips для текущего языка
    tooltips = [field.get("tooltips", {}).get(lang, "") for field in FORM_FIELDS]
    
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "fields": FORM_FIELDS,
            "lang": lang,
            "content": copy,
            "tooltips": tooltips,
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
async def show_result(request: Request):    
    try:
        lang = "ru"
        form_data = dict(await request.form())
        lang = pick_lang(request.query_params.get("lang", form_data.get("lang")))
        
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
            },
        )
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"Error in show_result: {error_msg}")
        return HTMLResponse(
            content=f"<html><body><h1>Ошибка обработки данных</h1><pre>{error_msg}</pre><a href='/form?lang=ru'>Вернуться к форме</a></body></html>",
            status_code=500
        )


@app.get("/result", response_class=HTMLResponse)
async def result_get(request: Request):
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

