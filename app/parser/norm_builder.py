import pandas as pd
from pathlib import Path
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATASET_PATH = DATA_DIR / "dataset_v1.csv"
OUT_NORM_PATH = DATA_DIR / "norm_table.csv"

MIN_OKVED_GROUP = 12
MIN_SECTION_GROUP = 20
MIN_VALID_PER_METRIC = 10


def compute_mad(series):
    median = series.median()
    mad = np.median(np.abs(series - median))
    return mad


def safe_stats(series):
    valid = series.dropna()
    if len(valid) >= MIN_VALID_PER_METRIC:
        median = valid.median()
        mad = compute_mad(valid)
        return median, mad
    else:
        return None, None


def build_norms():

    df = pd.read_csv(DATASET_PATH)

    norm_rows = []

    for period in df["period"].unique():
        df_p = df[df["period"] == period]

        # =========================
        # 1️⃣ OKVED2 уровень
        # =========================
        g1 = df_p.groupby("okved2")

        for okved2, group in g1:
            n = len(group)

            if n >= MIN_OKVED_GROUP:

                median_margin, mad_margin = safe_stats(group["margin"])
                median_debt_ratio, mad_debt_ratio = safe_stats(group["debt_ratio"])
                median_inv_turnover, mad_inv_turnover = safe_stats(group["inv_turnover"])
                median_growth, mad_growth = safe_stats(group["growth_rev"])

                norm_rows.append({
                    "level": "okved2",
                    "key": okved2,
                    "period": period,
                    "n_companies": n,

                    "median_margin": median_margin,
                    "mad_margin": mad_margin,

                    "median_debt_ratio": median_debt_ratio,
                    "mad_debt_ratio": mad_debt_ratio,

                    "median_inv_turnover": median_inv_turnover,
                    "mad_inv_turnover": mad_inv_turnover,

                    "median_growth": median_growth,
                    "mad_growth": mad_growth,
                })

        # =========================
        # 2️⃣ OKVED SECTION уровень
        # =========================
        g2 = df_p.groupby("okved_section")

        for section, group in g2:
            n = len(group)

            if n >= MIN_SECTION_GROUP:

                median_margin, mad_margin = safe_stats(group["margin"])
                median_debt_ratio, mad_debt_ratio = safe_stats(group["debt_ratio"])
                median_inv_turnover, mad_inv_turnover = safe_stats(group["inv_turnover"])
                median_growth, mad_growth = safe_stats(group["growth_rev"])

                norm_rows.append({
                    "level": "section",
                    "key": section,
                    "period": period,
                    "n_companies": n,

                    "median_margin": median_margin,
                    "mad_margin": mad_margin,

                    "median_debt_ratio": median_debt_ratio,
                    "mad_debt_ratio": mad_debt_ratio,

                    "median_inv_turnover": median_inv_turnover,
                    "mad_inv_turnover": mad_inv_turnover,

                    "median_growth": median_growth,
                    "mad_growth": mad_growth,
                })

        # =========================
        # 3️⃣ FALLBACK по году
        # =========================
        median_margin, mad_margin = safe_stats(df_p["margin"])
        median_debt_ratio, mad_debt_ratio = safe_stats(df_p["debt_ratio"])
        median_inv_turnover, mad_inv_turnover = safe_stats(df_p["inv_turnover"])
        median_growth, mad_growth = safe_stats(df_p["growth_rev"])

        norm_rows.append({
            "level": "period",
            "key": "ALL",
            "period": period,
            "n_companies": len(df_p),

            "median_margin": median_margin,
            "mad_margin": mad_margin,

            "median_debt_ratio": median_debt_ratio,
            "mad_debt_ratio": mad_debt_ratio,

            "median_inv_turnover": median_inv_turnover,
            "mad_inv_turnover": mad_inv_turnover,

            "median_growth": median_growth,
            "mad_growth": mad_growth,
        })

    norm_df = pd.DataFrame(norm_rows)
    norm_df.to_csv(OUT_NORM_PATH, index=False)

    print("Norm table saved:", OUT_NORM_PATH)


if __name__ == "__main__":
    build_norms()