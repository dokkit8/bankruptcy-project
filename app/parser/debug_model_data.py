import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

df = pd.read_csv(DATA_DIR / "dataset_with_features.csv")

print("Всего строк в dataset_with_features:", len(df))

df_model = df.dropna(subset=[
    "z_margin",
    "z_debt_ratio",
    "z_inv_turnover",
    "z_growth"
])

print("Строк пригодных для модели (без NaN в z):", len(df_model))

print()
print("Процент пригодных строк:",
      round(len(df_model) / len(df) * 100, 2), "%")