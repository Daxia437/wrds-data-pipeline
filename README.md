# WRDS Data Pipeline Skill

An agent skill that turns a WRDS data request into either a **local parquet extract** or a **WRDS Cloud job**, choosing automatically from the size of the result. It is table-agnostic: it works with whatever libraries your institution subscribes to, and ships a discovery script so the agent checks access instead of assuming CRSP or Compustat is there. Works with WorkBuddy, Trae, Claude Code, Codex, Cursor and any agent that can read a skill folder or an `AGENTS.md`.

## Install with your agent

Paste this into your agent. It points the agent at the platform-specific install guide, then verifies the setup:

```text
Install the skill at https://github.com/Daxia437/wrds-data-pipeline

Read INSTALL.md and follow the installation method that matches your agent and platform — a skills directory, an AGENTS.md pointer, or pasted instructions, whichever applies. Keep the folder name wrds-data-pipeline, then run scripts/check_env.py once from my project root and report what passes. WRDS requires a Duo push on every login: ask me to approve it and never retry the login automatically. Do not create or print any credentials.
```

Installation differs by agent: some read a skill folder, some read an `AGENTS.md` pointer, and ChatGPT (which has no filesystem) uses [`platforms/chatgpt-instructions.md`](platforms/chatgpt-instructions.md) pasted into a Project or custom GPT. The agent picks whichever method applies; the full per-platform steps are in [`INSTALL.md`](INSTALL.md). Start a new session after installing so the skill is picked up.

### Then give it credentials

The skill never asks for a password in conversation. Create `.env` in your **home directory** once — every project and agent app under the same OS user then shares it, on Windows, macOS and Linux:

```dotenv
WRDS_USER=your_wrds_username
WRDS_PASS=your_password
```

A `.env` in a project directory overrides the home one, which is useful for a second account or a temporary switch. Keep the file out of shared and synced folders, and restrict its permissions (`chmod 600` on macOS/Linux).

Python side needs `pip install wrds pandas pyarrow paramiko`. The agent can install those, but not the `.env`.

### Keep the account working: Duo and lockouts

WRDS sends a **Duo push on every API and SSH login**, PostgreSQL included. The skill cannot approve it — you do, on your phone. Ten unanswered pushes lock the account, and a locked account fails exactly like a wrong password (`PAM authentication failed`, or `Authentication failed` over SSH), so check Duo before editing `.env`.

What to do every session:

1. Log in at [wrds-www.wharton.upenn.edu](https://wrds-www.wharton.upenn.edu/) in a browser first and approve the push.
2. Run the skill from the same network as that browser session. Do not switch Wi-Fi or turn a VPN on/off in between.
3. Stay near your phone. Never leave an agent retrying logins unattended — a retry loop is what triggers the 10-attempt lockout. `check_env.py` opens several connections, so run it once, not in a loop.
4. If no push arrives, open the Duo app manually and wait before retrying once.

**Prefer the cloud path for anything beyond a quick lookup.** One SSH login costs one push; the job then reaches PostgreSQL through the cloud `~/.pgpass`, so submission, queue wait and result pull all happen inside that single session (`submit --wait`). The local PostgreSQL route pushes a new Duo prompt for every new connection.

If the account is already locked (`Duo Disabled`), ask WRDS Support to reset it. Retrying before the reset only lengthens the lockout.

## Requirements

- A valid **WRDS account** with your institution's subscription, Duo enrolled (every login needs an approved push), and permission for the databases your project targets. The skill is free; WRDS access is not part of this repo — WRDS Cloud is included in institutional subscriptions, and what you can reach is whatever your account can reach.
- Python 3.10+, `git`, an SSH-capable shell (cloud jobs only), and `pip install wrds pandas pyarrow paramiko`.

## Example: end-to-end research task

[`examples/us_st_reversal/`](examples/us_st_reversal/) runs the classic short-term reversal question through the whole pipeline: a cloud spec that aggregates `crsp.dsf` into a monthly panel, and a local analysis script that sorts deciles, forms D1−D10, and reports a time-series t-test. Reproducing it takes two commands. The spec pins `route: cloud` (a ten-year panel), so it exercises the cloud path — submission, queue wait, pull and the manifest contract — rather than automatic routing.

## Why this skill exists

Empirical research keeps hitting the same loop: pull data from whatever WRDS library the project needs, use it, repeat with a different window or filter. Hand-rolled scripts break in the same three places:

- A "quick" query returns 6 GB and chokes a laptop, so it has to be rewritten as a cloud job with SAS upload/download plumbing.
- A cloud job sits in the SGE queue for minutes, which is the wrong tool for a 40 MB annual file.
- Every script invents its own folder layout, column order and manifest, so nobody can tell which extract produced which result.

The skill removes that decision from whoever writes the extraction: it sizes the result, picks the path, and guarantees the same output contract either way. It does not carry a fixed list of approved datasets — access is a property of the account, not of the skill, and it is checked at run time.

## What it does

1. **Sizes the extract** — bounded `count(*)`, catalog-statistics fallback, and a 1,000-row sample to estimate compressed parquet bytes.
2. **Routes it** — under 250 MB and 2.5M rows summed across the whole job stays local; anything larger, or unmeasurable, goes to the cloud.
3. **Runs it** — writes parquet locally, or uploads a spec plus extractor to WRDS Cloud and submits an SGE job.
4. **Hands off reproducibly** — both paths emit `manifest.json` (rows, bytes, columns, dtypes, sha256, exact SQL) and `provenance.json`.

Measured on this account: one quarter of CRSP daily (`permno, date, prc, ret, vol, shrout`) is ~620K rows / ~10 MB parquet, comfortably local; a full year is ~2.4M rows / ~39 MB, just under the local limits. Full CRSP history is not measurable from catalog statistics (a view), so it routes to the cloud.

## Check what your account can reach

Subscriptions differ by university. The skill distinguishes WRDS's access-aware library list from raw catalog metadata: `list_libraries()` is the library-level authorization signal, while an individual table still gets a live check because a view can point to a missing or denied underlying relation.

On the **first connection**, the skill reads WRDS's own `list_libraries()` authorization list, excludes sample/trial libraries, and caches each library as `ok`. It does **not** query tables to build this snapshot. WRDS's list applies schema `USAGE` privileges and resolves friendly views to their dependent product relations; those views check `SELECT` on the underlying tables. This avoids the false positives from checking only an outer view's ACL. The snapshot refreshes automatically after **90 days**, so subscription changes are picked up without a hard-coded product list. Failures learned during use (a specific table that turns out not to be queryable) are recorded and surfaced on the next run, and survive snapshot rebuilds.

```bash
python <skill>/scripts/discover.py                             # authorized libraries + status counts
python <skill>/scripts/discover.py --refresh                   # rebuild now
python <skill>/scripts/discover.py --libraries                 # every authorized library
python <skill>/scripts/discover.py --gaps                      # tables that failed at run time
python <skill>/scripts/discover.py --schema ibes               # tables in a library
python <skill>/scripts/discover.py --table comp.funda          # columns + live access probe
python <skill>/scripts/discover.py --grep linktable            # find link tables by name
```

The library snapshot is a coverage signal, not a guarantee that every table in an `ok` library works: `ok` means WRDS included the library in this account's access-aware list, not that a table query succeeded. Run `--table` before relying on a specific table. It prints `access: queryable` or the actual failure, e.g. `permission denied for schema crsp_a_ccm` for the CCM link table. Ask in plain language — "what IBES data do we have?" — and the agent runs this before writing SQL. `plan.py` reuses the snapshot, so a spec that targets a library outside the authorization list, or a table with a known gap, gets a warning before the size estimate.

The cache defaults to `Path.home() / ".wrds_inventory.json"` (that is, `~/.wrds_inventory.json`). Projects and agent apps running under the same OS user therefore share it automatically on Windows, macOS and Linux — no hard-coded absolute path, environment variable or per-session setup is needed. The project directory is only a fallback when the home directory is not writable. `WRDS_INVENTORY` is an optional override for sandboxes and other special setups, and a value such as `~/.wrds_inventory.json` is expanded portably. The snapshot is per WRDS user; switching accounts triggers a rebuild. Different OS users and different machines do not share it automatically.

## Use it

Just ask, in whatever words fit the task:

- "Pull CRSP daily returns for 2024 Q4 and Compustat fundamentals for FY2023 into `data/`."
- "I'm replicating Table 2 of this paper — get me the monthly CRSP panel, 1990–2023."
- "Do we have IBES analyst coverage, and which tables are actually queryable?"

The skill picks the route, writes the spec, runs it and reports the manifest. The CLI is available when you want to drive it yourself:

```bash
python <skill>/scripts/discover.py --table <schema>.<table>
python <skill>/scripts/plan.py --spec spec.json
python <skill>/scripts/run_local.py --spec spec.json
python <skill>/scripts/run_cloud.py submit --spec spec.json --wait
python <skill>/scripts/run_cloud.py status --job <job>
python <skill>/scripts/run_cloud.py pull --job <job> --tables a,b
python <skill>/scripts/run_cloud.py clean --job <job>
```

Keep your **project** as the working directory and call the scripts by absolute path — `data/<job>/` and `.env` resolve against the project, not the skill folder. Spec format and worked examples: [`assets/spec.example.json`](assets/spec.example.json), [`references/contracts.md`](references/contracts.md).

## Output contract

```
data/<job>/
├── manifest.json      # rows, bytes, columns, dtypes, sha256, SQL per table
├── provenance.json    # spec, timestamps, route, WRDS user
└── <table>.parquet
```

Cloud runs keep the remote copy under `<remote_root>/<job>/` with `job.log`, `spec.json`, the uploaded script and `out/manifest.json`. `remote_root` defaults to `~/wrds_jobs` on the cloud host, so code, logs and results keep working when scratch is full or unprovisioned; pin it with `--remote-root` or `WRDS_CLOUD_ROOT`. Large parquet outputs can be redirected with `--scratch-dir /scratch/<institution>/<user>/wrds_jobs` (or `WRDS_CLOUD_SCRATCH`); metadata and logs always stay under `remote_root`.

Date-like columns arrive as `datetime64[ns]` on both routes, so a local and a cloud extract of the same query are identical in content.

Traceability is not the same as full reproducibility. The manifest and provenance record the exact SQL, columns, dtypes, row counts, SHA-256 and script fingerprint, so an extract can be traced to its source and byte-verified. Repeating a whole study additionally needs the analysis code, its environment and dependencies, and the random seed — the skill guarantees the data contract; the analysis code remains yours to version.

## Scope and known limits

- **No fixed dataset list.** Any library the account can query works: CRSP, Compustat (North America and Global), CSMAR, IBES, Thomson Reuters, board/exposure/audit products, WRDS apps. What differs between universities is the subscription, so `discover.py` is the source of truth rather than this README.
- The CCM link table (`crsp.ccmxpf_linktable`) requires a separate subscription that many accounts lack. When it is missing, bridge `permno` and `gvkey` yourself or use another link product.
- Some CSMAR tables (`trd_dalyr`, `trd_cndalym`, `trd_cale`) appear in metadata while `csmar_trade` is not provisioned — the exact trap `discover.py --table` exists to catch. Run it before trusting any new table.
- Data end dates follow the subscription: on the verified account CRSP stops at 2024-12-31, Compustat at 2026-09-30. Absence of recent rows is usually the subscription, not the query.
- Cloud queue waits of tens of seconds to several minutes are normal at the institutional level.
- WRDS only. Not for non-WRDS sources.

## Worked example

`examples/us_st_reversal/` is a complete end-to-end run: a `spec.json` that
submits a CRSP daily panel to WRDS Cloud, and `analyze.py` that turns the
pulled parquet into decile returns. See
[`examples/us_st_reversal/README.md`](examples/us_st_reversal/README.md).

## License

[MIT](LICENSE).

## Repository layout

```text
SKILL.md                    routing rules, commands, hard constraints
AGENTS.md                   pointer for AGENTS.md-aware agents
INSTALL.md                  per-platform installation
README.md                   motivation, capabilities, usage
platforms/
└── chatgpt-instructions.md paste-ready instructions for ChatGPT
scripts/
├── wrds_common.py          env loading, SQL assembly, date normalisation, manifests
├── discover.py             library authorization metadata + live table checks
├── check_env.py            one-time access check for both paths
├── plan.py                 size estimate and local/cloud decision
├── run_local.py            local execution to parquet
├── run_cloud.py            submit / status / pull / clean
└── run_remote.py           generic extractor executed on the cloud host
references/
├── contracts.md            spec, manifest and cloud layout schemas
├── routing-rules.md        thresholds, estimation mechanics, worked examples
├── wrds-cloud-env.md       verified cloud environment and scheduler behaviour
└── troubleshooting.md      permission gaps, queue issues, common failures
assets/
├── spec.example.json       small local extract
└── spec.cloud.example.json full-history cloud extract
examples/
└── us_st_reversal/         cloud spec + local analysis, end to end
```

## License

MIT — see [`LICENSE`](LICENSE). WRDS data itself remains governed by your institution's subscription agreement; the license covers this code only.
