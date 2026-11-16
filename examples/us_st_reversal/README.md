# Example: US short-term reversal (monthly)

An end-to-end demonstration of the skill: pull a CRSP monthly panel on WRDS
Cloud, then analyze it locally. The research question is the classic one —
do stocks with poor past-month returns outperform over the next month?

- `spec.json` — builds the extract that gets submitted to the cloud. It
  aggregates `crsp.dsf` (daily) to monthly returns, joins `crsp.dsenames` for
  the share/exchange codes, and joins `crsp.dsedelist` for delisting returns.
- `analyze.py` — decile sort on the month-t return, next-month equal-weighted
  returns, D1−D10 long-short, ordinary time-series t-test.

## Universe and return construction

- Common stocks: `shrcd in (10, 11)`; major exchanges: `exchcd in (1, 2, 3)`.
  These come from `crsp.dsenames` (`namedt`/`nameendt`), not from `dsf`, which
  carries only the exchange code `hexcd` and no share code.
- Monthly return: compounding of daily `ret` via
  `exp(sum(ln(1 + ret::float8))) - 1`. `ret` is `numeric` in `dsf`, so it is
  cast to `float8` so that a valid −100% day (`ret = -1`) maps to a −100% month
  instead of raising on `ln(0)`.
- Missing-return codes: CRSP uses negative sentinel values below −1 (e.g.
  −99, −88, …). The extract keeps `ret >= -1`, which drops those codes while
  keeping a genuine −100% return.
- Delisting: `dlret` from `crsp.dsedelist` is left-joined on the delisting
  month; `analyze.py` folds it in as
  `ret_adj = (1 + ret_m) * (1 + dlret) - 1`.

## Reproduce

From your project root, with the skill installed and `~/.env` configured:

```bash
# 1. Copy the spec and submit the extract to the cloud
python <skill-dir>/scripts/run_cloud.py submit \
  --spec <skill-dir>/examples/us_st_reversal/spec.json --wait

# 2. Analyze the pulled parquet
python <skill-dir>/examples/us_st_reversal/analyze.py --data data/us_st_reversal_daily
```

## Filters and conventions

- Formation month t: `avg_prc >= $5` and `n_days >= 15`, both computed from
  month-t data only, so no future information enters the ranking.
- Signal: month-t delisting-adjusted return; portfolios are held in month t+1
  (aligned via `shift(-1)` within each `permno`), so there is no look-ahead.
- The t statistic is a plain time-series t-test, not Newey-West.
- This is a pipeline demonstration, not an investable strategy: no transaction
  costs, borrow constraints, or liquidity adjustment.

## Reference results

See `data/us_st_reversal_daily/summary.json` produced by `analyze.py`. Values
are recomputed from the extract above; they are not tuned to any prior figure.
