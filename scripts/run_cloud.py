#!/usr/bin/env python3
"""Submit, inspect, pull and clean WRDS Cloud (SGE) jobs.

Usage:
    python scripts/run_cloud.py submit --spec spec.json [--script x.py] [--wait] [--mem 8G]
    python scripts/run_cloud.py status --job <job>
    python scripts/run_cloud.py pull --job <job> [--max-mb 500] [--tables a,b]
    python scripts/run_cloud.py clean --job <job>
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import wrds_common as common

HOST = "wrds-cloud.wharton.upenn.edu"
PG_HOST = "wrds-pgdata.wharton.upenn.edu"
PG_PORT = "9737"
PG_DB = "wrds"

JOB_TEMPLATE = """#!/bin/bash
#$ -N {name}
#$ -cwd
#$ -j y
#$ -o job.log
#$ -l h_vmem={mem}
cd {remote_dir}
export WRDS_DATA_DIR={data_dir}
python3 {entry}
echo "EXIT_CODE=$?" >> job.log
"""


# --------------------------------------------------------------------------- #
# ssh helpers
# --------------------------------------------------------------------------- #
DUO_PROMPT_HINTS = ("passcode", "option", "duo", "verification", "factor")


def _ki_answers(prompts, password: str) -> list[str]:
    """Answers for an SSH keyboard-interactive round.

    WRDS Cloud asks for the password first and then for a Duo option
    (``Passcode or option (1-1):``). Answering ``1`` triggers a Duo Push;
    echoing the password there would fail the login and burn an attempt.
    """
    answers = []
    for prompt, _echo in prompts:
        text = (prompt or "").lower()
        answers.append("1" if any(hint in text for hint in DUO_PROMPT_HINTS) else password)
    return answers


def _ssh():
    import paramiko

    user, password = common.credentials()
    client = common.ssh_client()
    try:
        client.connect(HOST, username=user, password=password, timeout=60)
    except paramiko.BadAuthenticationType as exc:
        if "keyboard-interactive" not in (exc.allowed_types or []):
            client.close()
            common.fail(f"SSH authentication failed for {HOST}: {exc}")
        try:
            client.get_transport().auth_interactive(
                user, lambda title, instructions, prompts: _ki_answers(prompts, password)
            )
        except Exception as inner:
            client.close()
            common.fail(
                "SSH authentication failed. WRDS Cloud requires Duo: approve the push on your "
                f"phone when it arrives and retry. Detail: {inner}"
            )
    except paramiko.BadHostKeyException as exc:
        client.close()
        common.fail(f"SSH host key check failed for {HOST}: {exc}\n{common.HOST_KEY_HINT}")
    except paramiko.AuthenticationException as exc:
        client.close()
        common.fail(
            f"SSH authentication failed for {HOST}. Check the password in .env, approve the Duo "
            f"push when it arrives, and retry once. A locked account (Duo Disabled) fails the "
            f"same way. Detail: {exc}"
        )
    except paramiko.SSHException as exc:
        client.close()
        text = str(exc).lower()
        if "known_hosts" in text or "host key" in text:
            common.fail(f"SSH host key check failed for {HOST}: {exc}\n{common.HOST_KEY_HINT}")
        raise
    return client


def _run(ssh, cmd: str) -> tuple[int, str]:
    _, stdout, stderr = ssh.exec_command(cmd)
    code = stdout.channel.recv_exit_status()
    return code, (stdout.read().decode() + stderr.read().decode()).strip()


def _remote_home(ssh) -> str:
    """Remote home directory, without assuming an institutional layout."""
    sftp = ssh.open_sftp()
    try:
        return sftp.normalize(".").rstrip("/")
    finally:
        sftp.close()


def _detect_remote_root(ssh) -> str:
    """Job root on the cloud host: always the home directory.

    Code, logs, specs and the manifest live under ``~/wrds_jobs`` so jobs keep
    working when scratch fills up or has no provisioned quota. Large parquet
    outputs can be redirected with ``--scratch-dir``. ``WRDS_CLOUD_ROOT`` or
    ``--remote-root`` overrides this.
    """
    return f"{_remote_home(ssh)}/wrds_jobs"


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _merge_pgpass(content: str, prefix: str, entry: str) -> tuple[str, bool]:
    """Upsert `entry` (the line starting with `prefix`) into the .pgpass text.
    Returns (new_content, replaced). Other hosts/databases/users are untouched."""
    lines = content.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = entry
            replaced = True
    if not replaced:
        lines.append(entry)
    return "".join(lines), replaced


def _ensure_pgpass(ssh) -> None:
    """Write the current user's .pgpass line, updating it in place when the
    password changes, without touching lines for other hosts, databases or
    users. The password never reaches the terminal or the job log."""
    user, password = common.credentials()
    remote_home = _remote_home(ssh)
    prefix = f"{PG_HOST}:{PG_PORT}:{PG_DB}:{user}:"
    entry = prefix + _pgpass_escape(password) + "\n"

    content = ""
    sftp = ssh.open_sftp()
    try:
        try:
            with sftp.open(f"{remote_home}/.pgpass", "r") as handle:
                content = handle.read().decode()
        except (IOError, OSError):
            content = ""
    finally:
        sftp.close()

    new_content, replaced = _merge_pgpass(content, prefix, entry)

    sftp = ssh.open_sftp()
    try:
        with sftp.open(f"{remote_home}/.pgpass", "w") as handle:
            handle.write(new_content.encode())
    finally:
        sftp.close()
    _run(ssh, f"chmod 600 {remote_home}/.pgpass")
    action = "updated" if replaced else "created"
    print(f"{action} ~/.pgpass for {user} on the cloud host (mode 600)")


def _state_path(job: str) -> Path:
    return Path.cwd() / "data" / job / "cloud_run.json"


def _load_state(job: str) -> dict:
    path = _state_path(job)
    if not path.is_file():
        common.fail(f"no cloud_run.json for job {job}; run submit first")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(state: dict) -> None:
    common.write_json(_state_path(state["job"]), state)


def _tail_log(ssh, remote_dir: str, lines: int = 40) -> str:
    _, text = _run(ssh, f"tail -{lines} {remote_dir}/job.log 2>/dev/null")
    return text


def _job_exit_code(ssh, remote_dir: str) -> int | None:
    _, text = _run(ssh, f"grep -oE 'EXIT_CODE=[0-9-]+' {remote_dir}/job.log 2>/dev/null | tail -1")
    text = text.strip()
    if not text:
        return None
    try:
        return int(text.rsplit("=", 1)[1])
    except ValueError:
        return None


def _classify_status(tables: list, errors: list, exit_code: int | None = None) -> str:
    """Classify a cloud job from its SGE exit code and manifest contents.

    - SUCCESS: exit code 0, at least one table output, no table errors.
    - PARTIAL_SUCCESS: at least one table landed, but some tables errored.
    - FAILED: non-zero exit code, no outputs, or unverifiable execution.
    """
    if exit_code == 0 and not errors and tables:
        return "SUCCESS"
    if errors and tables:
        return "PARTIAL_SUCCESS"
    return "FAILED"


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_submit(args) -> None:
    spec = common.load_spec(args.spec)
    job = re.sub(r"[^A-Za-z0-9_.-]", "_", spec["job"])
    user, _ = common.credentials()

    ssh = _ssh()
    try:
        _ensure_pgpass(ssh)
        root = args.remote_root or os.environ.get("WRDS_CLOUD_ROOT") or _detect_remote_root(ssh)
        remote_dir = f"{root.rstrip('/')}/{job}"
        scratch_root = args.scratch_dir or os.environ.get("WRDS_CLOUD_SCRATCH")
        remote_meta_dir = f"{remote_dir}/out"
        remote_data_dir = f"{scratch_root.rstrip('/')}/{job}/out" if scratch_root else remote_meta_dir
        _run(ssh, f"mkdir -p {remote_dir}")
        _run(ssh, f"rm -rf {remote_dir}/out {remote_dir}/job.log")
        if remote_data_dir != remote_meta_dir:
            _run(ssh, f"rm -rf {remote_data_dir}")
        sftp = ssh.open_sftp()
        try:
            sftp.putfo(
                io.BytesIO(json.dumps(spec, indent=2).encode()), f"{remote_dir}/spec.json"
            )
            script_path = args.script or spec.get("script")
            if script_path:
                script_file = Path(script_path).expanduser()
                if not script_file.is_file():
                    common.fail(f"script not found: {script_file}")
                sftp.put(str(script_file), f"{remote_dir}/user_script.py")
                entry = "user_script.py"
            elif spec.get("tables"):
                sftp.put(str(common.SCRIPTS_DIR / "run_remote.py"), f"{remote_dir}/run_remote.py")
                entry = "run_remote.py"
            else:
                common.fail("spec has no tables and no script; nothing to run")
            job_sh = JOB_TEMPLATE.format(
                name=job[:10],
                remote_dir=remote_dir,
                entry=entry,
                mem=args.mem,
                data_dir=remote_data_dir,
            )
            sftp.putfo(io.BytesIO(job_sh.encode()), f"{remote_dir}/job.sh")
        finally:
            sftp.close()

        code, out = _run(ssh, f"cd {remote_dir} && qsub job.sh")
        if code != 0:
            common.fail(f"qsub failed: {out}")
        match = re.search(r"job\s+(\d+)", out, re.I)
        job_id = match.group(1) if match else ""
        state = {
            "job": spec["job"],
            "job_id": job_id,
            "remote_dir": remote_dir,
            "remote_root": root,
            "remote_meta_dir": remote_meta_dir,
            "remote_data_dir": remote_data_dir,
            "entry": entry,
            "output_dir": spec.get("output_dir") or "data",
            "submitted_utc": common.utc_now(),
            "user": user,
        }
        _write_state(state)
        print(f"submitted job {job_id} -> {remote_dir}")
        print(f"state: {_state_path(spec['job'])}")

        if args.wait:
            _wait(ssh, state, timeout=args.timeout)
            _pull(ssh, state, max_mb=args.pull_max_mb, only=None)
    finally:
        ssh.close()


def _wait(ssh, state: dict, timeout: int) -> int | None:
    job_id = state["job_id"]
    print(f"waiting for job {job_id} (queue waits of minutes are normal) ...")
    started = time.time()
    while time.time() - started < timeout:
        _, qstat = _run(ssh, "qstat")
        if job_id not in qstat:
            print(f"job left the queue after {int(time.time() - started)}s")
            exit_code = _job_exit_code(ssh, state["remote_dir"])
            print(f"job exit code: {exit_code if exit_code is not None else 'unknown'}")
            print(_tail_log(ssh, state["remote_dir"], 25))
            return exit_code
        time.sleep(20)
    common.fail(f"job {job_id} still queued after {timeout}s; check with status later")


def cmd_status(args) -> None:
    state = _load_state(args.job)
    ssh = _ssh()
    try:
        _, qstat = _run(ssh, f"qstat -j {state['job_id']} 2>&1")
        running = "job_number:" in qstat
        print(f"job {state['job_id']} ({state['job']}): {'queued/running' if running else 'not in queue'}")
        print("--- job.log tail ---")
        print(_tail_log(ssh, state["remote_dir"], args.lines) or "(no log yet)")
    finally:
        ssh.close()


def cmd_pull(args) -> None:
    state = _load_state(args.job)
    only = [t.strip() for t in args.tables.split(",")] if args.tables else None
    ssh = _ssh()
    try:
        _pull(ssh, state, max_mb=args.max_mb, only=only)
    finally:
        ssh.close()


def _pull(ssh, state: dict, max_mb: float, only: list[str] | None) -> None:
    remote_meta_dir = state.get("remote_meta_dir") or f"{state['remote_dir']}/out"
    remote_data_dir = state.get("remote_data_dir") or remote_meta_dir
    local_dir = common.output_root(
        {"job": state["job"], "output_dir": state.get("output_dir")}
    )
    local_dir.mkdir(parents=True, exist_ok=True)
    sftp = ssh.open_sftp()
    try:
        # A job leaves the queue a few seconds before its files land on disk.
        deadline = time.time() + 120
        while True:
            try:
                entries = sftp.listdir_attr(remote_data_dir)
                break
            except IOError:
                if time.time() > deadline:
                    common.fail("remote output directory not found; the job may still be running")
                time.sleep(5)
        sizes = {entry.filename: entry.st_size for entry in entries}

        try:
            sftp.get(f"{remote_meta_dir}/manifest.json", str(local_dir / "manifest.json"))
        except IOError:
            common.fail("remote manifest.json missing; check the job log")
        manifest = json.loads((local_dir / "manifest.json").read_text(encoding="utf-8"))
        tables_meta = manifest.get("tables", [])
        errors = manifest.get("errors", [])
        exit_code = _job_exit_code(ssh, state["remote_dir"])
        status = _classify_status(tables_meta, errors, exit_code)
        print(
            f"job status: {status} "
            f"(exit {exit_code if exit_code is not None else 'unknown'}, "
            f"{len(tables_meta)} tables, {len(errors)} errors)"
        )
        for err in errors:
            print(f"  ! {err.get('name', '?')}: {err.get('error', '')[:120]}")

        wanted = [t for t in tables_meta if not only or t["name"] in only]
        total = sum(sizes.get(f"{t['name']}.parquet", 0) for t in wanted)
        limit = max_mb * 1024 * 1024

        if total > limit and not only:
            print(
                f"outputs are {common.human_bytes(total)}, above the {max_mb:g}MB pull limit.\n"
                f"manifest downloaded; rerun with --tables {','.join(t['name'] for t in wanted[:2])} "
                "or raise --max-mb"
            )
        else:
            for table in wanted:
                filename = f"{table['name']}.parquet"
                if filename not in sizes:
                    print(f"missing on remote: {filename}")
                    continue
                sftp.get(f"{remote_data_dir}/{filename}", str(local_dir / filename))
                print(f"pulled {filename} ({common.human_bytes(sizes[filename])})")
    finally:
        sftp.close()

    provenance = {
        "created_utc": common.utc_now(),
        "route": "cloud",
        "job": state["job"],
        "job_id": state.get("job_id"),
        "remote_dir": state.get("remote_dir"),
        "remote_meta_dir": remote_meta_dir,
        "remote_data_dir": remote_data_dir,
        "submitted_utc": state.get("submitted_utc"),
        "output_dir": str(local_dir),
        "status": status,
        "exit_code": exit_code,
        "errors": errors,
    }
    common.write_json(local_dir / "provenance.json", provenance)
    print(f"manifest: {local_dir / 'manifest.json'}")


def cmd_clean(args) -> None:
    ssh = _ssh()
    try:
        state_path = _state_path(args.job)
        remote_data_dir = None
        if state_path.is_file():
            state = _load_state(args.job)
            remote_dir = state["remote_dir"]
            remote_data_dir = state.get("remote_data_dir")
        else:
            root = args.remote_root or os.environ.get("WRDS_CLOUD_ROOT") or _detect_remote_root(ssh)
            remote_dir = f"{root.rstrip('/')}/{args.job}"
            print("cloud_run.json missing, probing the remote job root")
        _run(ssh, f"rm -rf {remote_dir}")
        print(f"removed {remote_dir}")
        if remote_data_dir and remote_data_dir != f"{remote_dir}/out":
            _run(ssh, f"rm -rf {remote_data_dir}")
            print(f"removed {remote_data_dir}")
    finally:
        ssh.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit")
    submit.add_argument("--spec", required=True)
    submit.add_argument("--script", help="custom python script to run on the cloud host")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--timeout", type=int, default=1800)
    submit.add_argument("--mem", default="8G")
    submit.add_argument("--pull-max-mb", type=float, default=500)
    submit.add_argument("--remote-root", help="job root for code and logs; defaults to ~/wrds_jobs")
    submit.add_argument(
        "--scratch-dir",
        help="redirect large parquet outputs to this scratch root; metadata stays in --remote-root",
    )
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status")
    status.add_argument("--job", required=True)
    status.add_argument("--lines", type=int, default=40)
    status.set_defaults(func=cmd_status)

    pull = sub.add_parser("pull")
    pull.add_argument("--job", required=True)
    pull.add_argument("--max-mb", type=float, default=500)
    pull.add_argument("--tables", help="comma separated table names")
    pull.set_defaults(func=cmd_pull)

    clean = sub.add_parser("clean")
    clean.add_argument("--job", required=True)
    clean.add_argument("--remote-root", help="job root for code and logs; defaults to ~/wrds_jobs")
    clean.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
