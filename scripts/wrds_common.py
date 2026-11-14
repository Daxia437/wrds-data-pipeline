"""Shared helpers for the WRDS data pipeline skill.

No credentials are ever printed or persisted by these helpers beyond the
cloud-side ~/.pgpass that PostgreSQL itself requires.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LOCAL_MAX_MB = 250.0
LOCAL_MAX_ROWS = 2_500_000
SAMPLE_ROWS = 1000
COUNT_TIMEOUT_MS = 20_000
INVENTORY_TTL_DAYS = 90
INVENTORY_VERSION = 2
INVENTORY_METHOD = "live table probes of wrds.list_libraries(), samples excluded"
INVENTORY_NAME = ".wrds_inventory.json"

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def find_env_file() -> Path | None:
    here = Path.cwd().resolve()
    for base in [here, *here.parents[:4]]:
        candidate = base / ".env"
        if candidate.is_file():
            return candidate
    home = Path.home() / ".env"
    if home.is_file():
        return home
    skill_env = SKILL_DIR / ".env"
    if skill_env.is_file():
        return skill_env
    return None


def load_env() -> dict[str, str]:
    env_path = find_env_file()
    if env_path is None:
        raise SystemExit(
            "No .env found. Create one at the project root with WRDS_USER and WRDS_PASS."
        )
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def credentials(env: dict[str, str] | None = None) -> tuple[str, str]:
    env = env or load_env()
    user = env.get("WRDS_USER", "").strip()
    password = env.get("WRDS_PASS", "").strip()
    if not user or not password:
        raise SystemExit("WRDS_USER / WRDS_PASS missing from .env")
    return user, password


def connect():
    import wrds

    user, password = credentials()
    return wrds.Connection(wrds_username=user, wrds_password=password, verbose=False)


# --------------------------------------------------------------------------- #
# subscription inventory
#
# Subscriptions differ by institution, so WRDS's product libraries are probed
# with real queries once and cached for INVENTORY_TTL_DAYS. Catalog visibility
# and privilege functions do not prove access: views may forward to schemas the
# account cannot read. A successful library probe still does not prove every
# table is intact; use `discover.py --table` before relying on a specific table.
# --------------------------------------------------------------------------- #
def inventory_candidates() -> list[Path]:
    override = os.environ.get("WRDS_INVENTORY")
    if override:
        return [Path(override).expanduser()]
    # Home first so every project shares one cache; the project directory is the
    # fallback for locked-down machines and sandboxes.
    return [Path.home() / INVENTORY_NAME, Path.cwd() / INVENTORY_NAME]


def load_inventory() -> dict | None:
    # A sandbox may leave an old readable cache in home but only be able to write
    # the project fallback. Prefer the newest valid snapshot, not merely the first.
    found: list[dict] = []
    for path in inventory_candidates():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["_path"] = str(path)
        found.append(data)
    if not found:
        return None
    return max(found, key=lambda data: data.get("built_utc", ""))


def inventory_age_days(inventory: dict) -> float:
    built = datetime.fromisoformat(inventory["built_utc"])
    if built.tzinfo is None:
        built = built.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - built).total_seconds() / 86400.0


def save_inventory(inventory: dict) -> Path:
    payload = json.dumps({k: v for k, v in inventory.items() if not k.startswith("_")}, indent=2)
    errors: list[str] = []
    for path in inventory_candidates():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
            return path
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    raise SystemExit(
        "cannot cache the subscription inventory; set WRDS_INVENTORY to a writable path\n  "
        + "\n  ".join(errors)
    )


_SAMPLE_LIBRARY = re.compile(r"samp|sample|smp|trial", re.IGNORECASE)


def candidate_libraries(db) -> list[str]:
    """WRDS's own product list, minus the sample/trial libraries everyone can see.

    The PostgreSQL catalog is not a subscription signal: has_table_privilege()
    returns true for tables in schemas the account cannot even USAGE, and some
    libraries (ibes, optionm, boardex) are views forwarding to denied schemas.
    Only a real query answers the question, so we probe this curated list.
    """
    return sorted({name for name in db.list_libraries() if not _SAMPLE_LIBRARY.search(name)})


def _classify_probe_error(exc: Exception) -> tuple[str, str]:
    text = str(exc)
    match = re.search(r"\((?:psycopg2|sqlalchemy)[^)]*\)[^\n]*", text)
    detail = match.group(0)[:180] if match else text.splitlines()[0][:180]
    if "permission denied" in text:
        status = "denied"
    elif "does not exist" in text:
        status = "broken"
    else:
        status = "error"
    return status, detail


def probe_library(db, schema: str) -> dict:
    """Find a queryable table; a denied module must not hide a usable library.

    Large libraries mix products with separate subscriptions. For example, the
    first CRSP view can require the unsubscribed index module while crsp.dsf is
    available. Try every visible table until one succeeds; only classify the
    whole library as unusable after every table has failed.
    """
    try:
        columns = db.raw_sql(
            """select table_name, column_name from information_schema.columns
               where table_schema = %s
               order by table_name, ordinal_position""",
            params=(schema,),
        ).drop_duplicates("table_name")
    except Exception as exc:
        status, detail = _classify_probe_error(exc)
        return {"status": status, "detail": detail}
    if columns.empty:
        return {"status": "empty", "detail": "no table columns visible"}

    failures: list[tuple[str, str, str]] = []
    for row in columns.itertuples(index=False):
        table, column = row.table_name, row.column_name
        try:
            db.raw_sql(f'select "{column}" from {schema}.{table} limit 1')
            result = {"status": "ok", "table": table}
            if failures:
                result["attempts"] = len(failures) + 1
            return result
        except Exception as exc:
            status, detail = _classify_probe_error(exc)
            failures.append((status, detail, table))

    # Prefer the most actionable diagnosis when different tables fail differently.
    for status in ("denied", "broken", "error"):
        failure = next((item for item in failures if item[0] == status), None)
        if failure:
            return {
                "status": status,
                "detail": failure[1],
                "failed_table": failure[2],
                "attempts": len(failures),
            }
    return {"status": "error", "detail": "probe did not run", "attempts": len(failures)}


def build_inventory(db, user: str, quiet: bool = False) -> dict:
    started = time.monotonic()
    candidates = candidate_libraries(db)
    libraries: dict[str, dict] = {}
    for index, schema in enumerate(candidates, start=1):
        libraries[schema] = probe_library(db, schema)
        if not quiet and index % 20 == 0:
            print(f"[inventory] probed {index}/{len(candidates)} libraries", flush=True)
    counts = Counter(entry["status"] for entry in libraries.values())
    return {
        "version": INVENTORY_VERSION,
        "wrds_user": user,
        "built_utc": utc_now(),
        "ttl_days": INVENTORY_TTL_DAYS,
        "build_seconds": round(time.monotonic() - started, 1),
        "method": INVENTORY_METHOD,
        "libraries": libraries,
        "status_counts": dict(counts),
        "gaps": {},
    }


def ensure_inventory(source=None, user: str = "", force: bool = False, quiet: bool = False) -> dict:
    previous = load_inventory()
    inventory = previous
    if inventory is None:
        reason = "first connection"
    elif inventory.get("wrds_user") != user:
        reason = "different WRDS user"
    elif inventory.get("version") != INVENTORY_VERSION:
        reason = "snapshot format changed"
    elif force:
        reason = "refresh requested"
    else:
        age = inventory_age_days(inventory)
        if age < inventory.get("ttl_days", INVENTORY_TTL_DAYS):
            return inventory
        reason = f"{age:.0f} days old"
    if not quiet:
        print(f"[inventory] probing subscriptions ({reason}); this happens once per quarter", flush=True)
    own_connection = not hasattr(source, "list_libraries")
    db = connect() if own_connection else source
    try:
        inventory = build_inventory(db, user, quiet=quiet)
    finally:
        if own_connection:
            db.close()
    if previous and previous.get("wrds_user") == user:
        # Run-time findings outlive a snapshot rebuild; a table that failed last
        # month is still worth warning about until it is probed again.
        inventory["gaps"] = previous.get("gaps") or {}
    try:
        path = save_inventory(inventory)
        inventory["_path"] = str(path)
    except SystemExit as exc:
        # The snapshot is a cache; never fail a data pull because it cannot be stored.
        inventory["_path"] = "(not cached)"
        print(f"[inventory] warning: {exc}", file=sys.stderr, flush=True)
    if not quiet:
        counts = inventory.get("status_counts", {})
        print(
            f"[inventory] {counts.get('ok', 0)} usable / {counts.get('denied', 0)} denied / "
            f"{counts.get('broken', 0) + counts.get('error', 0)} broken libraries "
            f"in {inventory['build_seconds']}s -> {inventory['_path']}",
            flush=True,
        )
    return inventory


def learn_gap(user: str, qualified: str, error: str) -> None:
    """Remember a table that failed at run time; never break the caller."""
    try:
        inventory = load_inventory() or {"wrds_user": user, "libraries": {}, "gaps": {}}
        if inventory.get("wrds_user") != user:
            return
        inventory.setdefault("gaps", {})[qualified] = {
            "error": str(error).splitlines()[0][:200],
            "learned_utc": utc_now(),
        }
        save_inventory(inventory)
    except Exception:
        pass


def gap_for(inventory: dict | None, qualified: str) -> dict | None:
    if not inventory:
        return None
    return (inventory.get("gaps") or {}).get(qualified)


def clear_gap(user: str, qualified: str) -> None:
    """A successful probe retires the earlier failure."""
    try:
        inventory = load_inventory()
        if not inventory or inventory.get("wrds_user") != user:
            return
        gaps = inventory.get("gaps") or {}
        if gaps.pop(qualified, None) is not None:
            save_inventory(inventory)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# specs
# --------------------------------------------------------------------------- #
def load_spec(path: str | Path) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if not spec.get("job"):
        raise SystemExit("spec.job is required")
    tables = spec.get("tables") or []
    if not tables and not spec.get("script"):
        raise SystemExit("spec needs either tables[] or script")
    for table in tables:
        if not table.get("name"):
            raise SystemExit("each table needs a name")
        if not table.get("sql") and not (table.get("library") and table.get("table")):
            raise SystemExit(f"table {table['name']} needs sql or library+table")
    return spec


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


def output_root(spec: dict) -> Path:
    root = Path(spec.get("output_dir") or "data")
    if not root.is_absolute():
        root = Path.cwd() / root
    return root / spec["job"]


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_bytes(num: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"


def rows_per_byte(df) -> float:
    """Compressed parquet bytes per row for a sample frame."""
    if df.empty:
        return 0.0
    try:
        import io

        import pyarrow as pa
        import pyarrow.parquet as pq

        buffer = io.BytesIO()
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buffer, compression="snappy")
        return buffer.tell() / len(df)
    except Exception:
        csv_bytes = len(df.to_csv(index=False).encode("utf-8"))
        return csv_bytes * 0.4 / len(df)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


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
    """Convert date-like string columns to datetime before writing parquet."""
    import pandas as pd

    parsed_columns = {}
    for column in frame.columns:
        dtype = str(frame[column].dtype)
        if dtype not in {"object", "string", "str"}:
            continue
        if not any(hint in str(column).lower() for hint in DATE_HINTS):
            continue
        parsed = pd.to_datetime(frame[column], errors="coerce")
        if parsed.notna().mean() >= 0.95:
            parsed_columns[column] = parsed.astype("datetime64[ns]")
    return frame.assign(**parsed_columns) if parsed_columns else frame


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


LIBREF_RE = re.compile(r"^(?P<lib>[A-Za-z_][A-Za-z0-9_]*)\.(?P<tbl>[A-Za-z_][A-Za-z0-9_]*)$")


def parse_libref(sql: str) -> tuple[str, str] | None:
    """Best-effort extraction of 'lib.table' from a simple select."""
    match = re.search(r"\bfrom\s+([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)", sql, re.I)
    if not match:
        return None
    found = LIBREF_RE.match(match.group(1))
    if not found:
        return None
    return found.group("lib"), found.group("tbl")
