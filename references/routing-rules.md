# Routing rules

`plan.py` sizes every table, then compares the totals against the local limits. The rules are library-agnostic — the CRSP and Compustat figures below are calibration examples, not a whitelist. Cost scales with rows and bytes, not with which vendor supplies them.

## How size is estimated

1. **Rows** — `select count(*) from (<sql>) q` with `statement_timeout = 20s`.
   - On timeout, fall back to `pg_class.reltuples` for `<lib>.<table>`.
   - WRDS exposes most data through views (`crsp.dsf` is a view), so catalog stats are often `-1`. Unknown rows route to the cloud instead of guessing.
2. **Bytes per row** — sample 1,000 rows, compress to snappy parquet in memory, divide. Falls back to `csv_bytes × 0.4` when pyarrow is unavailable.
3. **Download size** — rows × bytes per row. This is the client-side transfer size, which is what actually hurts on a laptop.

Thresholds: `LOCAL_MAX_MB = 250`, `LOCAL_MAX_ROWS = 2_500_000`, both overridable per call.

## Why these numbers

Measured on this account (2026-11). CRSP figures are `select permno, date, prc, ret, vol, shrout from crsp.dsf` (the core daily-return columns, snappy parquet); a wider column set produces a larger file.

| extract | rows | parquet |
|---|---|---|
| `crsp.dsf` one month (2024-12) | 204K | ~3 MB |
| `crsp.dsf` one quarter (2024 Q4) | 620K | ~10 MB |
| `crsp.dsf` one year (2024) | 2.40M | ~39 MB |
| `crsp.dsf` full history | not measurable — catalog `reltuples = -1` (a view) | routes to cloud |
| `comp.funda` `datadate >= 2024-01-01` | 43.6K | <5 MB |
| `csmar.fs_combas` `accper >= 2024-01-01` | 90.9K | <10 MB |

A single quarter of CRSP daily data fits comfortably under the local limits; a full year (2.4M rows) sits just under the 2.5M-row threshold, and multi-year or full-history pulls route to the cloud.

## Route characteristics

| | local | cloud |
|---|---|---|
| latency | query + write only | queue wait 15 s to 5 min, then run |
| measured | 48.5K rows in ~2 s | 4.5 s compute, 94 s queue |
| transfer | full parquet to laptop | only requested outputs |
| limits | RAM, disk, home bandwidth | 500 GB shared scratch, 64 cores, `h_vmem=8G` default |
| best for | iteration, schema checks, < 250 MB | full panels, multi-table joins, anything near a GB |

## Overrides

- `plan.py --route cloud` or `--route local` — skip the estimate.
- `run_local.py --force` — run locally even when the plan says cloud.
- `"force_cloud": true` on a table — always cloud for that table.

Use an override when the estimator is blind: it cannot see downstream joins, repeated runs of the same query, or a temporarily slow laptop network.

## Split, do not inflate

If a spec mixes a 3 GB panel with a 40 MB annual file, split them into two specs. The small one should stay local and immediate; the big one runs in the cloud. `run_cloud.py pull --tables` then fetches only what an analysis step needs.
