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
PGPASS = "wrds-pgdata.wharton.upenn.edu:9737:wrds:{user}:{password}\n"

JOB_TEMPLATE = """#!/bin/bash
#$ -N {name}
#$ -cwd
#$ -j y
#$ -o job.log
#$ -l h_vmem={mem}
cd {remote_dir}
python3 {entry}
echo "EXIT_CODE=$?" >> job.log
"""


# --------------------------------------------------------------------------- #
# ssh helpers
# --------------------------------------------------------------------------- #
def _ssh():
    import paramiko

    user, password = common.credentials()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=user, password=password, timeout=60)
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
    """Job root on the cloud host: scratch when writable, else the home directory.

    WRDS sites arrange scratch differently (``/scratch/<user>``,
    ``/scratch/<institution>/<user>``, or no scratch at all), so probe instead of
    hard-coding.  ``WRDS_CLOUD_ROOT`` or ``--remote-root`` overrides this.
    """
    probe = (
        'if [ -d "/scratch/$USER" ] && [ -w "/scratch/$USER" ]; then echo "/scratch/$USER"; '
        'elif ls -d /scratch/*/"$USER" >/dev/null 2>&1; then ls -d /scratch/*/"$USER" | head -n 1; '
        'elif [ -d "/scratch/$USER" ]; then echo "/scratch/$USER"; '
        'else echo "$HOME"; fi'
    )
    _, out = _run(ssh, probe)
    base = next((line.strip() for line in out.splitlines()[::-1] if line.strip()), "")
    if not base:
        base = _remote_home(ssh)
    return f"{base.rstrip('/')}/wrds_jobs"


def _ensure_pgpass(ssh) -> None:
    user, password = common.credentials()
    _, exists = _run(ssh, "test -f ~/.pgpass && echo ok")
    if exists.strip().endswith("ok"):
        return
    sftp = ssh.open_sftp()
    try:
        with sftp.open(f"{_remote_home(ssh)}/.pgpass", "w") as handle:
            handle.write(PGPASS.format(user=user, password=password).encode())
    finally:
        sftp.close()
    _run(ssh, "chmod 600 ~/.pgpass")
    print("created ~/.pgpass on the cloud host (mode 600)")


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
        _run(ssh, f"mkdir -p {remote_dir}")
        _run(ssh, f"rm -rf {remote_dir}/out {remote_dir}/job.log")
        sftp = ssh.open_sftp()
        try:
            sftp.putfo(
                io.BytesIO(json.dumps(spec, indent=2).encode()), f"{remote_dir}/spec.json"
            )
            if args.script:
                sftp.put(args.script, f"{remote_dir}/user_script.py")
                entry = "user_script.py"
            else:
                sftp.put(str(common.SCRIPTS_DIR / "run_remote.py"), f"{remote_dir}/run_remote.py")
                entry = "run_remote.py"
            job_sh = JOB_TEMPLATE.format(
                name=job[:10], remote_dir=remote_dir, entry=entry, mem=args.mem
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


def _wait(ssh, state: dict, timeout: int) -> None:
    job_id = state["job_id"]
    print(f"waiting for job {job_id} (queue waits of minutes are normal) ...")
    started = time.time()
    while time.time() - started < timeout:
        _, qstat = _run(ssh, "qstat")
        if job_id not in qstat:
            print(f"job left the queue after {int(time.time() - started)}s")
            print(_tail_log(ssh, state["remote_dir"], 25))
            return
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
    remote_dir = state["remote_dir"]
    local_dir = common.output_root(
        {"job": state["job"], "output_dir": state.get("output_dir")}
    )
    local_dir.mkdir(parents=True, exist_ok=True)
    sftp = ssh.open_sftp()
    try:
        # A job leaves the queue a few seconds before its files land on scratch.
        deadline = time.time() + 120
        while True:
            try:
                entries = sftp.listdir_attr(f"{remote_dir}/out")
                break
            except IOError:
                if time.time() > deadline:
                    common.fail("remote out/ not found; the job may still be running")
                time.sleep(5)
        sizes = {entry.filename: entry.st_size for entry in entries}

        if "manifest.json" not in sizes:
            common.fail("remote manifest.json missing; check the job log")
        sftp.get(f"{remote_dir}/out/manifest.json", str(local_dir / "manifest.json"))
        manifest = json.loads((local_dir / "manifest.json").read_text(encoding="utf-8"))

        wanted = [t for t in manifest.get("tables", []) if not only or t["name"] in only]
        total = sum(sizes.get(f"{t['name']}.parquet", 0) for t in wanted)
        limit = max_mb * 1024 * 1024

        if total > limit and not only:
            print(
                f"outputs are {common.human_bytes(total)}, above the {max_mb:g}MB pull limit.\n"
                f"manifest downloaded; rerun with --tables {','.join(t['name'] for t in wanted[:2])} "
                "or raise --max-mb"
            )
            return

        for table in wanted:
            filename = f"{table['name']}.parquet"
            if filename not in sizes:
                print(f"missing on remote: {filename}")
                continue
            sftp.get(f"{remote_dir}/out/{filename}", str(local_dir / filename))
            print(f"pulled {filename} ({common.human_bytes(sizes[filename])})")
    finally:
        sftp.close()

    provenance = {
        "created_utc": common.utc_now(),
        "route": "cloud",
        "job": state["job"],
        "job_id": state.get("job_id"),
        "remote_dir": remote_dir,
        "submitted_utc": state.get("submitted_utc"),
        "output_dir": str(local_dir),
    }
    common.write_json(local_dir / "provenance.json", provenance)
    print(f"manifest: {local_dir / 'manifest.json'}")


def cmd_clean(args) -> None:
    ssh = _ssh()
    try:
        state_path = _state_path(args.job)
        if state_path.is_file():
            remote_dir = _load_state(args.job)["remote_dir"]
        else:
            root = args.remote_root or os.environ.get("WRDS_CLOUD_ROOT") or _detect_remote_root(ssh)
            remote_dir = f"{root.rstrip('/')}/{args.job}"
            print("cloud_run.json missing, probing the remote job root")
        _run(ssh, f"rm -rf {remote_dir}")
        print(f"removed {remote_dir}")
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
    submit.add_argument("--remote-root", help="job root on the cloud host; default is probed")
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
    clean.add_argument("--remote-root", help="job root on the cloud host; default is probed")
    clean.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
