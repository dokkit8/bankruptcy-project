import pandas as pd
import numpy as np
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATASET_PATH = DATA_DIR / "dataset_v1.csv"
NORM_PATH = DATA_DIR / "norm_table.csv"
OUT_PATH = DATA_DIR / "dataset_with_features.csv"

EPS = 1e-6


def find_norm(norm_df, period, okved2, section):
    # 1 уровень: okved2
    row = norm_df[
        (norm_df["level"] == "okved2") &
        (norm_df["period"] == period) &
        (norm_df["key"] == okved2)
    ]
    if len(row) > 0:
        return row.iloc[0]

    # 2 уровень: section
    row = norm_df[
        (norm_df["level"] == "section") &
        (norm_df["period"] == period) &
        (norm_df["key"] == section)
    ]
    if len(row) > 0:
        return row.iloc[0]

    # 3 уровень: period fallback
    row = norm_df[
        (norm_df["level"] == "period") &
        (norm_df["period"] == period)
    ]
    if len(row) > 0:
        return row.iloc[0]

    return None


def compute_z(value, median, mad):
    if pd.isna(value) or pd.isna(median) or pd.isna(mad):
        return np.nan
    mad_safe = mad if mad > EPS else EPS
    return (value - median) / mad_safe


def build_deviation_features():
    df = pd.read_csv(DATASET_PATH)
    norm_df = pd.read_csv(NORM_PATH)

    z_margin = []
    z_debt = []
    z_turnover = []
    z_growth = []

    for _, row in df.iterrows():
        period = row["period"]
        okved2 = row["okved2"]
        section = row["okved_section"]

        norm_row = find_norm(norm_df, period, okved2, section)

        if norm_row is None:
            z_margin.append(np.nan)
            z_debt.append(np.nan)
            z_turnover.append(np.nan)
            z_growth.append(np.nan)
            continue

        z_margin.append(
            compute_z(row["margin"],
                      norm_row["median_margin"],
                      norm_row["mad_margin"])
        )

        z_debt.append(
            compute_z(row["debt_ratio"],
                      norm_row["median_debt_ratio"],
                      norm_row["mad_debt_ratio"])
        )

        z_turnover.append(
            compute_z(row["inv_turnover"],
                      norm_row["median_inv_turnover"],
                      norm_row["mad_inv_turnover"])
        )

        z_growth.append(
            compute_z(row["growth_rev"],
                      norm_row["median_growth"],
                      norm_row["mad_growth"])
        )

    df["z_margin"] = z_margin
    df["z_debt_ratio"] = z_debt
    df["z_inv_turnover"] = z_turnover
    df["z_growth"] = z_growth

    df.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print("Deviation features saved to:", OUT_PATH)


if __name__ == "__main__":
    build_deviation_features()