"""SQLite 完整性校验。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .dbutil import open_ro


def integrity_check(path: Path) -> tuple[str, str]:
    """返回 (state, detail)，state ∈ {"ok", "corrupt", "unavailable"}。"""
    if not path.is_file():
        return "unavailable", "文件不存在"
    try:
        con = open_ro(path)
    except sqlite3.Error as e:
        return "unavailable", str(e)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        s = row[0] if row else ""
        return ("ok", "") if s == "ok" else ("corrupt", s)
    except sqlite3.Error as e:
        return "corrupt", str(e)
    finally:
        con.close()
