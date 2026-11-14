"""List the libraries WRDS authorizes for this account.

Subscriptions differ by institution, so nothing here assumes CRSP or Compustat
is available. The snapshot reads WRDS's access-aware list_libraries() metadata,
excludes sample libraries, and caches the result for 90 days; it does not query
every table. Library-level authorization does not prove every table works:
`--table` runs a real-column query because an individual view can still have a
missing or denied underlying relation.

Usage:
    python scripts/discover.py                  # cached subscription summary
    python scripts/discover.py --refresh        # rebuild the snapshot now
    python scripts/discover.py --libraries      # every authorized library
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
    libraries = inventory.get("libraries", {})
    authorized = sorted(name for name, entry in libraries.items() if entry.get("status") == "ok")
    counts = inventory.get("status_counts", {})
    gaps = inventory.get("gaps") or {}
    age = common.inventory_age_days(inventory)
    print(f"user      : {inventory['wrds_user']}")
    print(
        f"snapshot  : {inventory['built_utc'][:10]} "
        f"({age:.0f} days ago, refreshes every {inventory['ttl_days']} days)"
    )
    print(f"cache     : {inventory['_path']}")
    print(f"method    : {inventory.get('method', 'n/a')}")
    print(f"\nauthorized libraries ({len(authorized)}, from WRDS access metadata):")
    for i in range(0, len(authorized), 4):
        print("  " + "  ".join(f"{name:26s}" for name in authorized[i:i + 4]))
    unavailable = {k: v for k, v in counts.items() if k != "ok"}
    if unavailable:
        print(f"\nother statuses: {unavailable} — details with discover.py --libraries")
    if gaps:
        print(f"known gaps: {len(gaps)} tables failed at run time — discover.py --gaps")
    print("\nnext: discover.py --schema <name> | --table <schema>.<table> | --grep <text>")


def _libraries(db) -> None:
    user, _ = common.credentials()
    inventory = common.ensure_inventory(db, user, quiet=True)
    for name, entry in sorted(inventory.get("libraries", {}).items()):
        status = "authorized" if entry.get("status") == "ok" else entry.get("status", "?")
        print(f"{name:28s} {status}")


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
    parser = argparse.ArgumentParser(description="Read WRDS authorization metadata for this account.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--schema", help="list tables in this schema")
    action.add_argument("--table", help="list columns, and probe access, for schema.table")
    action.add_argument("--grep", help="search table names across schemas")
    action.add_argument("--gaps", action="store_true", help="tables that failed in earlier runs")
    action.add_argument("--libraries", action="store_true", help="every library in the access snapshot")
    action.add_argument("--live", action="store_true", help="table counts straight from the catalog")
    parser.add_argument("--refresh", action="store_true", help="rebuild the cached snapshot")
    args = parser.parse_args()

    if args.refresh and (args.schema or args.table or args.grep or args.gaps or args.libraries or args.live):
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
        elif args.libraries:
            _libraries(db)
        elif args.live:
            _table_counts(db)
        else:
            _summary(db, force=args.refresh)
    finally:
        db.close()


if __name__ == "__main__":
    main()
