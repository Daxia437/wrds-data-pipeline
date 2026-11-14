# WRDS Data Pipeline Skill

An agent skill that turns a WRDS data request into either a **local parquet extract** or a **WRDS Cloud job**, choosing automatically from the size of the result. It is table-agnostic: it works with whatever libraries your institution subscribes to, and ships a discovery script so the agent checks access instead of assuming CRSP or Compustat is there. Works with WorkBuddy, Trae, Claude Code, Codex, Cursor and any agent that can read a skill folder or an `AGENTS.md`.

## Install with your agent

Paste this into your agent. It clones the repo into the right skills directory, then verifies the setup:

```text
Install the skill at https://github.com/Daxia437/wrds-data-pipeline

Clone it into your skills directory, keep the folder name wrds-data-pipeline, and confirm SKILL.md is readable there. Then run scripts/check_env.py from my project root and report what passes. Do not create or print any credentials.
```

If the agent asks where to put it:

| Agent | Skills directory |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/` |
| Trae | `~/.trae-cn/skills/` (CN) or `~/.trae/skills/` |
| Claude Code | `~/.claude/skills/` |
| Codex / Cursor | no skill dir — add `AGENTS.md` from this repo to the project root and point it at the clone |
| ChatGPT | no filesystem — paste [`platforms/chatgpt-instructions.md`](platforms/chatgpt-instructions.md) into a Project or custom GPT |

Start a new session after installing so the skill is picked up. Manual installation, per-platform detail and troubleshooting: [`INSTALL.md`](INSTALL.md).

### Then give it credentials

The skill never asks for a password in conversation. Create `.env` at your **project root** yourself:

```dotenv
WRDS_USER=your_wrds_username
WRDS_PASS=your_password
```

Python side needs `pip install wrds pandas pyarrow paramiko`. The agent can install those, but not the `.env`.

## Why this skill exists

Replication work keeps hitting the same loop: pull data from whatever WRDS library the paper uses, use it, repeat with a different window or filter. Hand-rolled scripts break in the same three places:

- A "quick" query returns 6 GB and chokes a laptop, so it has to be rewritten as a cloud job with SAS upload/download plumbing.
- A cloud job sits in the SGE queue for minutes, which is the wrong tool for a 40 MB annual file.
- Every script invents its own folder layout, column order and manifest, so nobody can tell which extract produced which result.

The skill removes that decision from whoever writes the extraction: it sizes the result, picks the path, and guarantees the same output contract either way. It does not carry a fixed list of approved datasets — access is a property of the account, not of the skill, and it is checked at run time.

## What it does

1. **Sizes the extract** — bounded `count(*)`, catalog-statistics fallback, and a 1,000-row sample to estimate compressed parquet bytes.
2. **Routes it** — under 250 MB and 2.5M rows stays local; anything larger, or unmeasurable, goes to the cloud.
3. **Runs it** — writes parquet locally, or uploads a spec plus extractor to WRDS Cloud and submits an SGE job.
4. **Hands off reproducibly** — both paths emit `manifest.json` (rows, bytes, columns, dtypes, sha256, exact SQL) and `provenance.json`.

Measured on real data: one quarter of CRSP daily is ~620K rows / ~18 MB (local); full CRSP history is ~99.5M rows / ~1.5 GB (cloud, 21–103 s of queue wait, seconds of compute).

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

Cloud runs also keep a remote copy under `<remote_root>/<job>/` with `job.log` and `out/`, plus a local `cloud_run.json` linking the two. `remote_root` is resolved on the cloud host at submit time — writable scratch if the site has it, otherwise `$HOME` — and can be pinned with `--remote-root` or `WRDS_CLOUD_ROOT`, so no institutional path is baked in.

Date-like columns arrive as `datetime64[ns]` on both routes, so a local and a cloud extract of the same query are identical in content.

## Scope and known limits

- **No fixed dataset list.** Any library the account can query works: CRSP, Compustat (North America and Global), CSMAR, IBES, Thomson Reuters, board/exposure/audit products, WRDS apps. What differs between universities is the subscription, so `discover.py` is the source of truth rather than this README.
- The CCM link table (`crsp.ccmxpf_linktable`) requires a separate subscription that many accounts lack. When it is missing, bridge `permno` and `gvkey` yourself or use another link product.
- Some CSMAR tables (`trd_dalyr`, `trd_cndalym`, `trd_cale`) appear in metadata while `csmar_trade` is not provisioned — the exact trap `discover.py --table` exists to catch. Run it before trusting any new table.
- Data end dates follow the subscription: on the verified account CRSP stops at 2024-12-31, Compustat at 2026-09-30. Absence of recent rows is usually the subscription, not the query.
- Cloud queue waits of tens of seconds to several minutes are normal at the institutional level.
- WRDS only. Not for non-WRDS sources.

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
```
