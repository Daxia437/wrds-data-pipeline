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
from datetime import datetime, timezone
from pathlib import Path

LOCAL_MAX_MB = 250.0
LOCAL_MAX_ROWS = 2_500_000
SAMPLE_ROWS = 1000
COUNT_TIMEOUT_MS = 20_000

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
            parsed_columns[column] = parsed
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
