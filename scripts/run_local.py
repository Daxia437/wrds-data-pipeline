#!/usr/bin/env python3
"""Run an extract locally and write parquet + manifest.

Usage:
    python scripts/run_local.py --spec spec.json [--force] [--chunk 500000]
"""

from __future__ import annotations

import argparse
import platform
import sys

import pandas as pd

import wrds_common as common


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--force", action="store_true", help="ignore the size guard")
    parser.add_argument("--chunk", type=int, default=500_000)
    args = parser.parse_args()

    spec = common.load_spec(args.spec)
    out_dir = common.output_root(spec)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.force and spec.get("route") != "cloud":
        import subprocess

        plan = subprocess.run(
            [sys.executable, str(common.SCRIPTS_DIR / "plan.py"), "--spec", args.spec, "--json"],
            capture_output=True,
            text=True,
        )
        if plan.returncode == 0:
            import json

            route = json.loads(plan.stdout)["route"]
            if route == "cloud":
                common.fail(
                    "plan.py routed this job to the cloud; rerun with --force to keep it local"
                )

    db = common.connect()
    user, _ = common.credentials()
    manifest_tables = []

    try:
        for table in spec["tables"]:
            sql = common.build_sql(table)
            name = table["name"]
            print(f"[{name}] querying ...", flush=True)
            frame = db.raw_sql(sql, chunksize=args.chunk)
            frame = common.normalize_dates(frame)
            path = out_dir / f"{name}.parquet"
            frame.to_parquet(path, index=False, compression="snappy")
            manifest_tables.append(
                {
                    "name": name,
                    "file": path.name,
                    "rows": int(len(frame)),
                    "bytes": path.stat().st_size,
                    "sha256": common.sha256_file(path),
                    "columns": list(frame.columns),
                    "dtypes": {c: str(t) for c, t in frame.dtypes.items()},
                    "sql": sql,
                }
            )
            print(
                f"[{name}] {len(frame):,} rows -> {path.name} "
                f"({common.human_bytes(path.stat().st_size)})"
            )
    finally:
        db.close()

    created = common.utc_now()
    manifest = {
        "job": spec["job"],
        "route": "local",
        "created_utc": created,
        "wrds_user": user,
        "tables": manifest_tables,
    }
    provenance = {
        "created_utc": created,
        "route": "local",
        "wrds_user": user,
        "spec_path": str(args.spec),
        "spec": spec,
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "output_dir": str(out_dir),
    }
    common.write_json(out_dir / "manifest.json", manifest)
    common.write_json(out_dir / "provenance.json", provenance)
    print(f"\nmanifest: {out_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
