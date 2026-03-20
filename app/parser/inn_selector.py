import pandas as pd
import numpy as np
from pathlib import Path
from typing import Literal

# ==========================
# Конфигурация
# ==========================

MODE: Literal["balanced", "realistic", "hybrid"] = "hybrid"

TOTAL_TARGET = 10000
MIN_COMPANIES_PER_GROUP = 1000
TOP_GROUPS = 20
RANDOM_STATE = 42


# ==========================
# Загрузка и подготовка
# ==========================

def load_data(path: str) -> pd.DataFrame:
    print("Loading in chunks...")

    chunks = []

    for chunk in pd.read_csv(
            path,
            sep=";",
            usecols=["inn", "okved", "region"],
            dtype=str,
            chunksize=200_000
    ):
        chunk = chunk.dropna(subset=["inn", "okved", "region"])
        chunks.append(chunk)

    df = pd.concat(chunks, ignore_index=True)
    df["okved_group"] = df["okved"].str[:2]

    print("Loaded rows:", len(df))
    return df


# ==========================
# Balanced выборка
# ==========================

def sample_balanced(df: pd.DataFrame, total: int) -> pd.DataFrame:
    group_counts = df["okved_group"].value_counts()
    valid_groups = group_counts[group_counts >= MIN_COMPANIES_PER_GROUP].index
    df = df[df["okved_group"].isin(valid_groups)]

    top_groups = df["okved_group"].value_counts().head(TOP_GROUPS).index
    df = df[df["okved_group"].isin(top_groups)]

    per_group = total // len(top_groups)

    result = []

    for group in top_groups:
        sub = df[df["okved_group"] == group]

        region_counts = sub["region"].value_counts()
        valid_regions = region_counts[region_counts >= 50].index
        sub = sub[sub["region"].isin(valid_regions)]

        regions = sub["region"].unique()
        per_region = max(1, per_group // len(regions))

        selected = []

        for r in regions:
            sub_r = sub[sub["region"] == r]
            selected.append(
                sub_r.sample(
                    min(per_region, len(sub_r)),
                    random_state=RANDOM_STATE
                )
            )

        group_sample = pd.concat(selected)

        if len(group_sample) < per_group:
            remaining = sub.drop(group_sample.index)
            need = per_group - len(group_sample)
            if len(remaining) >= need:
                group_sample = pd.concat([
                    group_sample,
                    remaining.sample(need, random_state=RANDOM_STATE)
                ])

        result.append(group_sample)

    return pd.concat(result)


# ==========================
# Реалистичная выборка
# ==========================

def sample_realistic(df: pd.DataFrame, total: int) -> pd.DataFrame:
    return df.sample(total, random_state=RANDOM_STATE)


# ==========================
# Гибридная выборка
# ==========================

def sample_hybrid(df: pd.DataFrame, total: int) -> pd.DataFrame:
    balanced_part = int(total * 0.7)
    realistic_part = total - balanced_part

    df_balanced = sample_balanced(df, balanced_part)
    df_remaining = df.drop(df_balanced.index)

    df_realistic = sample_realistic(df_remaining, realistic_part)

    return pd.concat([df_balanced, df_realistic])


# ==========================
# Главная функция
# ==========================

def build_inn_sample(
    input_path: str,
    output_path: str,
    total: int = TOTAL_TARGET,
    mode: Literal["balanced", "realistic", "hybrid"] = MODE
) -> None:

    df = load_data(input_path)

    if mode == "balanced":
        result = sample_balanced(df, total)
    elif mode == "realistic":
        result = sample_realistic(df, total)
    elif mode == "hybrid":
        result = sample_hybrid(df, total)
    else:
        raise ValueError("Unknown mode")

    result = result.sample(frac=1, random_state=RANDOM_STATE).reset_index(drop=True)

    result.to_csv(output_path, sep=";", index=False)

    print("================================")
    print(f"Mode: {mode}")
    print(f"Total selected: {len(result)}")
    print("Industry distribution:")
    print(result["okved_group"].value_counts())
    print("================================")


# ==========================
# CLI запуск
# ==========================

if __name__ == "__main__":
    BASE_DIR = Path(__file__).resolve().parent
    DATA_DIR = BASE_DIR / "data"

    build_inn_sample(
        input_path=str(DATA_DIR / "inn.csv"),
        output_path=str(DATA_DIR / "balanced_inn.csv"),
        total=30000,
        mode="hybrid"
    )
