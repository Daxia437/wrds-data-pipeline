#!/usr/bin/env python3
"""One-time access check for both WRDS paths.

Usage:
    python scripts/check_env.py
"""

from __future__ import annotations

import sys

import wrds_common as common

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} - {detail}")


def check_local_db() -> None:
    try:
        db = common.connect()
    except Exception as exc:
        record("local postgres", False, str(exc).splitlines()[0][:160])
        return
    try:
        libs = db.list_libraries()
        record("local postgres", True, f"{len(libs)} libraries in WRDS access metadata")
        user, _ = common.credentials()
        try:
            inventory = common.ensure_inventory(db, user)
            counts = inventory.get("status_counts", {})
            record(
                "subscription snapshot",
                True,
                f"{counts.get('ok', 0)} authorized libraries, "
                f"built {common.inventory_age_days(inventory):.0f} days ago, "
                f"refreshes every {common.INVENTORY_TTL_DAYS} days -> {inventory['_path']}",
            )
        except Exception as exc:
            record("subscription snapshot", False, str(exc).splitlines()[0][:160])
    finally:
        db.close()


def check_cloud() -> None:
    try:
        import paramiko
    except ImportError:
        record("cloud ssh", False, "paramiko not installed (pip install paramiko)")
        return

    user, password = common.credentials()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(common_host(), username=user, password=password, timeout=60)
    except Exception as exc:
        record("cloud ssh", False, str(exc).splitlines()[0][:160])
        return

    try:
        def run(cmd: str) -> str:
            _, out, err = client.exec_command(cmd)
            return (out.read().decode() + err.read().decode()).strip()

        record("cloud ssh", True, run("hostname"))
        stack = run(
            "python3 -c \"import pandas, pyarrow, psycopg2, wrds; "
            "print(pandas.__version__, pyarrow.__version__)\" 2>&1 | tail -1"
        )
        record("cloud python stack", "Error" not in stack and stack != "", stack)

        pgpass = run("test -f ~/.pgpass && echo present || echo missing")
        record("cloud ~/.pgpass", pgpass == "present", pgpass)
        if pgpass != "present":
            print("      -> run_cloud.py submit creates ~/.pgpass automatically")

        quota = run("quota 2>/dev/null | grep -E 'Home|Scratch'")
        if quota:
            print("      quota: " + quota.replace("\n", " | "))
        record("cloud scheduler", run("which qsub qsas") != "", "qsub available")
    finally:
        client.close()


def common_host() -> str:
    return "wrds-cloud.wharton.upenn.edu"


def main() -> None:
    print(f".env: {common.find_env_file()}\n")
    check_local_db()
    check_cloud()
    failed = [name for name, ok, _ in CHECKS if not ok and name != "cloud ~/.pgpass"]
    print()
    if failed:
        print("blocked: " + ", ".join(failed))
        sys.exit(1)
    print("both paths are usable")


if __name__ == "__main__":
    main()
