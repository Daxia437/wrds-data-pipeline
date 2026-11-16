# Example: US short-term reversal (monthly)

An end-to-end demonstration of the skill: pull a CRSP daily panel on WRDS
Cloud, then analyze it locally. The research question is the classic one —
do stocks with poor past-month returns outperform over the next month?

- `spec.json` — monthly aggregation of `crsp.dsf` (2015–2024), the extract
  that gets submitted to the cloud.
- `analyze.py` — decile sort on the current-month return, next-month
  equal-weighted returns, D1−D10 long-short, ordinary time-series t-test.

## Reproduce

From your project root, with the skill installed and `~/.env` configured:

```bash
# 1. Copy the spec and submit the extract (about 750k rows, ~20 MB)
python <skill-dir>/scripts/run_cloud.py submit \
  --spec <skill-dir>/examples/us_st_reversal/spec.json --wait

# 2. Analyze the pulled parquet
python <skill-dir>/examples/us_st_reversal/analyze.py --data data/us_st_reversal_daily
```

## Filters and conventions

- Universe: CRSP common stocks (EXCHCD 1–3), price ≥ $5, ≥ 15 trading days
  in the month.
- Signal: month-t return; portfolios are held in month t+1 (no look-ahead).
- The t statistic is a plain time-series t-test, not Newey-West.
- This is a pipeline demonstration, not an investable strategy: no transaction
  costs, borrow constraints, or liquidity adjustment.

Reference results (119 months, 2015-01 to 2024-11): D1 mean +1.56%/month,
D10 +0.46%, long-short +1.10% with t = 2.62. Deciles decline overall from
D1 to D10 but are not strictly monotonic (D2–D4 are close together).
