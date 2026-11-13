# AGENTS.md — WRDS data work

This repository is an agent skill for pulling WRDS data (CRSP, Compustat, CSMAR) into a project.

When the task involves WRDS extracts, replication datasets, or CRSP/Compustat/CSMAR data:

1. Read [`SKILL.md`](SKILL.md) and follow it as the procedure.
2. Use the scripts in `scripts/` by absolute path; keep the analysis project as the working directory.
3. Follow [`references/contracts.md`](references/contracts.md) for the spec and manifest schemas.
4. Look up failures in [`references/troubleshooting.md`](references/troubleshooting.md) before changing code.

Never write WRDS credentials into the repository. They belong in `.env` at the project root.
