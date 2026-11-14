"""List what a WRDS account can actually reach.

Subscriptions differ by institution, so nothing here assumes CRSP or Compustat
is available. The first run builds a grant-level snapshot of reachable schemas
and caches it for 90 days; later runs reuse it and refresh automatically once
it expires. `--table` adds a live probe because metadata is not proof: WRDS
lists some tables whose underlying schema is not provisioned.

Usage:
    python scripts/discover.py                  # cached subscription summary
    python scripts/discover.py --refresh        # rebuild the snapshot now
    python scripts/discover.py --gaps           # tables that failed at run time
    python scripts/discover.py --schema ibes    # live table list for a schema
    python scripts/discover.py --table comp.funda
    python scripts/discover.py --grep linktable
"""

from __future__ import annotations

import argparse
import sys

import wrds_common as common


def _table_counts(db) -> None:
    frame = db.raw_sql(
        """
        select table_schema as schema, count(*) as tables
        from information_schema.tables
        where table_type in ('BASE TABLE', 'VIEW')
          and table_schema not in ('pg_catalog', 'information_schema', 'public')
        group by 1
        order by 1
        """
    )
    print(frame.to_string(index=False))
    print(f"\n{len(frame)} schemas, {int(frame['tables'].sum())} tables visible to this account")


def _tables(db, schema: str) -> None:
    frame = db.raw_sql(
        """
        select table_name, table_type
        from information_schema.tables
        where table_schema = %s
          and table_type in ('BASE TABLE', 'VIEW')
        order by table_name
        """,
        params=(schema,),
    )
    if frame.empty:
        common.fail(f"no tables visible in schema '{schema}' — check the name with discover.py")
    print(frame.to_string(index=False))
    print(f"\n{len(frame)} tables in {schema}")
    print(f"columns for one with: discover.py --table {schema}.<table>")


def _columns(db, qualified: str) -> None:
    if "." not in qualified:
        common.fail("use --table <schema>.<table>")
    schema, table = qualified.split(".", 1)
    frame = db.raw_sql(
        """
        select column_name, data_type
        from information_schema.columns
        where table_schema = %s and table_name = %s
        order by ordinal_position
        """,
        params=(schema, table),
    )
    if frame.empty:
        common.fail(f"{qualified} is not visible in metadata")

    # `select 1 ... limit 0` can be answered without expanding a view, so probe a
    # real column: that forces resolution of the underlying relation.
    column = frame.iloc[0]["column_name"]
    user, _ = common.credentials()
    try:
        db.raw_sql(f'select "{column}" from {schema}.{table} limit 1')
        status = "queryable"
        common.clear_gap(user, qualified)
    except Exception as exc:  # permission, missing schema, withdrawn table
        status = f"NOT queryable: {str(exc).splitlines()[0][:160]}"
        common.learn_gap(user, qualified, status)

    print(frame.to_string(index=False))
    print(f"\n{len(frame)} columns in {qualified}")
    print(f"access: {status}")
    if status != "queryable":
        sys.exit(1)


def _grep(db, keyword: str) -> None:
    frame = db.raw_sql(
        """
        select table_schema, table_name
        from information_schema.tables
        where table_type in ('BASE TABLE', 'VIEW')
          and table_name ilike %s
          and table_schema not in ('pg_catalog', 'information_schema', 'public')
        order by 1, 2
        limit 300
        """,
        params=(f"%{keyword}%",),
    )
    if frame.empty:
        print(f"no table name matches '{keyword}'")
        return
    print(frame.to_string(index=False))
    print(f"\n{len(frame)} matches (capped at 300)")


def _summary(db, force: bool) -> None:
    user, _ = common.credentials()
    inventory = common.ensure_inventory(db, user, force=force)
    schemas = inventory["schemas"]
    age = common.inventory_age_days(inventory)
    gaps = inventory.get("gaps") or {}
    print(f"user      : {inventory['wrds_user']}")
    print(
        f"snapshot  : {inventory['built_utc'][:10]} "
        f"({age:.0f} days ago, refreshes every {inventory['ttl_days']} days)"
    )
    print(f"cache     : {inventory['_path']}")
    print(
        f"reachable : {len(schemas)} schemas, "
        f"{sum(s['selectable'] for s in schemas.values()):,} selectable tables"
    )
    if gaps:
        print(f"known gaps: {len(gaps)} tables failed at run time — discover.py --gaps")
    print("\nnext: discover.py --schema <name> | --table <schema>.<table> | --grep <text>")


def _gaps(db) -> None:
    user, _ = common.credentials()
    inventory = common.ensure_inventory(db, user, quiet=True)
    gaps = inventory.get("gaps") or {}
    if not gaps:
        print("no run-time gaps recorded")
        return
    for qualified, detail in sorted(gaps.items()):
        print(f"{qualified}\n  {detail['learned_utc'][:10]}  {detail['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe WRDS access for this account.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--schema", help="list tables in this schema")
    action.add_argument("--table", help="list columns, and probe access, for schema.table")
    action.add_argument("--grep", help="search table names across schemas")
    action.add_argument("--gaps", action="store_true", help="tables that failed in earlier runs")
    action.add_argument("--live", action="store_true", help="table counts straight from the catalog")
    parser.add_argument("--refresh", action="store_true", help="rebuild the cached snapshot")
    args = parser.parse_args()

    if args.refresh and (args.schema or args.table or args.grep or args.live):
        parser.error("--refresh applies to the cached summary only")

    db = common.connect()
    try:
        if args.schema:
            _tables(db, args.schema)
        elif args.table:
            _columns(db, args.table)
        elif args.grep:
            _grep(db, args.grep)
        elif args.gaps:
            _gaps(db)
        elif args.live:
            _table_counts(db)
        else:
            _summary(db, force=args.refresh)
    finally:
        db.close()


if __name__ == "__main__":
    main()
