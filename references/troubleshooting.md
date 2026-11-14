# Troubleshooting

Read the failing SQL and the first error line before changing code.

## Permission and coverage

Access is per institution and changes over time, so the examples below describe one verified account rather than WRDS in general. Confirm any table before writing SQL: `python scripts/discover.py --table <schema>.<table>` runs a live probe and reports the real error.

| symptom | cause | action |
|---|---|---|
| `cannot cache the subscription inventory` | neither home nor the working directory is writable | set `WRDS_INVENTORY` to a writable path; the run continues without a cache |
| snapshot still lists a library you lost access to | the 90-day cache has not expired | `discover.py --refresh` |
| snapshot missing a library you just subscribed to | same | `discover.py --refresh`; the next `plan.py` reuses it |
| a table warns "failed before" but works now | the gap was recorded earlier | `discover.py --table <schema>.<table>` clears it once the probe succeeds |
| `permission denied for schema crsp_a_ccm` on `crsp.ccmxpf_linktable` / `lnkhist` | CRSP-Compustat link is a separate subscription | request CCM access, or link manually on `permno`+`gvkey` with your own crosswalk |
| `relation "csmar_trade.trd_dalyr" does not exist` (also `trd_cndalym`, `trd_cale`) | table appears in metadata but the underlying schema is not provisioned | use `csmar.trd_adjustfactor` / `csmar.fs_combas`, or buy the trade module |
| `relation "csmar_trade..."` for other tables | same as above | run `discover.py --table <schema>.<table>` before building a spec |
| CRSP data stops at 2024-12-31 across `dsf`, `msf`, `dsi`, `stocknames` | dataset not refreshed past that date | confirm subscription status; do not report it as a query bug |

`db.list_tables(library=...)`, `db.describe_table()`, raw `information_schema` visibility, and `has_table_privilege()` on an outer view can indicate access for tables that are not actually queryable. WRDS's `list_libraries()` is different: it is an access-aware library list based on schema `USAGE` and friendly-view dependencies, so the snapshot uses it directly without querying every table. A library marked `ok` is authorized at the library level, not validated table by table. Always run `discover.py --table <schema>.<table>` before trusting a new table; its real-column `LIMIT 1` probe exposes denied or missing underlying relations without scanning the table.

## Local path

- `plan.py` says cloud but the job is small — the row count timed out. Raise `--timeout-ms` or pass `--route local`.
- Local writes are slow or memory-heavy — lower `--chunk`; parquet file size, not RAM, is the routing signal.
- `.env` not found — `wrds_common.find_env_file()` walks up four directories from cwd, then `~/.env`. Put credentials in `~/.env` to share them across projects; a project `.env` overrides it.

## Cloud path

- **`Libref ... must be followed either by quoted string`** in a `qsas` log — broken `~/autoexec.sas`. This affects SAS jobs only; see `wrds-cloud-env.md`. Python `qsub` jobs are unaffected.
- `still queued after 1800s` — institutional queue congestion, not an error. Check later with `status`; do not resubmit blindly, duplicates write to the same `out/`.
- Job disappears from `qstat` but `job.log` is missing — the job may have failed before start. Run `qstat -j <id>` and check `sge_o_workdir`.
- `remote out/ not found` on `pull` — the job is still running, or it never wrote output. `pull` already retries for 120 s because SGE drops the job from `qstat` a few seconds before files land; beyond that, check `status` for the log tail.
- Job written somewhere unexpected — the job root is `$HOME/wrds_jobs` on the cloud host. Pin it with `--remote-root /path/wrds_jobs` or `WRDS_CLOUD_ROOT`.
- `No space left on device` on submit — `/scratch` is full or the account has no quota there. Job code and logs already live in `$HOME/wrds_jobs`; rerun without `--scratch-dir`, or point it at a directory with space.
- `mkdir: cannot create directory` / permission denied on submit — the configured root is not writable. Override it with `WRDS_CLOUD_ROOT`; all remote paths derive from that value.
- `outputs are X, above the pull limit` — intentional. The manifest is already downloaded; call `pull --tables a,b` for the tables an analysis step actually needs.
- Script mode ignores your file — `--script` uploads it as `user_script.py` and runs it instead of the generic extractor. It must write into `out/`.

## Housekeeping

- Scratch is shared across the whole institution. `run_cloud.py clean --job <job>` after a successful pull; delete anything older than the current project phase.
- `data/<job>/cloud_run.json` is the only link to a remote directory. Keep it until cleanup.
- Never commit `data/` or `.env`; parquet outputs are reproducible from the manifest SQL.
