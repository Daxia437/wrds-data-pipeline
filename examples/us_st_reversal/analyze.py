"""Short-term reversal example on CRSP monthly data built from daily returns.

Signal: month-t return (delisting-adjusted). Portfolio: equal-weighted deciles,
long D1 (worst) and short D10 (best), held for the next month. The t statistic
is an ordinary time-series t-test (no Newey-West).

Usage (from the project root, after the spec has been pulled):

    python <skill-dir>/examples/us_st_reversal/analyze.py --data data/us_st_reversal_daily
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

MIN_PRICE = 5.0
MIN_DAYS = 15
DECILES = 10


def load_panel(data_dir: Path) -> pd.DataFrame:
    frame = pd.read_parquet(data_dir / "crsp_monthly.parquet")
    frame["month"] = pd.to_datetime(frame["month"])
    # Delisting-adjusted month-t return (dlret is null outside the delisting month).
    frame["ret_adj"] = (1 + frame["ret_m"]) * (1 + frame["dlret"].fillna(0.0)) - 1
    # Formation-month filters use month-t information only (no look-ahead).
    frame = frame[(frame["avg_prc"] >= MIN_PRICE) & (frame["n_days"] >= MIN_DAYS)].copy()
    frame.sort_values(["permno", "month"], inplace=True)
    # Holding return = next month's return; align signal(t) with return(t+1).
    frame["ret_next"] = frame.groupby("permno")["ret_adj"].shift(-1)
    frame["month_next"] = frame.groupby("permno")["month"].shift(-1)
    frame = frame[frame["month_next"] == frame["month"] + pd.offsets.MonthBegin(1)]
    return frame.dropna(subset=["ret_next"])


def decile_returns(frame: pd.DataFrame) -> pd.DataFrame:
    ranked = frame.groupby("month")["ret_adj"].transform(
        lambda s: pd.qcut(s.rank(method="first"), DECILES, labels=False) + 1
    )
    frame = frame.assign(decile=ranked)
    returns = frame.groupby(["month", "decile"])["ret_next"].mean().unstack()
    returns.columns = [f"D{int(c)}" for c in returns.columns]
    returns["LS"] = returns["D1"] - returns[f"D{DECILES}"]
    return returns.dropna()


def summarize(returns: pd.Series) -> dict:
    mean = returns.mean()
    std = returns.std()
    return {
        "months": int(returns.size),
        "mean_monthly": round(float(mean), 6),
        "t_stat": round(float(mean / std * np.sqrt(returns.size)), 2),
        "win_rate": round(float((returns > 0).mean()), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data", default="data/us_st_reversal_daily",
        help="job output directory containing crsp_monthly.parquet",
    )
    args = parser.parse_args()
    data_dir = Path(args.data)

    panel = load_panel(data_dir)
    returns = decile_returns(panel)
    summary = {
        "sample": f"{returns.index.min():%Y-%m} to {returns.index.max():%Y-%m}",
        "universe": "CRSP common stocks (SHRCD 10/11, EXCHCD 1-3), delisting-adjusted",
        "filters": f"month-t avg price >= ${MIN_PRICE:.0f}, >= {MIN_DAYS} trading days",
        "signal": "month-t return (delisting-adjusted)",
        "holding": "next month, equal-weighted deciles",
        "long_short": "D1 minus D10 (long losers, short winners)",
        "t_test": "ordinary time-series t-test (no Newey-West)",
        "long_short_stats": summarize(returns["LS"]),
        "decile_mean_monthly": {
            column: round(float(returns[column].mean()), 6)
            for column in returns.columns
        },
    }
    returns.to_csv(data_dir / "decile_returns.csv", index_label="month")
    (data_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(returns.tail(6).round(4).to_string())
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
