#!/usr/bin/env python3
"""Estimate extract size and choose between the local and cloud path.

Usage:
    python scripts/plan.py --spec spec.json [--route auto|local|cloud]
                           [--max-local-mb 250] [--max-local-rows 2500000] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys

import wrds_common as common

WRDS_HOST = "wrds-pgdata.wharton.upenn.edu"
WRDS_PORT = 9737
WRDS_DB = "wrds"


def _connect_psycopg2():
    import psycopg2

    user, password = common.credentials()
    conn = psycopg2.connect(
        host=WRDS_HOST, port=WRDS_PORT, dbname=WRDS_DB, user=user, password=password
    )
    conn.autocommit = True
    return conn


def _count_rows(conn, sql: str, timeout_ms: int) -> tuple[int | None, str]:
    """Exact row count, or None when the statement exceeds the timeout."""
    cur = conn.cursor()
    try:
        cur.execute("set statement_timeout = %s", (timeout_ms,))
        cur.execute(f"select count(*) from ({sql}) q")
        return int(cur.fetchone()[0]), "count"
    except Exception as exc:  # timeout or unsupported construct
        conn.rollback()
        return None, f"count failed ({str(exc).splitlines()[0][:80]})"
    finally:
        try:
            cur.execute("set statement_timeout = 0")
        except Exception:
            pass
        cur.close()


def _catalog_rows(conn, sql: str) -> tuple[int | None, str]:
    ref = common.parse_libref(sql)
    if not ref:
        return None, "no lib.table in sql"
    cur = conn.cursor()
    try:
        cur.execute("select reltuples::bigint from pg_class where oid = %s::regclass", (f"{ref[0]}.{ref[1]}",))
        row = cur.fetchone()
    except Exception:
        return None, "catalog lookup failed"
    finally:
        cur.close()
    if not row or row[0] is None or row[0] <= 0:
        return None, "catalog has no estimate (view or stale stats)"
    return int(row[0]), "catalog"


def _sample(conn, sql: str, limit: int):
    import pandas as pd

    cur = conn.cursor()
    try:
        cur.execute(f"select * from ({sql}) q limit {limit}")
        columns = [desc[0] for desc in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)
    finally:
        cur.close()


def estimate_table(conn, table: dict, timeout_ms: int, sample_rows: int) -> dict:
    sql = common.build_sql(table)
    rows, source = _count_rows(conn, sql, timeout_ms)
    if rows is None:
        rows, catalog_source = _catalog_rows(conn, sql)
        source = f"{source}; {catalog_source}"

    try:
        sample = _sample(conn, sql, sample_rows)
        bytes_per_row = common.rows_per_byte(sample)
        columns = list(sample.columns)
        sample_note = f"{len(sample)} sampled rows"
    except Exception as exc:
        bytes_per_row = 0.0
        columns = []
        sample_note = f"sample failed ({str(exc).splitlines()[0][:80]})"

    estimated_bytes = int(rows * bytes_per_row) if rows and bytes_per_row else None
    return {
        "name": table["name"],
        "sql": sql,
        "rows": rows,
        "rows_source": source,
        "bytes_per_row": round(bytes_per_row, 2),
        "estimated_bytes": estimated_bytes,
        "estimated_human": common.human_bytes(estimated_bytes) if estimated_bytes else "unknown",
        "columns": columns,
        "sample": sample_note,
        "force_cloud": bool(table.get("force_cloud")),
    }


def decide(tables: list[dict], route_override: str, max_mb: float, max_rows: int) -> dict:
    reasons: list[str] = []
    if route_override in {"local", "cloud"}:
        if route_override == "local":
            reasons.append("route forced to local by caller")
        else:
            reasons.append("route forced to cloud by caller")
        return {"route": route_override, "reasons": reasons}

    route = "local"
    for table in tables:
        name = table["name"]
        if table["force_cloud"]:
            route = "cloud"
            reasons.append(f"{name}: marked force_cloud in spec")
        if table["rows"] is None:
            route = "cloud"
            reasons.append(f"{name}: row count unknown within timeout, cannot size the download")
            continue
        if table["rows"] > max_rows:
            route = "cloud"
            reasons.append(f"{name}: {table['rows']:,} rows exceeds local limit {max_rows:,}")
        if table["estimated_bytes"] is None:
            route = "cloud"
            reasons.append(f"{name}: download size could not be estimated")
        elif table["estimated_bytes"] > max_mb * 1024 * 1024:
            route = "cloud"
            reasons.append(
                f"{name}: ~{common.human_bytes(table['estimated_bytes'])} exceeds local limit {max_mb:g}MB"
            )
    if route == "local":
        total = sum(t["estimated_bytes"] or 0 for t in tables)
        reasons.append(f"all tables fit local limits (total ~{common.human_bytes(total)})")
    return {"route": route, "reasons": reasons}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--route", default="auto", choices=["auto", "local", "cloud"])
    parser.add_argument("--max-local-mb", type=float, default=common.LOCAL_MAX_MB)
    parser.add_argument("--max-local-rows", type=int, default=common.LOCAL_MAX_ROWS)
    parser.add_argument("--sample-rows", type=int, default=common.SAMPLE_ROWS)
    parser.add_argument("--timeout-ms", type=int, default=common.COUNT_TIMEOUT_MS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    spec = common.load_spec(args.spec)
    if spec.get("script") and not spec.get("tables"):
        result = {
            "job": spec["job"],
            "route": args.route if args.route != "auto" else "cloud",
            "reasons": ["spec runs a user script on the cloud host, no client-side estimate possible"],
            "tables": [],
        }
        print(json.dumps(result, indent=2))
        return

    conn = _connect_psycopg2()
    try:
        tables = [estimate_table(conn, t, args.timeout_ms, args.sample_rows) for t in spec["tables"]]
    finally:
        conn.close()

    decision = decide(tables, args.route, args.max_local_mb, args.max_local_rows)
    result = {
        "job": spec["job"],
        "route": decision["route"],
        "reasons": decision["reasons"],
        "limits": {"max_local_mb": args.max_local_mb, "max_local_rows": args.max_local_rows},
        "totals": {
            "rows": sum(t["rows"] or 0 for t in tables),
            "estimated_bytes": sum(t["estimated_bytes"] or 0 for t in tables),
        },
        "tables": tables,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"job      : {result['job']}")
    print(f"route    : {result['route'].upper()}")
    for table in tables:
        print(
            f"  - {table['name']:<24} rows={table['rows'] if table['rows'] is not None else '?':>12}"
            f" size={table['estimated_human']:>10}  ({table['rows_source']})"
        )
    print("reasons  :")
    for reason in result["reasons"]:
        print(f"  * {reason}")
    print("\nnext:")
    if result["route"] == "cloud":
        print(f"  python scripts/run_cloud.py submit --spec {args.spec} --wait")
    else:
        print(f"  python scripts/run_local.py --spec {args.spec}")


if __name__ == "__main__":
    sys.exit(main())
