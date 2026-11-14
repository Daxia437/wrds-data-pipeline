"""List what a WRDS account can actually reach.

Subscriptions differ by institution, so never assume a library or table exists.
Metadata is not proof either: some tables appear in ``information_schema`` while
the underlying schema is not provisioned, so ``--table`` also runs a zero-row
probe and reports whether the table is queryable.
"""

from __future__ import annotations

import argparse
import sys

import wrds_common as common


def _schemas(db) -> None:
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
    print(f"\n{len(frame)} schemas, {int(frame['tables'].sum())} tables")
    print("inspect one with: discover.py --schema <name>")


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
    try:
        db.raw_sql(f'select "{column}" from {schema}.{table} limit 1')
        status = "queryable"
    except Exception as exc:  # permission, missing schema, withdrawn table
        status = f"NOT queryable: {str(exc).splitlines()[0][:160]}"

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


def main() -> None:
    parser = argparse.ArgumentParser(description="List accessible WRDS schemas, tables and columns.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--schema", help="list tables in this schema")
    group.add_argument("--table", help="list columns, and probe access, for schema.table")
    group.add_argument("--grep", help="search table names across schemas")
    args = parser.parse_args()

    db = common.connect()
    try:
        if args.schema:
            _tables(db, args.schema)
        elif args.table:
            _columns(db, args.table)
        elif args.grep:
            _grep(db, args.grep)
        else:
            _schemas(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
