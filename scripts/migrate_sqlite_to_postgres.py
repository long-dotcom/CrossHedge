from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.sql.sqltypes import Boolean, DateTime


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_PATH = ROOT_DIR / "data" / "mt5_hedge.db"
DEFAULT_BACKUP_DIR = ROOT_DIR / "data" / "migration-backups"
CHUNK_SIZE = 10_000
LEGACY_NULL_DEFAULTS: dict[str, dict[str, Any]] = {
    "arbitrage_opportunities": {
        "trigger_hyperliquid_bid": 0.0,
        "trigger_hyperliquid_ask": 0.0,
        "trigger_mt5_bid": 0.0,
        "trigger_mt5_ask": 0.0,
    },
    "hedge_groups": {
        "trigger_hyperliquid_bid": 0.0,
        "trigger_hyperliquid_ask": 0.0,
        "trigger_mt5_bid": 0.0,
        "trigger_mt5_ask": 0.0,
    },
}


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_database_url(cli_url: str | None) -> str:
    if cli_url:
        return cli_url
    env_values = load_env_file(ROOT_DIR / ".env")
    url = env_values.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is missing. Pass --target-url or configure .env first.")
    return url


def display_database_url(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def parse_datetime(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text_value = str(value)
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text_value, fmt)
        except ValueError:
            continue
    return value


def convert_row(row: sqlite3.Row, table: Table, columns: list[str]) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for name in columns:
        value = row[name]
        if value is None:
            value = LEGACY_NULL_DEFAULTS.get(table.name, {}).get(name)
        col_type = table.c[name].type
        if isinstance(col_type, Boolean) and value is not None:
            value = bool(value)
        elif isinstance(col_type, DateTime):
            value = parse_datetime(value)
        converted[name] = value
    return converted


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    }


def sqlite_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [row[1] for row in conn.execute(f'pragma table_info("{table_name}")')]


def count_sqlite_rows(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f'select count(*) from "{table_name}"').fetchone()[0])


def pg_tables(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    return sorted(inspector.get_table_names(schema="public"))


def count_pg_rows(engine: Engine, table_name: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f'select count(*) from "{table_name}"')).scalar() or 0)


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def ensure_postgres_database(target_url: str) -> None:
    url = make_url(target_url)
    database_name = url.database
    if not database_name:
        raise SystemExit("Target PostgreSQL URL must include a database name.")

    maintenance_url = url.set(database="postgres")
    try:
        engine = create_engine(maintenance_url, future=True, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            exists = conn.execute(text("select 1 from pg_database where datname = :name"), {"name": database_name}).scalar()
            if exists:
                return
            conn.execute(text(f"create database {quote_identifier(database_name)}"))
            print(f"Created PostgreSQL database: {database_name}")
    except Exception as exc:
        fallback_url = url.set(database="template1")
        engine = create_engine(fallback_url, future=True, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            exists = conn.execute(text("select 1 from pg_database where datname = :name"), {"name": database_name}).scalar()
            if exists:
                return
            conn.execute(text(f"create database {quote_identifier(database_name)}"))
            print(f"Created PostgreSQL database: {database_name}")


def postgres_database_exists(target_url: str) -> bool:
    url = make_url(target_url)
    database_name = url.database
    if not database_name:
        raise SystemExit("Target PostgreSQL URL must include a database name.")
    for maintenance_db in ("postgres", "template1"):
        try:
            engine = create_engine(url.set(database=maintenance_db), future=True)
            with engine.connect() as conn:
                return bool(conn.execute(text("select 1 from pg_database where datname = :name"), {"name": database_name}).scalar())
        except Exception:
            continue
    raise SystemExit("Cannot connect to PostgreSQL maintenance database postgres/template1.")


def init_postgres_schema(target_url: str) -> None:
    ensure_postgres_database(target_url)
    os.environ["DATABASE_URL"] = target_url
    sys.path.insert(0, str(ROOT_DIR / "backend"))
    from app.db.init_db import init_db

    init_db()


def backup_sqlite(source: Path, backup_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination_dir = backup_root / stamp
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "sqlite-before-postgres-migration.db"
    shutil.copy2(source, destination)
    return destination_dir


def truncate_pg(engine: Engine, table_names: list[str]) -> None:
    if not table_names:
        return
    quoted = ", ".join(f'"{name}"' for name in table_names)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


def reset_sequences(engine: Engine, metadata: MetaData, table_names: list[str]) -> None:
    with engine.begin() as conn:
        for table_name in table_names:
            table = metadata.tables.get(table_name)
            if table is None or "id" not in table.columns:
                continue
            sequence_name = conn.execute(
                text("select pg_get_serial_sequence(:table_name, 'id')"),
                {"table_name": table_name},
            ).scalar()
            if not sequence_name:
                continue
            max_id = conn.execute(text(f'select coalesce(max(id), 0) from "{table_name}"')).scalar() or 0
            conn.execute(
                text("select setval(:sequence_name, :next_value, false)"),
                {"sequence_name": sequence_name, "next_value": int(max_id) + 1},
            )


def migrate(source: Path, target_url: str, replace: bool, dry_run: bool) -> None:
    if not source.exists():
        raise SystemExit(f"SQLite source not found: {source}")
    if not target_url.startswith("postgresql"):
        raise SystemExit("Target DATABASE_URL must be PostgreSQL.")

    sqlite = sqlite3.connect(source)
    sqlite.row_factory = sqlite3.Row
    source_tables = sqlite_tables(sqlite)
    pg_engine = create_engine(target_url, future=True)

    metadata = MetaData()
    metadata.reflect(bind=pg_engine)
    target_tables = pg_tables(pg_engine)
    insert_order = [table for table in metadata.sorted_tables if table.name in target_tables]

    if not insert_order:
        raise SystemExit("PostgreSQL schema has no application tables. Check DATABASE_URL and init_db.")

    print(f"Source SQLite: {source}")
    print(f"Target PostgreSQL: {display_database_url(target_url)}")
    print("")

    print("Planned row counts:")
    total_source_rows = 0
    for table in insert_order:
        count = count_sqlite_rows(sqlite, table.name) if table.name in source_tables else 0
        total_source_rows += count
        print(f"  {table.name}: {count}")

    target_existing = sum(count_pg_rows(pg_engine, table.name) for table in insert_order)
    print("")
    print(f"Source total rows: {total_source_rows}")
    print(f"Target existing rows: {target_existing}")

    if dry_run:
        print("Dry run only; no data changed.")
        sqlite.close()
        return

    if target_existing > 0 and not replace:
        sqlite.close()
        raise SystemExit("PostgreSQL already contains data. Re-run with --replace to overwrite it.")

    truncate_pg(pg_engine, target_tables)

    for table in insert_order:
        table_name = table.name
        if table_name not in source_tables:
            print(f"{table_name}: skipped, missing in SQLite")
            continue
        source_columns = sqlite_columns(sqlite, table_name)
        common_columns = [column.name for column in table.columns if column.name in source_columns]
        if not common_columns:
            print(f"{table_name}: skipped, no shared columns")
            continue

        select_sql = "select " + ", ".join(f'"{name}"' for name in common_columns) + f' from "{table_name}"'
        cursor = sqlite.execute(select_sql)
        inserted = 0
        batch: list[dict[str, Any]] = []
        with pg_engine.begin() as conn:
            for row in cursor:
                batch.append(convert_row(row, table, common_columns))
                if len(batch) >= CHUNK_SIZE:
                    conn.execute(table.insert(), batch)
                    inserted += len(batch)
                    batch.clear()
            if batch:
                conn.execute(table.insert(), batch)
                inserted += len(batch)
        print(f"{table_name}: inserted {inserted}")

    reset_sequences(pg_engine, metadata, target_tables)
    sqlite.close()
    print("Migration complete.")


def dry_run_source_only(source: Path, target_url: str, reason: str) -> None:
    if not source.exists():
        raise SystemExit(f"SQLite source not found: {source}")
    sqlite = sqlite3.connect(source)
    source_tables = sorted(sqlite_tables(sqlite))
    print(f"Source SQLite: {source}")
    print(f"Target PostgreSQL: {display_database_url(target_url)}")
    print(f"Target inspection skipped: {reason}")
    print("")
    print("SQLite row counts:")
    total_source_rows = 0
    for table_name in source_tables:
        count = count_sqlite_rows(sqlite, table_name)
        total_source_rows += count
        print(f"  {table_name}: {count}")
    print("")
    print(f"Source total rows: {total_source_rows}")
    print("Dry run only; no data changed.")
    sqlite.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate MT5 Hedge data from SQLite to PostgreSQL.")
    parser.add_argument("--source", default=str(DEFAULT_SQLITE_PATH), help="SQLite database path.")
    parser.add_argument("--target-url", default=None, help="PostgreSQL SQLAlchemy URL. Defaults to .env DATABASE_URL.")
    parser.add_argument("--replace", action="store_true", help="Truncate PostgreSQL tables before importing.")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    parser.add_argument("--dry-run", action="store_true", help="Print row counts without changing PostgreSQL.")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="Directory for SQLite backup copy.")
    parser.add_argument("--skip-backup", action="store_true", help="Do not copy SQLite before migration.")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    target_url = resolve_database_url(args.target_url)
    if target_url.startswith("sqlite"):
        raise SystemExit("Current target DATABASE_URL is SQLite. Set .env DATABASE_URL to PostgreSQL first.")

    if not args.dry_run and not args.yes:
        answer = input("This can overwrite PostgreSQL data when --replace is used. Type MIGRATE to continue: ")
        if answer != "MIGRATE":
            raise SystemExit("Cancelled.")

    if not args.dry_run and not args.skip_backup:
        backup_dir = backup_sqlite(source, Path(args.backup_dir).expanduser().resolve())
        print(f"SQLite backup written to: {backup_dir}")

    if args.dry_run:
        if not postgres_database_exists(target_url):
            dry_run_source_only(source, target_url, "target database does not exist")
            return
    else:
        init_postgres_schema(target_url)
    migrate(source, target_url, args.replace, args.dry_run)


if __name__ == "__main__":
    main()
