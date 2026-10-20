#!/usr/bin/env python3
"""
Execute every Room query against a real SQLite database.

Room compiles each `@Query` against the schema it derives from the `@Entity`
classes, and reports a failure as generated-Java errors that take a while to
read. This builds the equivalent SQLite schema from `Entities.kt`, prepares
every query in `Dao.kt` against it, and runs it with dummy bind values — so a
syntax error, a wrong column, a bad enum comparison or a join that does not
resolve shows up here with the function name next to it.

SQLite is the same engine Room runs on, so a query that prepares and executes
here will prepare and execute on the device.

Run from `android/`:  python3 tools/verify_sql.py
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app/src/main/java/lk/ac/nibm/gnlc"

# How Room maps Kotlin types to SQLite column types.
TYPE_MAP = {
    "Long": "INTEGER", "Int": "INTEGER", "Short": "INTEGER",
    "Boolean": "INTEGER", "Double": "REAL", "Float": "REAL",
    "String": "TEXT", "ByteArray": "BLOB",
    "SyncState": "TEXT",          # via the @TypeConverter
}

problems: list[str] = []


def parse_entities() -> list[tuple[str, list[tuple[str, str, bool]], list[str]]]:
    """(table, [(column, sql_type, not_null)], primary_key_columns)"""
    text = (SRC / "data/Entities.kt").read_text(encoding="utf-8")
    tables = []

    for block in re.split(r"(?=@Entity)", text):
        if not block.startswith("@Entity"):
            continue
        tname = re.search(r'tableName\s*=\s*"([A-Za-z0-9_]+)"', block)
        if not tname:
            continue
        cls = re.search(r"data class \w+\((.*?)\n\)", block, re.S)
        if not cls:
            continue

        # Composite primary keys declared on @Entity.
        composite = re.search(r"primaryKeys\s*=\s*\[([^\]]*)\]", block)
        pk_cols = (
            [c.strip().strip('"') for c in composite.group(1).split(",") if c.strip()]
            if composite else []
        )

        cols: list[tuple[str, str, bool]] = []
        body = cls.group(1)
        # Each constructor parameter, with any annotations in front of it.
        for m in re.finditer(
            r"(?:^|\n)((?:\s*@\w+(?:\([^)]*\))?\s*)*)\s*(?:val|var)\s+"
            r"(\w+)\s*:\s*([\w<>, ?]+?)\s*(?:=|,|$)",
            body,
        ):
            annots, name, ktype = m.group(1), m.group(2), m.group(3).strip()
            nullable = ktype.endswith("?")
            base = ktype.rstrip("?").strip()
            sql_type = TYPE_MAP.get(base)
            if sql_type is None:
                problems.append(
                    f"Entities.kt: {tname.group(1)}.{name} has type {base!r}, "
                    f"which Room has no built-in mapping for"
                )
                sql_type = "TEXT"
            if "@PrimaryKey" in annots:
                pk_cols = [name]
            cols.append((name, sql_type, not nullable))

        tables.append((tname.group(1), cols, pk_cols))
    return tables


def build_db(tables) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for table, cols, pk in tables:
        parts = []
        for name, sql_type, not_null in cols:
            piece = f'"{name}" {sql_type}'
            # Room makes the single autoGenerate key INTEGER PRIMARY KEY.
            if pk == [name] and sql_type == "INTEGER":
                piece += " PRIMARY KEY AUTOINCREMENT"
            elif not_null:
                piece += " NOT NULL"
            parts.append(piece)
        if len(pk) > 1:
            parts.append("PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk) + ")")
        conn.execute(f'CREATE TABLE "{table}" (' + ", ".join(parts) + ")")
    conn.commit()
    return conn


DUMMY = {
    "INTEGER": 1, "TEXT": "x", "REAL": 1.0, "BLOB": b"x",
}


def extract_queries() -> list[tuple[str, str, list[str]]]:
    """(function name, sql, ordered bind parameter names)"""
    dao = (SRC / "data/Dao.kt").read_text(encoding="utf-8")
    pattern = re.compile(
        r'@Query\(\s*(?:"""(?P<triple>.*?)"""|(?P<single>(?:"[^"]*"\s*(?:\+\s*)?)+))'
        r'\s*\)\s*suspend fun (?P<fn>\w+)\(',
        re.S)
    out = []
    for m in pattern.finditer(dao):
        sql = m.group("triple")
        if sql is None:
            sql = "".join(re.findall(r'"([^"]*)"', m.group("single")))
        sql = " ".join(sql.split())
        binds = []
        for name in re.findall(r":(\w+)", sql):
            if name not in binds:
                binds.append(name)
        out.append((m.group("fn"), sql, binds))
    return out


def main() -> int:
    tables = parse_entities()
    if not tables:
        print("FAIL  no @Entity tables found")
        return 1

    conn = build_db(tables)
    print(f"Built {len(tables)} tables from Entities.kt: "
          + ", ".join(t for t, _, _ in tables))

    queries = extract_queries()
    ran = 0

    for fn, sql, binds in queries:
        # Room turns :name into a positional ?; the values only have to be of
        # a plausible type, since what is being tested is the statement.
        prepared = sql
        for name in binds:
            prepared = prepared.replace(f":{name}", "?")
        args = [guess_value(name) for name in binds]

        try:
            cur = conn.execute(prepared, args)
            if prepared.lstrip().upper().startswith("SELECT"):
                cur.fetchall()
            ran += 1
        except sqlite3.Error as exc:
            problems.append(f"Dao.kt {fn}(): {exc}\n          {sql}")

    conn.rollback()
    print(f"Executed {ran} of {len(queries)} queries against SQLite.\n")

    if problems:
        print(f"{len(problems)} problem(s):\n")
        for p in problems:
            print(f"  FAIL  {p}")
        return 1

    print("Every Room query prepares and executes.")
    return 0


def guess_value(name: str):
    """A value of the right shape for each bind parameter."""
    lowered = name.lower()
    if any(k in lowered for k in ("id", "qty", "limit", "before", "millis",
                                  "attempts", "engine", "order")):
        return 1
    if "state" in lowered:
        return "QUEUED"
    return "x"


if __name__ == "__main__":
    sys.exit(main())
