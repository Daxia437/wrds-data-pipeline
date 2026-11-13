# Install

`wrds-data-pipeline` is a plain folder: `SKILL.md`, `scripts/`, `references/`, `assets/`. It calls Python from the shell and has no agent-specific API, so the same copy works across agents. Only the location differs.

## Prerequisites

- Python 3.10+, `git`, and an SSH-capable shell (needed only for cloud jobs).
- Packages: `pip install wrds pandas pyarrow paramiko`
- WRDS credentials in a `.env` at the **project** root, never in the skill folder:

```dotenv
WRDS_USER=your_wrds_username
WRDS_PASS=your_password
```

The scripts search `.env` from the current directory upward, then `~/.env`.

## WorkBuddy / Trae

```bash
git clone https://github.com/Daxia437/wrds-data-pipeline.git \
  ~/.trae-cn/skills/wrds-data-pipeline     # CN build
# or
  ~/.trae/skills/wrds-data-pipeline        # international build
```

Project-scoped instead: `<project>/.trae/skills/wrds-data-pipeline/`. The skill is picked up automatically on the next session.

## Claude Code

```bash
git clone https://github.com/Daxia437/wrds-data-pipeline.git \
  ~/.claude/skills/wrds-data-pipeline
```

Project-scoped instead: `<project>/.claude/skills/wrds-data-pipeline/`. `SKILL.md` already carries the `name` and `description` frontmatter Claude Code reads for activation, so no extra configuration is required.

## Codex, Cursor, and other `AGENTS.md` agents

These read project instructions rather than a global skill directory. Copy [`AGENTS.md`](AGENTS.md) to the project root, or add one line to your existing `AGENTS.md`:

```markdown
For WRDS data work, read <path-to-repo>/SKILL.md and follow it.
```

Then either clone the repo beside the project or reference it in place.

## ChatGPT

ChatGPT has no filesystem, so the skill runs in two halves:

1. Paste [`platforms/chatgpt-instructions.md`](platforms/chatgpt-instructions.md) into a Project's instructions or a custom GPT. That carries the routing thresholds, spec schema and output contract, so ChatGPT writes specs and commands correctly.
2. Execute the commands yourself in a local shell (or a Code Interpreter session that has the repo and your credentials). ChatGPT cannot SSH to WRDS Cloud or read your `.env`.

Upload `references/` to the Project's knowledge if you want it to answer questions about WRDS Cloud quotas, permission gaps and the manifest schema.

## Verify

Run once per machine, from the project root:

```bash
python ~/.claude/skills/wrds-data-pipeline/scripts/check_env.py
```

It checks the local PostgreSQL connection, the cloud SSH login, the cloud Python stack and the cloud `~/.pgpass`.

## Running from outside the skill directory

Always keep the **project** as the working directory and call the scripts by absolute path:

```bash
python ~/.claude/skills/wrds-data-pipeline/scripts/plan.py --spec specs/q4.json
python ~/.claude/skills/wrds-data-pipeline/scripts/run_local.py --spec specs/q4.json
```

`data/<job>/` and `.env` resolve against the project directory, not the skill directory. Copy `assets/spec.example.json` into your project as a starting point.

## Updating

```bash
cd ~/.claude/skills/wrds-data-pipeline && git pull
```

Skill and project copies are independent; pull in each place you installed it.
