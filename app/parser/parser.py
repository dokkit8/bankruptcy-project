import re
import warnings
from typing import Optional, Dict, Any, List
import numpy as np
import pandas as pd

# ------------- 0) Утилиты: чистка чисел и текста -------------

def normalize_text(x: Any) -> str:
    """Приводим текст к виду, удобному для поиска."""
    if pd.isna(x):
        return ""
    s = str(x).strip().lower()
    s = s.replace("\u00a0", " ")  # неразрывные пробелы
    s = re.sub(r"\s+", " ", s)
    return s

def to_number(x: Any) -> float:
    """Преобразуем '213 553' / '-' / '—' / '(123)' в число или NaN."""
    if pd.isna(x):
        return np.nan
    s = str(x).strip()
    if s in {"-", "—", ""}:
        return np.nan
    s = s.replace("\u00a0", "").replace(" ", "")
    # скобки как отрицательное
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    # иногда запятая
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return np.nan

# ------------- 1) Поиск строки заголовков и чтение листа -------------

def find_header_row_idx(path: str, sheet_name: str, anchor: str = "Наименование показателя", max_scan_rows: int = 50) -> int:
    """
    Читает лист без заголовков и ищет строку, где есть 'Наименование показателя'.
    Возвращает индекс строки заголовков.
    """
    df_raw = pd.read_excel(path, sheet_name=sheet_name, header=None, nrows=max_scan_rows)
    mask = df_raw.apply(
        lambda row: row.astype(str).str.contains(anchor, na=False).any(),
        axis=1
    )
    if not mask.any():
        raise ValueError(f"Не нашёл строку заголовков по якорю '{anchor}' на листе '{sheet_name}'.")
    return int(df_raw.index[mask][0])

def read_table_with_header(path: str, sheet_name: str) -> pd.DataFrame:
    """Перечитывает лист уже с правильной строкой заголовков."""
    header_idx = find_header_row_idx(path, sheet_name)
    df = pd.read_excel(path, sheet_name=sheet_name, header=header_idx)
    return df

# ------------- 2) Работа с колонками лет: "якорь + окно" -------------

def find_year_anchor_col(columns: List[Any], year: int) -> Optional[int]:
    """Находит индекс колонки, в названии которой встречается год."""
    y = str(year)
    for i, c in enumerate(columns):
        if y in str(c):
            return i
    return None

def get_year_value_from_row(row: pd.Series, year: int, window: int = 2) -> float:
    """
    Достаём значение за конкретный год, учитывая, что оно может сидеть в Unnamed рядом с колонкой года.
    Правило поиска:
      1) якорная колонка года
      2) справа от якоря
      3) слева от якоря
    """
    cols = list(row.index)
    anchor_idx = find_year_anchor_col(cols, year)
    if anchor_idx is None:
        return np.nan

    # строим порядок обхода: anchor, anchor+1..anchor+window, anchor-1..anchor-window
    idxs = [anchor_idx]
    idxs += [anchor_idx + k for k in range(1, window + 1) if anchor_idx + k < len(cols)]
    idxs += [anchor_idx - k for k in range(1, window + 1) if anchor_idx - k >= 0]

    for j in idxs:
        v = to_number(row.iloc[j])
        if not np.isnan(v):
            return v

    return np.nan

# ------------- 3) Поиск строки показателя по ключевым словам -------------

def find_indicator_row(df: pd.DataFrame, name_col: str, patterns: List[str]) -> Optional[pd.Series]:
    """
    Ищем строку показателя по списку паттернов (подстроки).
    Возвращаем первую найденную строку.
    """
    if name_col not in df.columns:
        raise ValueError(f"В таблице нет колонки '{name_col}'. Колонки: {list(df.columns)}")

    s = df[name_col].map(normalize_text)

    for p in patterns:
        mask = s.str.contains(p, na=False)
        if mask.any():
            return df.loc[mask].iloc[0]
    return None


def find_row_by_code(df: pd.DataFrame, code_col: str, code: int) -> Optional[pd.Series]:
    if code_col not in df.columns:
        return None
    s = df[code_col].astype(str).str.replace(".0", "", regex=False)
    mask = s == str(code)
    if mask.any():
        return df.loc[mask].iloc[0]
    return None


# ------------- 4) Парсинг метаданных: ИНН, ОКВЭД, scale -------------

def _find_value_to_right(df: pd.DataFrame, r: int, c: int, max_shift: int = 12):
    """Ищем первое осмысленное значение справа от (r,c)."""
    for k in range(1, max_shift + 1):
        if c + k >= df.shape[1]:
            break
        v = df.iat[r, c + k]
        if pd.isna(v):
            continue
        s = str(v).strip()
        if s == "" or s.lower() in {"инн", "оквэд", "единица измерения"}:
            continue
        return s
    return None


def parse_meta(path: str):
    sheet = "Сведения об организации"
    df = pd.read_excel(path, sheet_name=sheet, header=None)

    inn = None
    okved = None
    scale = 1

    # проходим по ячейкам, но анализируем как текст
    for r in range(df.shape[0]):
        for c in range(df.shape[1]):
            cell = df.iat[r, c]
            if pd.isna(cell):
                continue
            t = str(cell).strip().lower().replace("\u00a0", " ")
            t = re.sub(r"\s+", " ", t)

            # ИНН
            if inn is None and t == "инн":
                val = _find_value_to_right(df, r, c)
                if val:
                    inn = val

            # ОКВЭД 2 (ключ может быть разный)
            if okved is None and ("оквэд" in t):
                val = _find_value_to_right(df, r, c)
                if val:
                    okved = val

            # Единицы измерения
            if "единица измерения" in t or "ед. измерения" in t:
                val = _find_value_to_right(df, r, c)
                if val:
                    v = val.lower()
                    if "тыс" in v:
                        scale = 1000
                    elif "млн" in v:
                        scale = 1_000_000
                    else:
                        scale = 1

    okved_2 = None
    if okved:
        m = re.search(r"(\d{2})", okved)
        if m:
            okved_2 = m.group(1)

    return {"inn": inn, "okved": okved, "okved_2": okved_2, "scale": scale}

# ------------- 5) Главная функция: парсим компанию за год -------------

def parse_company_year(xlsx_path: str, company_id: int, period: int) -> Optional[Dict[str, Any]]:
    """
    Возвращает одну строку датасета или None, если ключевых данных нет.
    """

    # глушим шумные warning-и openpyxl, но не ошибки
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        meta = parse_meta(xlsx_path)
        scale = meta["scale"]

        # --- Финрез ---
        fin_sheet = "Отчет о финансовых результатах"
        df_fin = read_table_with_header(xlsx_path, fin_sheet)
        code_col_fin = "Код строки"
        name_col = "Наименование показателя"

        # выручка
        row_rev = find_row_by_code(df_fin, code_col_fin, 2110)
        if row_rev is None:
            row_rev = find_indicator_row(df_fin, name_col, patterns=["выручка"])

        rev_t = get_year_value_from_row(row_rev, period) * scale if row_rev is not None else np.nan
        rev_t1 = get_year_value_from_row(row_rev, period - 1) * scale if row_rev is not None else np.nan

        # чистая прибыль
        row_np = find_row_by_code(df_fin, code_col_fin, 2400)
        if row_np is None:
            row_np = find_indicator_row(df_fin, name_col, patterns=["чистая прибыль"])

        np_t = get_year_value_from_row(row_np, period) * scale if row_np is not None else np.nan
        np_t1 = get_year_value_from_row(row_np, period - 1) * scale if row_np is not None else np.nan

        # --- Баланс ---
        bal_sheet = "Бухгалтерский баланс"
        df_bal = read_table_with_header(xlsx_path, bal_sheet)
        code_col = "Код строки"

        # запасы
        row_inv = find_row_by_code(df_bal, code_col, 1210)
        if row_inv is None:
            row_inv = find_indicator_row(df_bal, name_col, patterns=["запасы"])

        inv_t = get_year_value_from_row(row_inv, period) * scale if row_inv is not None else np.nan
        inv_t1 = get_year_value_from_row(row_inv, period - 1) * scale if row_inv is not None else np.nan

        # кредиторка
        row_pay = find_row_by_code(df_bal, code_col, 1520)
        if row_pay is None:
            row_pay = find_indicator_row(df_bal, name_col, patterns=["кредитор"])

        pay_t = get_year_value_from_row(row_pay, period) * scale if row_pay is not None else np.nan
        pay_t1 = get_year_value_from_row(row_pay, period - 1) * scale if row_pay is not None else np.nan

        # капитал
        row_eq = find_row_by_code(df_bal, code_col, 1300)
        if row_eq is None:
            row_eq = find_indicator_row(df_bal, name_col, patterns=["капитал", "резерв"])

        eq_t = get_year_value_from_row(row_eq, period) * scale if row_eq is not None else np.nan
        eq_t1 = get_year_value_from_row(row_eq, period - 1) * scale if row_eq is not None else np.nan

        # долг: заемные средства (1410 + 1510) — устойчиво для разных форм

        row_debt_long = find_row_by_code(df_bal, code_col, 1410)  # заемные средства (долгосрочные)
        row_debt_short = find_row_by_code(df_bal, code_col, 1510)  # заемные средства (краткосрочные)

        debt_long_t = get_year_value_from_row(row_debt_long, period) * scale if row_debt_long is not None else np.nan
        debt_short_t = get_year_value_from_row(row_debt_short, period) * scale if row_debt_short is not None else np.nan

        debt_t = np.nan
        if not np.isnan(debt_long_t) or not np.isnan(debt_short_t):
            debt_t = (0 if np.isnan(debt_long_t) else debt_long_t) + (0 if np.isnan(debt_short_t) else debt_short_t)

        debt_long_t1 = get_year_value_from_row(row_debt_long,
                                               period - 1) * scale if row_debt_long is not None else np.nan
        debt_short_t1 = get_year_value_from_row(row_debt_short,
                                                period - 1) * scale if row_debt_short is not None else np.nan

        debt_t1 = np.nan
        if not np.isnan(debt_long_t1) or not np.isnan(debt_short_t1):
            debt_t1 = (0 if np.isnan(debt_long_t1) else debt_long_t1) + (
                0 if np.isnan(debt_short_t1) else debt_short_t1)

    # --- Политика качества: ключевые поля ---
    # --- Политика качества: ключевые поля ---
    missing = []
    if np.isnan(rev_t): missing.append("revenue")
    if np.isnan(np_t): missing.append("net_profit")
    if np.isnan(eq_t): missing.append("equity")

    is_complete = 0 if missing else 1
    skip_reason = ("MISSING_" + "_".join(missing).upper()) if missing else ""

    return {
        "company_id": company_id,
        "inn": meta["inn"],
        "okved": meta["okved"],
        "okved_2": meta["okved_2"],
        "period": period,
        "scale": meta["scale"],

        "revenue_t": rev_t,
        "revenue_t_1": rev_t1,

        "net_profit_t": np_t,
        "net_profit_t_1": np_t1,

        "inventory_t": inv_t,
        "inventory_t_1": inv_t1,

        "payables_t": pay_t,
        "payables_t_1": pay_t1,

        "equity_t": eq_t,
        "equity_t_1": eq_t1,

        "debt_t": debt_t,
        "debt_t_1": debt_t1,

        # новые поля для диагностики
        "is_complete": is_complete,
        "skip_reason": skip_reason,
    }


