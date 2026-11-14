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

Each non-sample/trial library returned by WRDS's access-aware `wrds.list_libraries()` is recorded as `ok`, meaning authorized at the library level. WRDS applies schema `USAGE` and resolves friendly views to their dependent product relations; those views check `SELECT` on the underlying tables. Building the snapshot issues no per-table queries. This is deliberately different from raw `information_schema` visibility or a standalone `has_table_privilege()` check on an outer view, either of which can overstate access. Library authorization does not prove every table works; use `discover.py --table` for a real-column check of the specific table. `gaps` collects run-time failures and is carried across rebuilds until the table is probed successfully. Location: `Path.home() / ".wrds_inventory.json"` by default, which lets projects and agent apps under one OS user share the snapshot on Windows, macOS and Linux without configuration. The working directory is only a fallback when home is not writable. `$WRDS_INVENTORY` is an optional override, not a required setting; `~` in its value is expanded.

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

`remote_root` is resolved at submit time: `--remote-root`, then `WRDS_CLOUD_ROOT`, then `$HOME/wrds_jobs` on the cloud host. Code, logs, specs and `out/manifest.json` always live there, so a full or unprovisioned scratch cannot block a submission. Large parquet outputs can be redirected with `--scratch-dir` (or `WRDS_CLOUD_SCRATCH`), which writes them to `<scratch_root>/<job>/out`; `submit` records both paths in `cloud_run.json` so `pull` and `clean` target the right directories.

`data/<job>/cloud_run.json` on the client stores `job`, `job_id`, `remote_dir`, `remote_meta_dir`, `remote_data_dir`, `entry`, `submitted_utc`, `user`. `status`, `pull` and `clean` read it; keep it until the remote directory is deleted.

## Custom cloud scripts

`--script my.py` uploads the file and runs it instead of `run_remote.py`. Requirements:

- `cd` is already the remote job directory; write metadata into `out/` and large parquet into `$WRDS_DATA_DIR` when it is set.
- Call `wrds.Connection()` with no arguments. `submit` provisions `~/.pgpass` (mode 600) from `.env` when it is missing.
- End with `out/manifest.json` in the schema above if you want `pull` to read row counts and column names. Without it, `pull` fails by design.
- Never print credentials; `job.log` is retained in the home job directory.
