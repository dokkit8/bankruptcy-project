import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

df = pd.read_csv(DATA_DIR / "dataset_v1.csv")

print("Всего строк:", len(df))
print()

print("Распределение по годам:")
print(df["period"].value_counts())
print()

print("Топ групп (period + okved2):")
print(
    df.groupby(["period", "okved2"])
      .size()
      .sort_values(ascending=False)
      .head(10)
)

print(df.columns)

# ===== Проверка clean_data логики вручную =====

df_clean = df.copy()

# то же самое, что в norm_builder
df_clean = df_clean.dropna(subset=["margin", "debt_ratio", "inv_turnover", "growth_rev"])

# если у тебя есть фильтр по экстремальным значениям, добавь его сюда
df_clean = df_clean[
    (df_clean["margin"].between(-5, 5)) &
    (df_clean["debt_ratio"].between(-50, 50)) &
    (df_clean["inv_turnover"].between(0, 50)) &
    (df_clean["growth_rev"].between(-10, 10))
]

print()
print("Строк после очистки:", len(df_clean))

print()
print("Топ групп после очистки:")
print(
    df_clean.groupby(["period", "okved2"])
        .size()
        .sort_values(ascending=False)
        .head(10)
)
print("\nNaN по колонкам dataset:")
print(df.isna().sum())

print("\nПроцент NaN по колонкам dataset:")
print((df.isna().mean() * 100).round(2))

norm_df = pd.read_csv(DATA_DIR / "norm_table.csv")

print("\nNaN по колонкам norm_table:")
print(norm_df.isna().sum())

print("\nПроцент NaN по колонкам norm_table:")
print((norm_df.isna().mean() * 100).round(2))