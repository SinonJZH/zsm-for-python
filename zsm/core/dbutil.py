"""SQLite 打开方式与内省工具。"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path


def open_ro(path: Path) -> sqlite3.Connection:
    quoted = urllib.parse.quote(Path(path).as_posix(), safe="/:")
    return sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=5)


def open_rw(path: Path) -> sqlite3.Connection:
    # wait instead of failing instantly when ZCode holds the write lock briefly
    return sqlite3.connect(str(path), timeout=5, isolation_level=None)


def existing_tables(con: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def has_table(con: sqlite3.Connection, table: str) -> bool:
    return table in existing_tables(con)


def qm(n: int) -> str:
    return ",".join(["?"] * n)


def checkpoint(con: sqlite3.Connection) -> None:
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def col_ms(v) -> int:
    """epoch-ms 读取，容忍 SQLite 动态类型把 ms 存成 REAL。"""
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    return 0
