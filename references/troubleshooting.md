# Troubleshooting

Read the failing SQL and the first error line before changing code.

## Permission and coverage

| symptom | cause | action |
|---|---|---|
| `permission denied for schema crsp_a_ccm` on `crsp.ccmxpf_linktable` / `lnkhist` | CRSP-Compustat link is a separate subscription | request CCM access, or link manually on `permno`+`gvkey` with your own crosswalk |
| `relation "csmar_trade.trd_dalyr" does not exist` (also `trd_cndalym`, `trd_cale`) | table appears in metadata but the underlying schema is not provisioned | use `csmar.trd_adjustfactor` / `csmar.fs_combas`, or buy the trade module |
| `relation "csmar_trade..."` for other tables | same as above | check with `select count(*)` before building a spec |
| CRSP data stops at 2024-12-31 across `dsf`, `msf`, `dsi`, `stocknames` | dataset not refreshed past that date | confirm subscription status; do not report it as a query bug |

`db.list_tables(library=...)` and `db.describe_table()` read metadata and can succeed for tables that are not actually queryable. Always run `select count(*)` once before trusting a new table.

## Local path

- `plan.py` says cloud but the job is small — the row count timed out. Raise `--timeout-ms` or pass `--route local`.
- Local writes are slow or memory-heavy — lower `--chunk`; parquet file size, not RAM, is the routing signal.
- `.env` not found — `wrds_common.find_env_file()` walks up four directories from cwd, then `~/.env`. Run commands from the project root.

## Cloud path

- **`Libref ... must be followed either by quoted string`** in a `qsas` log — broken `~/autoexec.sas`. This affects SAS jobs only; see `wrds-cloud-env.md`. Python `qsub` jobs are unaffected.
- `still queued after 1800s` — institutional queue congestion, not an error. Check later with `status`; do not resubmit blindly, duplicates write to the same `out/`.
- Job disappears from `qstat` but `job.log` is missing — the job may have failed before start. Run `qstat -j <id>` and check `sge_o_workdir`.
- `remote out/ not found` on `pull` — the job is still running, or it never wrote output. `pull` already retries for 120 s because SGE drops the job from `qstat` a few seconds before files land; beyond that, check `status` for the log tail.
- Job written somewhere unexpected — the scratch probe found no writable `/scratch/$USER` or `/scratch/*/$USER` and fell back to `$HOME/wrds_jobs`. Pin the location with `--remote-root /path/wrds_jobs` or `WRDS_CLOUD_ROOT`.
- `mkdir: cannot create directory` / permission denied on submit — the probe chose a base the account cannot write, usually a stale institutional scratch. Override it with `WRDS_CLOUD_ROOT`. All remote paths are built from this one value; nothing is hard-coded per institution.
- `outputs are X, above the pull limit` — intentional. The manifest is already downloaded; call `pull --tables a,b` for the tables an analysis step actually needs.
- Script mode ignores your file — `--script` uploads it as `user_script.py` and runs it instead of the generic extractor. It must write into `out/`.

## Housekeeping

- Scratch is shared across the whole institution. `run_cloud.py clean --job <job>` after a successful pull; delete anything older than the current project phase.
- `data/<job>/cloud_run.json` is the only link to a remote directory. Keep it until cleanup.
- Never commit `data/` or `.env`; parquet outputs are reproducible from the manifest SQL.
