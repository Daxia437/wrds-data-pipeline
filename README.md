# WRDS Data Pipeline Skill

An agent skill that turns a WRDS data requirement into either a **local parquet extract** or a **WRDS Cloud job**, choosing automatically based on the size of the result.

## Why this skill exists

Replication work keeps hitting the same loop: pull CRSP/Compustat/CSMAR data, get a usable file, repeat with a different window or filter. Hand-rolled scripts break in the same three places every time:

- A "quick" query returns 6 GB and chokes a laptop, so it has to be rewritten as a cloud job with SAS upload/download plumbing.
- The cloud job sits in the SGE queue for minutes, which makes it the wrong tool for a 40 MB annual file.
- Every script invents its own folder layout, column order and manifest, so downstream analysis cannot tell which extract produced which result.

This skill removes the decision from the person writing the extraction. It sizes the result, picks the path, and guarantees the same output contract either way.

## What it does

1. **Sizes the extract** — runs a bounded `count(*)`, falls back to catalog statistics, and samples 1,000 rows to estimate compressed parquet bytes.
2. **Routes it** — under 250 MB and 2.5M rows stays local; anything larger (or unmeasurable) goes to the cloud.
3. **Runs it** — writes parquet locally, or uploads a spec plus extractor to WRDS Cloud and submits an SGE job.
4. **Hands off reproducibly** — both paths emit `manifest.json` with rows, bytes, columns, dtypes, sha256 and the exact SQL, plus `provenance.json`.

### The routing rule

| Condition | Route |
|---|---|
| estimated download ≤ 250 MB and ≤ 2.5M rows | local |
| above either threshold, or row count unknown | cloud |
| multi-year / full-history panels, cross-table joins | cloud |
| iterative exploration, schema checks | local |

Measured on real data: one quarter of CRSP daily is ~620K rows / ~18 MB (local); full CRSP daily history is ~99.5M rows / ~1.5 GB (cloud, ~21–103 s of queue wait, seconds of compute).

Overrides exist for what the estimator cannot see: `plan.py --route local|cloud`, `"force_cloud": true` per table, `run_local.py --force`.

## Install

The skill is a plain folder — `SKILL.md` plus `scripts/` — with no agent-specific dependencies. Drop it wherever your agent looks for skills:

| Agent | Global | Project |
|---|---|---|
| WorkBuddy | `~/.workbuddy/skills/wrds-data-pipeline/` | `<project>/.workbuddy/skills/wrds-data-pipeline/` |
| Trae (CN) | `~/.trae-cn/skills/wrds-data-pipeline/` | `<project>/.trae/skills/wrds-data-pipeline/` |
| Trae (international) | `~/.trae/skills/wrds-data-pipeline/` | `<project>/.trae/skills/wrds-data-pipeline/` |
| Claude Code | `~/.claude/skills/wrds-data-pipeline/` | `<project>/.claude/skills/wrds-data-pipeline/` |
| Codex / Cursor / other `AGENTS.md` agents | point the agent at this repo | copy `AGENTS.md` into the project root |
| ChatGPT | no filesystem — paste [`platforms/chatgpt-instructions.md`](platforms/chatgpt-instructions.md) into a Project or custom GPT | same |

```bash
git clone https://github.com/Daxia437/wrds-data-pipeline.git ~/.workbuddy/skills/wrds-data-pipeline
```

Per-platform details, including how to run the commands when the skill lives outside your working directory: [`INSTALL.md`](INSTALL.md).

Once it is in place, ask for a WRDS extract, a replication dataset, or CRSP/Compustat/CSMAR data; the skill activates on its own. Commands can also be run directly, as below.

## Setup

Python 3.10+ with:

```bash
pip install wrds pandas pyarrow paramiko
```

Credentials go in a `.env` at the project root (never inside the skill):

```dotenv
WRDS_USER=your_wrds_username
WRDS_PASS=your_password
```

The scripts locate `.env` by walking up from the current directory, then falling back to `~/.env`. Nothing is ever printed, logged or committed; cloud jobs receive a `~/.pgpass` (mode 600) generated on the cloud host instead.

Verify both paths once per machine:

```bash
python scripts/check_env.py
```

## Usage

Write a spec — either raw SQL, or a table plus columns and filters:

```json
{
  "job": "crsp_dsf_2024_q4",
  "output_dir": "data",
  "tables": [
    {
      "name": "crsp_dsf",
      "sql": "select permno, date, prc, ret, vol, shrout from crsp.dsf where date between '2024-10-01' and '2024-12-31'"
    },
    {
      "name": "comp_funda",
      "library": "comp",
      "table": "funda",
      "columns": ["gvkey", "datadate", "fyear", "sale", "at"],
      "where": "indfmt='INDL' and datafmt='STD' and popsrc='D' and consol='C'"
    }
  ]
}
```

Let the router decide, then run:

> Installed globally? Keep your project as the working directory and call the scripts by absolute path, e.g. `python ~/.claude/skills/wrds-data-pipeline/scripts/plan.py --spec spec.json`. The `data/` folder and `.env` are resolved from the project directory, not the skill directory.

```bash
python scripts/plan.py --spec spec.json
# route: LOCAL  -> python scripts/run_local.py --spec spec.json
# route: CLOUD  -> python scripts/run_cloud.py submit --spec spec.json --wait
```

Cloud job lifecycle:

```bash
python scripts/run_cloud.py submit --spec spec.json --wait    # submit, wait, pull
python scripts/run_cloud.py status --job crsp_dsf_2024_q4     # queue state + log tail
python scripts/run_cloud.py pull --job crsp_dsf_2024_q4 --tables comp_funda
python scripts/run_cloud.py clean --job crsp_dsf_2024_q4
```

`pull` refuses to download more than `--max-mb` (default 500 MB) but still fetches the manifest, so analysis code can read the schema and then request only the tables it needs.

For work that cannot be expressed as SQL — weighting, CRSP macro merges, custom aggregation — pass a script:

```bash
python scripts/run_cloud.py submit --spec spec.json --script analysis/build_panel.py
```

The script runs with the job directory as cwd, may call `wrds.Connection()` with no arguments, must write into `out/`, and should emit `out/manifest.json` in the documented schema.

## Output contract

Both paths produce the same client-side layout, so analysis code never branches on route:

```
data/<job>/
├── manifest.json      # rows, bytes, columns, dtypes, sha256, SQL per table
├── provenance.json    # spec, timestamps, route, WRDS user
└── <table>.parquet
```

Cloud runs additionally keep a remote copy under `<remote_root>/<job>/` with `job.log` and `out/`, and a local `cloud_run.json` linking the two. `remote_root` is resolved on the cloud host at submit time — writable scratch if the site has it, otherwise `$HOME` — and can be pinned with `--remote-root` or `WRDS_CLOUD_ROOT`, so no institutional path is baked in.

Date-like string columns are converted to `datetime64` before writing, so downstream code does not have to guess.

## Scope and known limits

- Verified against CRSP, Compustat and CSMAR availability as of 2026-11. CRSP data on this account ends 2024-12-31; this reflects the subscription, not the query code.
- `crsp.ccmxpf_linktable` (CRSP–Compustat link) requires a separate CCM subscription. Without it, linking must be done manually on `permno` + `gvkey`.
- Some CSMAR tables (`trd_dalyr`, `trd_cndalym`, `trd_cale`) appear in metadata but their underlying schema is not provisioned. Run `select count(*)` before trusting any new table.
- Cloud queue waits of tens of seconds to several minutes are normal at the institutional level; `--wait` tolerates up to `--timeout` (default 1800 s).
- Not for non-WRDS sources.

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
