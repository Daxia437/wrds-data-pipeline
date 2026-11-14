# AGENTS.md — WRDS data work

This file is a pointer for agents that read project instructions (`AGENTS.md`) instead of a skill directory. Replace `<path-to-skill>` with the absolute path to the cloned repository.

```markdown
## WRDS data

When the task involves WRDS extracts or data from any WRDS library (CRSP, Compustat, CSMAR, IBES, Thomson, … — availability depends on the account):

1. Read `<path-to-skill>/SKILL.md` and follow it as the procedure.
2. Run its scripts by absolute path, keeping the analysis project as the working directory:
   `python <path-to-skill>/scripts/plan.py --spec spec.json`
3. Spec and manifest schemas: `<path-to-skill>/references/contracts.md`
4. Failures: `<path-to-skill>/references/troubleshooting.md`
5. Never write WRDS credentials into the repository; they belong in `~/.env`, with an optional project-level `.env` override.
```
