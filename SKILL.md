---
name: wrds-data-pipeline
description: Fetch WRDS data for a project or paper, routing big pulls to WRDS Cloud jobs and small pulls to local parquet. Table-agnostic: works with any library the account subscribes to (CRSP, Compustat, CSMAR, IBES, Thomson, Ravenpack, …). Use scripts/discover.py to find what is actually reachable. Do not use for non-WRDS data sources.
---

# WRDS Data Pipeline

Turn a data requirement (project step or paper replication) into either a local parquet extract or a WRDS Cloud job, then hand reproducible files back to the analysis code.

## Workflow

1. **Write a spec** — copy `assets/spec.example.json` and fill `job` + `tables`. Each table is either raw `sql` or `library`/`table`/`columns`/`where`.
2. **Confirm access for unfamiliar libraries** — the first connection builds a subscription snapshot automatically and caches it for 90 days, so repeat runs are instant. `python scripts/discover.py` shows the cached summary; `--refresh` rebuilds it, `--gaps` lists tables that failed at run time. Subscriptions vary by institution, so probe before writing SQL against a library you have not used on this account: `--schema <name>` lists tables, `--table <schema>.<table>` lists columns and runs a live access probe. `plan.py` reuses the snapshot and warns when a spec targets a schema or table the account is known to lack.
3. **Verify access once per machine** — `python scripts/check_env.py`. It checks local credentials, the PostgreSQL endpoint, the cloud SSH login, the cloud Python stack and the cloud `~/.pgpass`.
4. **Let the router decide** — `python scripts/plan.py --spec spec.json`. It estimates rows and download bytes per table, then prints the route (`local` or `cloud`) with reasons.
5. **Run the chosen path**:
   - local: `python scripts/run_local.py --spec spec.json`
   - cloud: `python scripts/run_cloud.py submit --spec spec.json --wait`
6. **Consume `data/<job>/manifest.json`** in the analysis code. Never hard-code column order or row counts; read both from the manifest.
7. **Inspect cloud jobs** with `run_cloud.py status --job <job>` and fetch results with `run_cloud.py pull --job <job>`.

Override routing only when you have a reason the estimator cannot see: `plan.py --route cloud` or `run_local.py --force`.

## Routing rule

Estimator output drives the decision; thresholds live in `scripts/plan.py` and can be overridden per call.

| Condition | Route |
|---|---|
| estimated download ≤ 250 MB and ≤ 2.5M rows | local |
| above either threshold, or row count unknown | cloud |
| cross-table joins over full-history panels | cloud |
| iterative exploration, schema checks, ≤ 5 queries | local |

Local is cheaper end-to-end because it skips the SGE queue (observed queue waits of 15 s to 5 min). Cloud avoids moving gigabytes over the wire and gives 500 GB scratch plus a 64-core node. Details and worked examples: `references/routing-rules.md`.

## Two paths, one contract

Both paths produce the same layout, so analysis code does not care which one ran:

```
data/<job>/
├── manifest.json     # job, route, per-table rows/bytes/columns/sql/sha256
├── provenance.json   # verbatim SQL, timestamps, WRDS user, route details
└── <table>.parquet   # one file per table
```

Cloud adds `cloud_run.json` (job id, remote dir, submit time) and keeps the remote copy under `<remote_root>/<job>/` with `job.log` and `out/`. `remote_root` is probed on the cloud host (writable scratch, then `$HOME`) and can be pinned with `--remote-root` or `WRDS_CLOUD_ROOT`; never assume an institutional path. Full schemas: `references/contracts.md`.

Cloud scripts handed to `--script` must write into `out/` themselves and must call `wrds.Connection()` with no arguments (the skill provisions `~/.pgpass` on the cloud host).

## Commands

```bash
python scripts/discover.py                                   # cached subscription summary
python scripts/discover.py --refresh                         # rebuild the snapshot now
python scripts/discover.py --gaps                            # tables that failed before
python scripts/discover.py --schema comp                     # tables in a schema
python scripts/discover.py --table comp.funda                # columns + access probe
python scripts/discover.py --grep linktable                  # search table names
python scripts/discover.py --live                            # table counts straight from catalog
python scripts/check_env.py                                  # one-time access check
python scripts/plan.py --spec spec.json [--route local|cloud] [--json]
python scripts/run_local.py --spec spec.json [--force] [--chunk 500000]
python scripts/run_cloud.py submit --spec spec.json [--script x.py] [--wait] [--mem 8G]
python scripts/run_cloud.py status --job <job>
python scripts/run_cloud.py pull --job <job> [--max-mb 500] [--tables a,b]
python scripts/run_cloud.py clean --job <job>
```

`pull` refuses to download more than `--max-mb`; it still fetches `manifest.json` so the analysis code can read the schema and then request specific tables.

## Non-negotiable rules

- Credentials come from `.env` at the project root (`WRDS_USER`, `WRDS_PASS`). Never print, echo or hard-code them; never write them into a spec, log or parquet path.
- Always delete `*.parquet` / `*.csv` derivatives from the skill directory; they belong under `data/` or the project's own data folder.
- Prefer one spec per analysis step over one giant spec, so a failed table does not force a re-download.
- Check `references/troubleshooting.md` before debugging a failure; permission-denied and missing-schema errors are usually subscription issues, not code bugs.
- Never assume a library or table exists. Subscriptions differ by institution and by year — probe with `discover.py --table` first, and treat `NOT queryable` as a fact about the account, not something to work around by editing SQL.
