# WRDS Cloud environment (verified 2026-11-12)

Probed live against `<wrds_user>@wrds-cloud.wharton.upenn.edu`.

## Host and scheduler

| item | value |
|---|---|
| login host | `wrds-cloud.wharton.upenn.edu` (password auth, paramiko works) |
| scheduler | SGE 8.12 (`AGE 2026.1.0`); `qsub`, `qstat`, `qsas` in `/gridware/sge/bin/lx-amd64` |
| login node | 64 cores, 187 GB RAM, `ulimit -v` ≈ 4 GB per process |
| home | `$HOME` (typically 10 GB quota) — resolve it with `sftp.normalize(".")`, never hard-code |
| scratch | site-specific: `/scratch/<user>` or `/scratch/<institution>/<user>`; 500 GB at the verified site, shared with the whole institution |
| postgres host | `wrds-pgdata.wharton.upenn.edu:9737`, db `wrds`, resolves internally to 192.168.9.200 |

Scratch layout is **not** uniform across institutions, and it can be full or unavailable at any moment. `run_cloud.py` therefore keeps jobs in the home directory and uses scratch only when asked:

1. `--remote-root` or `WRDS_CLOUD_ROOT`, when set;
2. otherwise `$HOME/wrds_jobs` on the cloud host.

Code, logs, specs and `out/manifest.json` stay under that job root. Large parquet outputs go to `<scratch_root>/<job>/out` only when `--scratch-dir` or `WRDS_CLOUD_SCRATCH` is set; at the verified site that root is `/scratch/uiccn/<user>/wrds_jobs`. Set `WRDS_CLOUD_ROOT` only when home is unsuitable.

Default job resources come from site policy: `m_mem_free=8G`. Pass `--mem` to change it; do not exceed what the queue will grant.

## Python stack (already installed, no pip needed)

Python 3.14.5 at `/usr/local/bin/python3` with pandas 2.2.3, numpy 2.4.6, SQLAlchemy 2.0.50, psycopg2 2.9.12, pyarrow 24.0.0, and the `wrds` package. Outbound pip installs are unnecessary for this workflow; assume the network is restricted beyond the internal postgres host.

## Credentials on the cloud host

There is no `~/.pgpass` by default. `run_cloud.py submit` writes one (mode 600) from `.env`:

```
wrds-pgdata.wharton.upenn.edu:9737:wrds:<user>:<password>
```

With that file in place, `wrds.Connection()` works with no arguments, so uploaded scripts never contain secrets.

## Observed job behaviour

- `qsub job.sh` returns `Your job <id> ("<name>") has been submitted`; the id is what you poll.
- **SGE truncates job names to 10 characters in `qstat`** — never poll by name, poll by id.
- Queue wait observed: 15 s to ~4 min; compute for a 48K-row extract: 4.5 s.
- `qacct` returns `job id not found` for recent jobs; use `qstat` plus `job.log` instead.
- The job log captures stdout and stderr together (`-j y`), ending with the wrapper's `EXIT_CODE=<n>`.

## autoexec.sas (only relevant for SAS jobs)

Every SAS grid session runs `~/autoexec.sas`, which assigns all WRDS librefs. A malformed final line breaks **every** `qsas` job with a misleading `Libref ... must be followed either by quoted string` error. It was found and fixed on 2026-11-12:

```sas
libname scratch '<scratch-root>/<user>';   /* site-specific; probe it, do not copy this line */
```

A backup sits at `~/autoexec.sas.bak`. Python jobs are unaffected by this file.

## Python vs SAS on the grid

Prefer Python grid jobs: same scheduler, no macro library, no `autoexec` dependency, and pyarrow/parquet output is far smaller than `.sas7bdat`. Use `qsas` only when a CRSP macro (for example `%crspmerge`) is genuinely required.
