#!/usr/bin/env python3
"""Generic extractor executed on the WRDS Cloud host.

Reads spec.json from the working directory, runs every table query and writes
out/<table>.parquet plus out/manifest.json. Uses the cloud-side ~/.pgpass, so
no credentials appear in the uploaded files.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone


def build_sql(table: dict) -> str:
    if table.get("sql"):
        return table["sql"].strip().rstrip(";")
    columns = ", ".join(table.get("columns") or ["*"])
    sql = f"select {columns} from {table['library']}.{table['table']}"
    if table.get("where"):
        sql += f" where {table['where']}"
    if table.get("order_by"):
        sql += f" order by {table['order_by']}"
    return sql


DATE_HINTS = (
    "date",
    "_dt",
    "datadate",
    "accper",
    "trddt",
    "namedt",
    "nameenddt",
    "dlstdt",
    "caldt",
)


def normalize_dates(frame):
    import pandas as pd

    parsed_columns = {}
    for column in frame.columns:
        if str(frame[column].dtype) not in {"object", "string", "str"}:
            continue
        if not any(hint in str(column).lower() for hint in DATE_HINTS):
            continue
        parsed = pd.to_datetime(frame[column], errors="coerce")
        if parsed.notna().mean() >= 0.95:
            parsed_columns[column] = parsed.astype("datetime64[ns]")
    return frame.assign(**parsed_columns) if parsed_columns else frame


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    import pandas as pd
    import wrds

    spec = json.loads(open("spec.json", encoding="utf-8").read())
    meta_dir = "out"
    data_dir = os.environ.get("WRDS_DATA_DIR") or meta_dir
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = wrds.Connection(verbose=False)
    tables, errors = [], []

    for table in spec.get("tables") or []:
        name = table["name"]
        sql = build_sql(table)
        t0 = time.time()
        print(f"[{name}] querying ...", flush=True)
        try:
            frame = db.raw_sql(sql)
            frame = normalize_dates(frame)
            path = os.path.join(data_dir, f"{name}.parquet")
            frame.to_parquet(path, index=False, compression="snappy")
            tables.append(
                {
                    "name": name,
                    "file": f"{name}.parquet",
                    "rows": int(len(frame)),
                    "bytes": os.path.getsize(path),
                    "sha256": sha256_file(path),
                    "columns": list(frame.columns),
                    "dtypes": {c: str(t) for c, t in frame.dtypes.items()},
                    "sql": sql,
                    "elapsed_sec": round(time.time() - t0, 1),
                }
            )
            print(f"[{name}] {len(frame):,} rows -> {os.path.getsize(path)} bytes", flush=True)
        except Exception as exc:
            errors.append({"name": name, "sql": sql, "error": str(exc).splitlines()[0][:300]})
            print(f"[{name}] FAILED {exc}", flush=True)

    db.close()

    manifest = {
        "job": spec["job"],
        "route": "cloud",
        "created_utc": started,
        "wrds_user": os.environ.get("USER") or os.environ.get("LOGNAME", ""),
        "host": platform.node(),
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "tables": tables,
        "errors": errors,
    }
    with open(os.path.join(meta_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, default=str)

    print(f"RESULT tables={len(tables)} errors={len(errors)}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
