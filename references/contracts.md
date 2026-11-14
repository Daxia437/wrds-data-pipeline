# Contracts

Both paths write the same client-side layout, so analysis code never branches on route.

## spec.json

| field | required | meaning |
|---|---|---|
| `job` | yes | job name; also the output folder name |
| `output_dir` | no | client output root, default `data` (relative to cwd) |
| `route` | no | `local` / `cloud`; `cloud` disables the local size guard |
| `script` | no | path to a Python file to run on the cloud host instead of the generic extractor |
| `tables[]` | yes unless `script` | one entry per output file |

Table entry:

| field | meaning |
|---|---|
| `name` | output file stem, used as is |
| `sql` | full SQL, preferred when joins or window functions are needed |
| `library`, `table` | alternative to `sql` |
| `columns` | list or `["*"]` |
| `where`, `order_by` | appended to the generated SQL |
| `force_cloud` | route this table to the cloud regardless of size |

SQL must be read-only. Do not add a trailing semicolon; it is stripped anyway.

## data/<job>/manifest.json

```json
{
  "job": "crsp_dsf_2024_q4",
  "route": "local",
  "created_utc": "2026-11-13T02:00:00+00:00",
  "wrds_user": "<wrds_user>",
  "tables": [
    {
      "name": "crsp_dsf",
      "file": "crsp_dsf.parquet",
      "rows": 48511,
      "bytes": 1103028,
      "sha256": "...",
      "columns": ["permno", "date", "prc", "ret", "vol", "shrout"],
      "dtypes": {"permno": "Int64", "date": "datetime64[ns]"},
      "sql": "select ... from crsp.dsf where ..."
    }
  ]
}
```

Cloud jobs produce the same fields (`wrds_user`, per-table `sha256` and `dtypes`) and add `host`, `python`, `pandas`, `elapsed_sec` per table and an `errors` list. A non-empty `errors` list means some tables failed but others still landed; read `errors` before trusting the job. Date columns are cast to `datetime64[ns]` on both routes, so a local and a cloud extract of the same query are identical in content.

## data/<job>/provenance.json

Local runs record the full spec, interpreter versions and output directory. Cloud pulls record `job_id`, `remote_dir`, `submitted_utc` and the download time.

## Subscription inventory (`.wrds_inventory.json`)

Written by `ensure_inventory()`, which `check_env.py`, `discover.py` and `plan.py` call. The cache holds the current WRDS user, is valid for `ttl_days`, and is rebuilt automatically when missing, stale, from an older snapshot `version`, or belonging to a different user.

```json
{
  "version": 3,
  "wrds_user": "<wrds_user>",
  "built_utc": "2026-11-14T02:46:34+00:00",
  "ttl_days": 90,
  "build_seconds": 0.2,
  "method": "wrds.list_libraries() access metadata (schema USAGE and view dependencies), samples excluded",
  "libraries": {
    "comp": {"status": "ok"},
    "crsp": {"status": "ok"}
  },
  "status_counts": {"ok": 91},
  "gaps": {
    "csmar.trd_dalyr": {
      "error": "relation \"csmar_trade.trd_dalyr\" does not exist",
      "learned_utc": "2026-11-14T02:51:07+00:00"
    }
  }
}
```

Each non-sample/trial library returned by WRDS's access-aware `wrds.list_libraries()` is recorded as `ok`, meaning authorized at the library level. WRDS applies schema `USAGE` and resolves friendly views to their dependent product relations; those views check `SELECT` on the underlying tables. Building the snapshot issues no per-table queries. This is deliberately different from raw `information_schema` visibility or a standalone `has_table_privilege()` check on an outer view, either of which can overstate access. Library authorization does not prove every table works; use `discover.py --table` for a real-column check of the specific table. `gaps` collects run-time failures and is carried across rebuilds until the table is probed successfully. Location: `$WRDS_INVENTORY`, else `~/.wrds_inventory.json`, else the working directory.

## Cloud layout

```
<remote_root>/<job>/
├── spec.json
├── run_remote.py     # or user_script.py with --script
├── job.sh            # SGE directives, -l h_vmem=<mem>
├── job.log           # stdout+stderr, ends with EXIT_CODE=<n>
└── out/
    ├── <table>.parquet
    └── manifest.json
```

`remote_root` is resolved on the cloud host at submit time: `--remote-root`, then `WRDS_CLOUD_ROOT`, then `/scratch/$USER` if writable, then `/scratch/*/$USER`, then `$HOME`, each suffixed with `/wrds_jobs`. All remote paths are built from this value plus the job name; nothing institution-specific is hard-coded. `submit` records the resolved root in `cloud_run.json` so `clean` removes the right directory even if the probe would answer differently later.

`data/<job>/cloud_run.json` on the client stores `job`, `job_id`, `remote_dir`, `entry`, `submitted_utc`, `user`. `status`, `pull` and `clean` read it; keep it until the remote directory is deleted.

## Custom cloud scripts

`--script my.py` uploads the file and runs it instead of `run_remote.py`. Requirements:

- `cd` is already the remote job directory; write everything into `out/`.
- Call `wrds.Connection()` with no arguments. `submit` provisions `~/.pgpass` (mode 600) from `.env` when it is missing.
- End with `out/manifest.json` in the schema above if you want `pull` to read row counts and column names. Without it, `pull` fails by design.
- Never print credentials; `job.log` is retained on scratch.
