#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""zsm.py — ZCode 会话管理器（CLI 版）。

浏览 ZCode（智谱 ADE）的全部历史会话（含 UI 上已归档/删除的），并把选定的会话
从 ZCode 数据库与磁盘中彻底删除。删除前自动备份，删除后完整性校验，失败自动还原。

ZCode 的 UI"删除"实际只是归档：会话正文仍留在 cli/db/db.sqlite，磁盘文件仍留在
cli/{exec,artifacts,rollout,...} 下。本工具补上真正的删除。

用法（python = ~/py_global_venv/bin/python）:
  python zsm.py list                        # 列出全部会话（含已归档/ghost）
  python zsm.py list --active-only          # 只看未归档的
  python zsm.py show <id|前缀>              # 只读浏览某会话完整消息流
  python zsm.py plan <id...>                # 删除计划（dry-run，不动任何数据）
  python zsm.py delete <id...> --yes        # 备份 → 删除 → 校验（失败自动还原）
  python zsm.py compat / integrity          # 结构兼容 / 完整性检查（只读）

常用参数:
  --dir PATH        指定 ZCode 数据目录（默认自动探测 ~/.zcode）。
                    可指向 /mnt/c/Users/<name>/.zcode 管理 Windows 侧数据；
                    此时进程探测自动改走 tasklist.exe（WSL interop）。
  --backups-dir PATH  备份目录（默认 <zcode>/zsm-backups）。
  --json            机器可读输出。

delete 安全轨（与原版一致）:
  * 数据库结构与预期不符（compat）→ 拒绝操作；
  * 库文件完整性检查不过 → 拒绝操作；
  * ZCode 正在运行 → 默认拒绝；--limited 进入受限模式（仅"已归档 + 闲置超过
    --idle-minutes（默认 60）+ 未被启用的自动化引用"的会话，根会话必须已归档）；
  * 先备份后删除：备份含两个 sqlite 库（连同 -wal）+ 会话磁盘文件 + manifest.json，
    整体还原 = 把备份目录内容拷回原位；
  * 删除后 PRAGMA integrity_check，失败自动从本次备份整体还原。

手工还原方法：退出 ZCode，把 <备份目录>/db.sqlite、tasks-index.sqlite 拷回
<zcode>/cli/db/、<zcode>/v2/（先删原位的 -wal/-shm），disk/ 下的内容按 manifest.json
里的记录拷回 cli/ 对应相对路径。

注意事项:
  * 会话 ID 支持唯一前缀匹配（有歧义时报错并列出候选）。
  * delete 成功后默认对两个库执行 VACUUM 真正回收文件空间（--no-vacuum 跳过）。
  * delete 的执行报告会写入 scripts/out/zsm/<时间戳>-delete.json。
  * 首次正式使用前，建议先造一个无意义的测试会话对其执行删除，确认行为符合预期。
  * 跨端写操作（--dir 指向 /mnt/...）在 ZCode（Windows 侧）完全退出后进行；sqlite
    经 drvfs 跨文件系统写入虽有备份兜底，但更稳妥的做法是把目录复制到 WSL 本地操作。

版权说明：本工具为 zcode-session-manager（MIT License, Copyright (c) 2026 wooooooooolf,
https://github.com/woooooooooolf/zcode-session-manager）核心逻辑的 Python 移植衍生作品，
依 MIT 许可保留原版权声明；原 LICENSE 全文见同目录 LICENSE.zsm-core。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - 环境缺失时只读功能仍可用
    psutil = None

try:
    from rich.console import Console
    from rich.table import Table

    _rich = True
except ImportError:  # pragma: no cover
    _rich = False

OUT_DIR = Path(__file__).resolve().parent.parent / "out" / "zsm"

# ---------------------------------------------------------------- 路径与工具


@dataclass
class Paths:
    zcode_dir: Path
    cli_dir: Path
    db_path: Path
    tasks_db: Path

    @classmethod
    def from_dir(cls, d: Path) -> "Paths":
        return cls(zcode_dir=d, cli_dir=d / "cli", db_path=d / "cli" / "db" / "db.sqlite",
                   tasks_db=d / "v2" / "tasks-index.sqlite")

    @classmethod
    def default(cls) -> "Paths | None":
        home = Path.home()
        cand = home / ".zcode"
        return cls.from_dir(cand) if looks_valid(cand) else None

    def backups_base(self, custom: str | None) -> Path:
        if custom and custom.strip():
            return Path(custom.strip())
        return self.zcode_dir / "zsm-backups"


def looks_valid(d: Path) -> bool:
    return (d / "cli" / "db" / "db.sqlite").is_file() or (d / "v2" / "tasks-index.sqlite").is_file()


def open_ro(path: Path) -> sqlite3.Connection:
    quoted = urllib.parse.quote(Path(path).as_posix(), safe="/:")
    return sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=5)


def open_rw(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    return con


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


def fmt_time(ms: int) -> str:
    if not ms:
        return "-"
    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}GB"


# ---------------------------------------------------------------- 兼容性门

CHILD_TABLES = [
    "message", "part", "session_entry", "session_input", "session_target",
    "todo", "tool_usage", "turn_usage", "model_usage", "input_history",
]
SESSION_REQUIRED_COLS = ["id", "parent_id", "directory", "title", "task_type",
                         "time_created", "time_updated"]


@dataclass
class CompatReport:
    db_present: bool
    tasks_present: bool
    problems: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def compat_check(paths: Paths) -> CompatReport:
    problems: list[str] = []
    warnings: list[str] = []
    db_present = paths.db_path.is_file()
    if not db_present:
        problems.append("db.sqlite: cli/db/db.sqlite 不存在")
    else:
        try:
            con = open_ro(paths.db_path)
        except sqlite3.Error as e:
            problems.append(f"db.sqlite: 无法打开 ({e})")
        else:
            tables = existing_tables(con)
            for t in ("session", "message", "part"):
                if t not in tables:
                    problems.append(f"db.sqlite: 缺少必需的表 `{t}`")
            if "session" in tables:
                cols = columns(con, "session")
                for c in SESSION_REQUIRED_COLS:
                    if c not in cols:
                        problems.append(f"db.sqlite: `session` 缺少列 `{c}`")
            for t in ("message", "part", *CHILD_TABLES):
                if t in tables and "session_id" not in columns(con, t):
                    problems.append(f"db.sqlite: `{t}` 缺少列 `session_id`")
            for t, col in (("dwf_run", "parent_session_id"), ("dwf_actor", "session_id"),
                           ("dwf_node", "run_id"), ("dwf_event", "run_id")):
                if t in tables and col not in columns(con, t):
                    problems.append(f"db.sqlite: `{t}` 缺少列 `{col}`")
            con.close()
    tasks_present = paths.tasks_db.is_file()
    if not tasks_present:
        warnings.append("tasks-index: v2/tasks-index.sqlite 不存在 — 归档/置顶标记不可用")
    else:
        try:
            con = open_ro(paths.tasks_db)
        except sqlite3.Error as e:
            problems.append(f"tasks-index: 无法打开 ({e})")
        else:
            if not has_table(con, "tasks"):
                problems.append("tasks-index: 缺少表 `tasks`")
            else:
                cols = columns(con, "tasks")
                for c in ("task_id", "archived"):
                    if c not in cols:
                        problems.append(f"tasks-index: `tasks` 缺少列 `{c}`")
            con.close()
    return CompatReport(db_present, tasks_present, problems, warnings)


# ---------------------------------------------------------------- 完整性


def integrity_check(path: Path) -> tuple[str, str]:
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


# ---------------------------------------------------------------- 进程探测

RUNNING_OVERRIDE_ENV = "ZSM_FORCE_RUNNING"  # 测试钩子: "1"=运行中 "0"=未运行


def _psutil_probe() -> bool | None:
    if psutil is None:
        return None
    me = os.getpid()
    try:
        for p in psutil.process_iter(["pid", "name"]):
            if p.info["pid"] == me:
                continue
            name = (p.info.get("name") or "").lower()
            if "zcode" in name:
                return True
    except Exception:
        return None
    return False


def _windows_probe() -> bool | None:
    """WSL interop 下通过 tasklist.exe 探测 Windows 侧 ZCode。True/False/未知=None。"""
    exe = shutil.which("tasklist.exe")
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "/FO", "CSV", "/NH"], capture_output=True, timeout=20)
        text = r.stdout.decode("utf-8", "replace").lower()
        return "zcode" in text
    except Exception:
        return None


def zcode_running(paths: Paths) -> bool | None:
    """None 表示无法探测（对 delete 是拒绝执行的理由）。"""
    forced = os.environ.get(RUNNING_OVERRIDE_ENV)
    if forced == "1":
        return True
    if forced == "0":
        return False
    local = _psutil_probe()
    if local:
        return True
    win: bool | None = None
    if str(paths.zcode_dir).startswith("/mnt/"):
        win = _windows_probe()
        if win:
            return True
    if local is None and win is None:
        return None
    return False


# ---------------------------------------------------------------- 磁盘目标


def disk_targets(cli_dir: Path, sid: str) -> list[Path]:
    return [
        cli_dir / "agents" / sid,
        cli_dir / "artifacts" / sid,
        cli_dir / "exec" / sid,
        cli_dir / "exec" / "bash-startup" / sid,
        cli_dir / "image-cache" / sid,
        cli_dir / "rollout" / f"model-io-{sid}.jsonl",
    ]


def walk_size(path: Path) -> tuple[int, int]:
    """返回 (bytes, files)。"""
    b = f = 0
    try:
        it = path.rglob("*") if path.is_dir() else iter([])
        for p in it:
            try:
                if p.is_file():
                    b += p.stat().st_size
                    f += 1
            except OSError:
                pass
    except OSError:
        pass
    if path.is_file():
        try:
            b += path.stat().st_size
            f += 1
        except OSError:
            pass
    return b, f


def disk_usage(cli_dir: Path, sid: str) -> int:
    return sum(walk_size(t)[0] for t in disk_targets(cli_dir, sid))


def disk_remove(targets: list[Path]) -> tuple[int, int, list[str]]:
    files = b = 0
    errors: list[str] = []
    for t in targets:
        if not t.exists() and not t.is_symlink():
            continue
        tb, tf = walk_size(t)
        try:
            if t.is_dir() and not t.is_symlink():
                shutil.rmtree(t)
            else:
                t.unlink()
            b += tb
            files += max(tf, 1)
        except OSError as e:
            errors.append(f"{t}: {e}")
    return files, b, errors


# ---------------------------------------------------------------- 备份 / 还原


def _copy_dir(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)


def new_backup_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = base / stamp
    n = 1
    while d.exists():
        d = base / f"{stamp}-{n}"
        n += 1
    d.mkdir(parents=True)
    return d


def copy_database(src: Path, dest_dir: Path) -> None:
    if not src.is_file():
        return
    try:
        con = open_rw(src)
        checkpoint(con)
        con.close()
    except sqlite3.Error:
        pass
    shutil.copy2(src, dest_dir / src.name)
    wal = Path(str(src) + "-wal")
    if wal.is_file():
        shutil.copy2(wal, dest_dir / (src.name + "-wal"))


@dataclass
class DiskRecord:
    source: str
    backup_rel: str
    is_dir: bool


def copy_session_disk(cli_dir: Path, sids: list[str], backup_dir: Path) -> list[DiskRecord]:
    records: list[DiskRecord] = []
    disk_root = backup_dir / "disk"
    for sid in sids:
        for t in disk_targets(cli_dir, sid):
            if not t.exists():
                continue
            rel = t.relative_to(cli_dir).as_posix()
            dest = disk_root / rel
            if t.is_dir():
                _copy_dir(t, dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(t, dest)
            records.append(DiskRecord(str(t), f"disk/{rel}", t.is_dir()))
    return records


def write_manifest(backup_dir: Path, manifest: dict) -> None:
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_from_backup(backup_dir: Path, paths: Paths, records: list[DiskRecord]) -> list[str]:
    log: list[str] = []
    for name, live in (("db.sqlite", paths.db_path), ("tasks-index.sqlite", paths.tasks_db)):
        bf = backup_dir / name
        if not bf.is_file():
            log.append(f"{name}: 备份副本缺失 — 跳过")
            continue
        for suffix in ("-wal", "-shm"):
            sc = Path(str(live) + suffix)
            if sc.exists():
                sc.unlink()
                log.append(f"已删除过期的 sidecar {sc.name}")
        shutil.copy2(bf, live)
        log.append(f"已还原 {live}")
        bwal = backup_dir / (name + "-wal")
        if bwal.is_file():
            shutil.copy2(bwal, Path(str(live) + "-wal"))
            log.append("已还原 -wal sidecar")
    for r in records:
        src = backup_dir / r.backup_rel
        dst = Path(r.source)
        if not src.exists():
            continue
        if r.is_dir:
            _copy_dir(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        log.append(f"已还原 {dst}")
    return log


# ---------------------------------------------------------------- 会话存储


@dataclass
class SessionSummary:
    id: str
    title: str = ""
    directory: str = ""
    parent_id: str | None = None
    created_ms: int = 0
    updated_ms: int = 0
    message_count: int | None = None
    child_count: int = 0
    archived: bool = False
    pinned: bool = False
    ghost: bool = False
    disk_bytes: int = 0


@dataclass
class Msg:
    id: str
    role: str | None
    visible: bool
    source: str
    parts: list


SYNTHETIC_SOURCES = {
    "todo_reminder", "compaction", "queued_input", "background_notification",
    "system", "tool_result_auto", "context_snapshot",
}


class DeleteRefused(Exception):
    """携带 machine code + 人类可读细节的删除拒绝。"""

    def __init__(self, code: str, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.problems = problems or []


class Store:
    def __init__(self, paths: Paths, backups_dir: str | None = None):
        self.paths = paths
        self.backups_dir = backups_dir

    # ---------- 读取 ----------

    def index_flags(self) -> dict[str, tuple[bool, bool, int]] | None:
        if not self.paths.tasks_db.is_file():
            return None
        try:
            con = open_ro(self.paths.tasks_db)
        except sqlite3.Error:
            return None
        try:
            if not has_table(con, "tasks"):
                return None
            cols = columns(con, "tasks")
            if "archived" not in cols or "pinned" not in cols:
                return None
            has_updated = "updated_at" in cols
            sql = ("SELECT task_id, archived, pinned, updated_at FROM tasks" if has_updated
                   else "SELECT task_id, archived, pinned, 0 FROM tasks")
            out = {}
            for tid, a, p, u in con.execute(sql):
                out[tid] = (bool(a), bool(p), col_ms(u) if has_updated else 0)
            return out
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def list_sessions(self, with_disk: bool = True) -> list[SessionSummary]:
        con = open_ro(self.paths.db_path)
        out: list[SessionSummary] = []
        try:
            rows = con.execute(
                "SELECT s.id, s.title, s.directory, s.parent_id, s.time_created, s.time_updated,"
                " (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id),"
                " (SELECT COUNT(*) FROM session c WHERE c.parent_id = s.id)"
                " FROM session s").fetchall()
        finally:
            con.close()
        for sid, title, directory, parent_id, tc, tu, mc, cc in rows:
            out.append(SessionSummary(
                id=sid, title=title or "", directory=directory or "",
                parent_id=parent_id, created_ms=col_ms(tc), updated_ms=col_ms(tu),
                message_count=mc, child_count=cc or 0))
        by_id = {s.id: s for s in out}
        flags = self.index_flags() or {}
        for tid, (archived, pinned, updated) in flags.items():
            s = by_id.get(tid)
            if s:
                s.archived, s.pinned = archived, pinned
                if updated and not s.updated_ms:
                    s.updated_ms = updated
            else:
                g = SessionSummary(id=tid, archived=archived, pinned=pinned,
                                   updated_ms=updated, ghost=True)
                out.append(g)
                by_id[tid] = g
        for s in out:
            s.disk_bytes = disk_usage(self.paths.cli_dir, s.id) if with_disk else 0
        return out

    def resolve_ids(self, inputs: list[str]) -> list[str]:
        """精确匹配优先；否则唯一前缀（允许省略 sess_ 前缀）。有歧义或无匹配则抛 DeleteRefused。"""
        known = {s.id for s in self.list_sessions(with_disk=False)}
        resolved: list[str] = []
        for raw in inputs:
            if raw in known:
                resolved.append(raw)
                continue
            hits = sorted(k for k in known
                          if k.startswith(raw) or k.removeprefix("sess_").startswith(raw))
            if not hits:
                raise DeleteRefused("no_match", f"找不到会话：{raw}")
            if len(hits) > 1:
                raise DeleteRefused(
                    "ambiguous", f"前缀 {raw} 有歧义，候选：{', '.join(hits)}")
            resolved.append(hits[0])
        return list(dict.fromkeys(resolved))

    def session_detail(self, sid: str) -> dict | None:
        con = open_ro(self.paths.db_path)
        try:
            meta = con.execute(
                "SELECT id, title, directory, task_type, time_created, time_updated"
                " FROM session WHERE id = ?", (sid,)).fetchone()
            if not meta:
                return None
            parts_by_msg: dict[str, list] = {}
            for mid, data in con.execute(
                    "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY sequence",
                    (sid,)):
                try:
                    parts_by_msg.setdefault(mid, []).append(json.loads(data))
                except json.JSONDecodeError:
                    pass
            messages = []
            for mid, data in con.execute(
                    "SELECT id, data FROM message WHERE session_id = ? ORDER BY sequence", (sid,)):
                try:
                    d = json.loads(data)
                except json.JSONDecodeError:
                    d = {}
                role = d.get("role")
                sem = (d.get("semantics") or {}).get("origin", "")
                src = d.get("source", "")
                visible = True
                if role == "user":
                    visible = (sem == "real_user"
                               or (src not in SYNTHETIC_SOURCES and sem != "agent_runtime"))
                messages.append(Msg(mid, role, visible,
                                    src if src else sem,
                                    parts_by_msg.pop(mid, [])))
            return {"id": meta[0], "title": meta[1] or "", "directory": meta[2] or "",
                    "task_type": meta[3] or "", "created_ms": col_ms(meta[4]),
                    "updated_ms": col_ms(meta[5]), "messages": messages}
        finally:
            con.close()

    # ---------- 级联与计划 ----------

    def cascade_ids(self, roots: list[str]) -> list[str]:
        con = open_ro(self.paths.db_path)
        all_ids: list[str] = []
        seen: set[str] = set()
        frontier = list(roots)
        try:
            while frontier:
                batch = [i for i in frontier if i not in seen]
                if not batch:
                    break
                seen.update(batch)
                all_ids.extend(batch)
                ph = qm(len(batch))
                frontier = [r[0] for r in con.execute(
                    f"SELECT id FROM session WHERE parent_id IN ({ph})", batch)]
        finally:
            con.close()
        return all_ids

    def plan_delete(self, roots: list[str]) -> dict:
        all_ids = self.cascade_ids(roots)
        con = open_ro(self.paths.db_path)
        counts: list[dict] = []
        try:
            tables = existing_tables(con)
            if not all_ids:
                metas = []
            else:
                ph = qm(len(all_ids))
                for t in CHILD_TABLES:
                    if t in tables:
                        n = con.execute(
                            f"SELECT COUNT(*) FROM {t} WHERE session_id IN ({ph})", all_ids
                        ).fetchone()[0]
                        counts.append({"table": t, "rows": n})
                if "session_task_link" in tables:
                    n = con.execute(
                        f"SELECT COUNT(*) FROM session_task_link WHERE parent_session_id IN ({ph})"
                        f" OR child_session_id IN ({ph})", all_ids + all_ids).fetchone()[0]
                    counts.append({"table": "session_task_link", "rows": n})
                if "workflow_run" in tables:
                    run_ids = [r[0] for r in con.execute(
                        f"SELECT id FROM workflow_run WHERE parent_session_id IN ({ph})", all_ids)]
                    if run_ids:
                        counts.append({"table": "workflow_run", "rows": len(run_ids)})
                        ph2 = qm(len(run_ids))
                        for t in ("workflow_event", "workflow_activity"):
                            if t in tables:
                                n = con.execute(
                                    f"SELECT COUNT(*) FROM {t} WHERE run_id IN ({ph2})",
                                    run_ids).fetchone()[0]
                                counts.append({"table": t, "rows": n})
                if "dwf_run" in tables:
                    dwf_ids = [r[0] for r in con.execute(
                        f"SELECT id FROM dwf_run WHERE parent_session_id IN ({ph})", all_ids)]
                    if dwf_ids:
                        ph2 = qm(len(dwf_ids))
                        for t in ("dwf_node", "dwf_event"):
                            if t in tables:
                                n = con.execute(
                                    f"SELECT COUNT(*) FROM {t} WHERE run_id IN ({ph2})",
                                    dwf_ids).fetchone()[0]
                                counts.append({"table": t, "rows": n})
                        counts.append({"table": "dwf_run", "rows": len(dwf_ids)})
                        if "dwf_actor" in tables:
                            if dwf_ids:
                                n = con.execute(
                                    f"SELECT COUNT(*) FROM dwf_actor WHERE run_id IN ({ph2})"
                                    f" OR session_id IN ({ph})", dwf_ids + all_ids).fetchone()[0]
                            else:
                                n = con.execute(
                                    f"SELECT COUNT(*) FROM dwf_actor WHERE session_id IN ({ph})",
                                    all_ids).fetchone()[0]
                            counts.append({"table": "dwf_actor", "rows": n})
                n = con.execute(
                    f"SELECT COUNT(*) FROM session WHERE id IN ({ph})", all_ids).fetchone()[0]
                counts.append({"table": "session", "rows": n})
                titles = {r[0]: r[1] or "" for r in con.execute(
                    f"SELECT id, title FROM session WHERE id IN ({ph})", all_ids)}
        finally:
            con.close()
        flags = self.index_flags() or {}
        metas = [{"id": i, "title": titles.get(i, ""), "ghost": i not in titles,
                  "archived": flags.get(i, (False,))[0]} for i in all_ids]
        disk_targets_detail: list[dict] = []
        disk_bytes = 0
        for i in all_ids:
            for t in disk_targets(self.paths.cli_dir, i):
                b, _f = walk_size(t)
                exists = t.exists() or t.is_symlink()
                disk_bytes += b
                if exists:
                    disk_targets_detail.append({
                        "session": i, "path": str(t),
                        "rel": t.relative_to(self.paths.cli_dir).as_posix(),
                        "bytes": b})
        return {"roots": roots, "all_ids": all_ids, "metas": metas, "counts": counts,
                "disk_bytes": disk_bytes, "disk_targets": disk_targets_detail,
                "backups_dir": str(self.paths.backups_base(self.backups_dir))}

    # ---------- 删除 ----------

    def _limited_violations(self, all_ids: list[str], roots: list[str],
                            idle_minutes: int) -> list[tuple[str, str]]:
        violations: list[tuple[str, str]] = []
        updated: dict[str, int] = {}
        con = open_ro(self.paths.db_path)
        try:
            ph = qm(len(all_ids))
            for sid, tu in con.execute(
                    f"SELECT id, time_updated FROM session WHERE id IN ({ph})", all_ids):
                updated[sid] = col_ms(tu)
        finally:
            con.close()
        idx = self.index_flags() or {}
        auto_refs: set[str] = set()
        if self.paths.tasks_db.is_file():
            try:
                tcon = open_ro(self.paths.tasks_db)
                try:
                    if has_table(tcon, "automations"):
                        cols = columns(tcon, "automations")
                        sql = ("SELECT target_task_id FROM automations WHERE enabled = 1"
                               if "enabled" in cols else "SELECT target_task_id FROM automations")
                        try:
                            for (tid,) in tcon.execute(sql):
                                if tid:
                                    auto_refs.add(tid)
                        except sqlite3.Error:
                            pass
                finally:
                    tcon.close()
            except sqlite3.Error:
                pass
        cutoff = int(_dt.datetime.now().timestamp() * 1000) - idle_minutes * 60_000
        roots_set = set(roots)
        for sid in all_ids:
            archived = idx.get(sid, (False,))[0]
            effective = updated.get(sid) or idx.get(sid, (False, False, 0))[2] or 0
            if effective > cutoff:
                violations.append((sid, "too_recent"))
                continue
            if sid in auto_refs:
                violations.append((sid, "automation_ref"))
                continue
            if sid in roots_set and not archived:
                violations.append((sid, "not_archived"))
        return violations

    def execute_delete(self, roots: list[str], running_policy: str = "refuse",
                       idle_minutes: int = 60, vacuum: bool = True) -> dict:
        """running_policy: refuse | limited。"""
        report = compat_check(self.paths)
        if not report.ok:
            raise DeleteRefused("compat", "数据库结构与预期不符，拒绝操作", report.problems)
        if not self.paths.db_path.is_file():
            raise DeleteRefused("db_not_found", f"找不到 {self.paths.db_path}")

        for name, p in (("db.sqlite", self.paths.db_path),
                        ("tasks-index.sqlite", self.paths.tasks_db)):
            state, detail = integrity_check(p)
            if state != "ok":
                raise DeleteRefused("corruption", f"{name} 完整性检查未通过：{detail}")

        plan = self.plan_delete(roots)
        if not plan["all_ids"]:
            raise DeleteRefused("no_match", "没有匹配的会话")

        running = zcode_running(self.paths)
        if running is None:
            raise DeleteRefused(
                "probe_unavailable",
                "无法探测 ZCode 是否正在运行（本机无 psutil 且跨端 tasklist 不可用），拒绝删除。"
                "请安装 psutil 或在能探测进程的环境执行。")
        if running:
            if running_policy == "refuse":
                raise DeleteRefused("zcode_running",
                                    "检测到 ZCode 正在运行，默认拒绝删除。请完全退出 ZCode 后"
                                    "重试，或使用 --limited 受限模式。")
            violations = self._limited_violations(plan["all_ids"], plan["roots"], idle_minutes)
            if violations:
                detail = "; ".join(f"{i} ({r})" for i, r in violations)
                raise DeleteRefused("limited",
                                    f"受限模式下以下会话不可删除：{detail}")

        backup_dir = new_backup_dir(self.paths.backups_base(self.backups_dir))
        copy_database(self.paths.db_path, backup_dir)
        copy_database(self.paths.tasks_db, backup_dir)
        records = copy_session_disk(self.paths.cli_dir, plan["all_ids"], backup_dir)
        write_manifest(backup_dir, {
            "createdAt": _dt.datetime.now().isoformat(timespec="seconds"),
            "roots": plan["roots"], "allIds": plan["all_ids"],
            "dbPath": str(self.paths.db_path), "tasksDb": str(self.paths.tasks_db),
            "cliDir": str(self.paths.cli_dir),
            "disk": [r.__dict__ for r in records],
        })

        deleted = self._delete_content(plan["all_ids"])
        targets = [t for i in plan["all_ids"] for t in disk_targets(self.paths.cli_dir, i)]
        files, bytes_freed, errors = disk_remove(targets)
        index, warnings = self._delete_index(plan["all_ids"])

        if vacuum:
            for p in (self.paths.db_path, self.paths.tasks_db):
                if p.is_file():
                    try:
                        con = open_rw(p)
                        con.execute("VACUUM")
                        con.close()
                    except sqlite3.Error:
                        warnings.append(f"VACUUM 失败：{p.name}")

        integrity: dict = {"passed": True, "restored": False,
                           "details": [], "restore_log": [], "restore_errors": []}
        after = [(n, p, *integrity_check(p)) for n, p in
                 (("db.sqlite", self.paths.db_path),
                  ("tasks-index.sqlite", self.paths.tasks_db))]
        integrity["details"] = [{"db": n, "state": s, "detail": d} for n, p, s, d in after]
        if any(s != "ok" for _, _, s, _ in after):
            try:
                integrity["restore_log"] = restore_from_backup(backup_dir, self.paths, records)
                integrity["restored"] = True
            except OSError as e:
                integrity["restore_errors"].append(str(e))
            after = [(n, p, *integrity_check(p)) for n, p in
                     (("db.sqlite", self.paths.db_path),
                      ("tasks-index.sqlite", self.paths.tasks_db))]
            integrity["details"] = [{"db": n, "state": s, "detail": d} for n, p, s, d in after]
            integrity["passed"] = all(s == "ok" for _, _, s, _ in after)
            if integrity["restored"] and not integrity["passed"]:
                integrity["restore_errors"].append("还原已完成但完整性仍不通过")

        return {"backup_dir": str(backup_dir), "roots": plan["roots"],
                "all_ids": plan["all_ids"], "deleted": deleted,
                "disk": {"files": files, "bytes": bytes_freed, "errors": errors},
                "index": index, "warnings": warnings, "integrity": integrity}

    def _delete_content(self, ids: list[str]) -> list[dict]:
        con = open_rw(self.paths.db_path)
        out: list[dict] = []
        try:
            tables = existing_tables(con)
            con.execute("BEGIN IMMEDIATE")
            ph = qm(len(ids))
            for t in CHILD_TABLES:
                if t in tables:
                    n = con.execute(f"DELETE FROM {t} WHERE session_id IN ({ph})", ids).rowcount
                    out.append({"table": t, "rows": max(n, 0)})
            if "session_task_link" in tables:
                n = con.execute(
                    f"DELETE FROM session_task_link WHERE parent_session_id IN ({ph})"
                    f" OR child_session_id IN ({ph})", ids + ids).rowcount
                out.append({"table": "session_task_link", "rows": max(n, 0)})
            if "workflow_run" in tables:
                run_ids = [r[0] for r in con.execute(
                    f"SELECT id FROM workflow_run WHERE parent_session_id IN ({ph})", ids)]
                if run_ids:
                    ph2 = qm(len(run_ids))
                    for t in ("workflow_event", "workflow_activity"):
                        if t in tables:
                            n = con.execute(f"DELETE FROM {t} WHERE run_id IN ({ph2})",
                                            run_ids).rowcount
                            out.append({"table": t, "rows": max(n, 0)})
                    n = con.execute(f"DELETE FROM workflow_run WHERE id IN ({ph2})",
                                    run_ids).rowcount
                    out.append({"table": "workflow_run", "rows": max(n, 0)})
            if "dwf_run" in tables:
                dwf_ids = [r[0] for r in con.execute(
                    f"SELECT id FROM dwf_run WHERE parent_session_id IN ({ph})", ids)]
                if dwf_ids:
                    ph2 = qm(len(dwf_ids))
                    for t in ("dwf_node", "dwf_event"):
                        if t in tables:
                            n = con.execute(f"DELETE FROM {t} WHERE run_id IN ({ph2})",
                                            dwf_ids).rowcount
                            out.append({"table": t, "rows": max(n, 0)})
                if "dwf_actor" in tables:
                    if dwf_ids:
                        n = con.execute(
                            f"DELETE FROM dwf_actor WHERE run_id IN ({qm(len(dwf_ids))})"
                            f" OR session_id IN ({ph})", dwf_ids + ids).rowcount
                    else:
                        n = con.execute(
                            f"DELETE FROM dwf_actor WHERE session_id IN ({ph})", ids).rowcount
                    out.append({"table": "dwf_actor", "rows": max(n, 0)})
                if dwf_ids:
                    n = con.execute(f"DELETE FROM dwf_run WHERE id IN ({qm(len(dwf_ids))})",
                                    dwf_ids).rowcount
                    out.append({"table": "dwf_run", "rows": max(n, 0)})
            n = con.execute(f"DELETE FROM session WHERE id IN ({ph})", ids).rowcount
            out.append({"table": "session", "rows": max(n, 0)})
            con.execute("COMMIT")
            checkpoint(con)
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        return out

    def _delete_index(self, ids: list[str]) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        if not self.paths.tasks_db.is_file():
            warnings.append("tasks-index 缺失 — 跳过索引清理")
            return [], warnings
        con = open_rw(self.paths.tasks_db)
        out: list[dict] = []
        try:
            tables = existing_tables(con)
            con.execute("BEGIN IMMEDIATE")
            ph = qm(len(ids))
            for t, col in (("task_group_members", "task_id"),
                           ("automation_runs", "session_id"),
                           ("off_peak_tasks", "session_id")):
                if t in tables:
                    n = con.execute(f"DELETE FROM {t} WHERE {col} IN ({ph})", ids).rowcount
                    out.append({"table": t, "rows": max(n, 0)})
            if "automations" in tables:
                try:
                    n = con.execute(
                        f"SELECT COUNT(*) FROM automations WHERE target_task_id IN ({ph})",
                        ids).fetchone()[0]
                except sqlite3.Error:
                    n = 0
                if n:
                    warnings.append(f"{n} 条自动化配置仍引用被删除的会话")
            if "tasks" in tables:
                n = con.execute(f"DELETE FROM tasks WHERE task_id IN ({ph})", ids).rowcount
                out.append({"table": "tasks", "rows": max(n, 0)})
            con.execute("COMMIT")
            checkpoint(con)
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        return out, warnings


# ---------------------------------------------------------------- 渲染


def _c() -> "Console":
    import sys as _sys
    if not _sys.stdout.isatty():
        return Console(width=200, highlight=False)
    return Console(highlight=False)


def print_sessions(sessions: list[SessionSummary], active_only=False, archived_only=False):
    rows = []
    for s in sorted(sessions, key=lambda x: -x.updated_ms):
        if active_only and (s.archived or s.ghost):
            continue
        if archived_only and not s.archived:
            continue
        rows.append(s)
    flags_map = {(False, False): "", (True, False): "已归档", (False, True): "置顶",
                 (True, True): "归档+置顶"}
    if _rich:
        console = _c()
        table = Table(box=None, header_style="bold")
        for col in {"更新时间": 17, "ID": 22, "标题": 40, "状态": 10, "消息": 6,
                    "子会话": 8, "磁盘": 9}.items():
            table.add_column(col[0], max_width=col[1], no_wrap=True)
        for s in rows:
            flags = "ghost" if s.ghost else flags_map[(s.archived, s.pinned)]
            prefix = "↳ " if s.parent_id else ""
            title = (s.title or ("<ghost>" if s.ghost else "-"))
            table.add_row(fmt_time(s.updated_ms), s.id.removeprefix("sess_")[:18],
                          prefix + title, flags,
                          "-" if s.message_count is None else str(s.message_count),
                          str(s.child_count) if s.child_count else "-", fmt_bytes(s.disk_bytes))
        console.print(table)
    else:
        for s in rows:
            flags = "ghost" if s.ghost else flags_map[(s.archived, s.pinned)]
            print(f"{fmt_time(s.updated_ms)}  {s.id}  [{flags}] {s.title}  "
                  f"msgs={s.message_count} disk={fmt_bytes(s.disk_bytes)}")
    print(f"\n共 {len(rows)} 个会话。")


def _excerpt(text, limit=300) -> str:
    if text is None:
        return ""
    t = str(text).replace("\n", " ").strip()
    return t if len(t) <= limit else t[:limit] + f" …(截断，共{len(t)}字符)"


def print_detail(detail: dict, full: bool = False):
    limit = None if full else 2000
    if _rich:
        _c().print(f"[bold]{detail['title']}[/bold]  ({detail['id']})\n"
                   f"目录: {detail['directory']}\n"
                   f"创建: {fmt_time(detail['created_ms'])}  "
                   f"更新: {fmt_time(detail['updated_ms'])}\n")
    else:
        print(detail["title"], f"({detail['id']})")
    for i, m in enumerate(detail["messages"], 1):
        tag = m.role or "?"
        mark = "" if m.visible else " [隐藏]"
        if _rich:
            _c().print(f"\n[bold cyan]#{i} {tag}{mark}[/bold cyan] [dim]({m.source})[/dim]")
        else:
            print(f"\n#{i} {tag}{mark} ({m.source})")
        for p in m.parts:
            t = p.get("type")
            if t == "text":
                body = _excerpt(p.get("text"), limit)
                if body:
                    print(f"  {body}")
            elif t == "reasoning":
                body = _excerpt(p.get("text"), 300)
                print(f"  [思考] {body}")
            elif t == "tool":
                st = p.get("state") or {}
                line = f"  [工具] {p.get('tool')} ({st.get('status')})"
                inp = (st.get("input") or {})
                brief = inp.get("description") or inp.get("command") or inp.get("file_path") or ""
                if brief:
                    line += f" {_excerpt(brief, 200)}"
                print(line)
            elif t in ("step-start", "step-finish"):
                continue
            elif t == "timeline":
                print(f"  [时间线] {_excerpt(p.get('timelineType'), 80)}")
            else:
                print(f"  [{t}]")


def print_plan(plan: dict):
    print(f"根会话: {', '.join(plan['roots'])}")
    print(f"级联展开后共 {len(plan['all_ids'])} 个会话：")
    for m in plan["metas"]:
        flags = []
        if m["ghost"]:
            flags.append("ghost")
        if m["archived"]:
            flags.append("已归档")
        print(f"  {m['id']}  {'(' + ','.join(flags) + ')' if flags else ''} {m['title']}")
    print("\n将删除的数据库行：")
    for c in plan["counts"]:
        print(f"  {c['table']}: {c['rows']}")
    print("\n将删除的磁盘文件/目录（exec / artifacts / rollout 等执行缓存）：")
    if plan["disk_targets"]:
        for d in plan["disk_targets"]:
            print(f"  [{d['session'].removeprefix('sess_')[:12]}] {d['rel']}  ({fmt_bytes(d['bytes'])})")
    else:
        print("  （这些会话在磁盘上没有残留文件）")
    print(f"\n磁盘将释放: {fmt_bytes(plan['disk_bytes'])}")
    print(f"备份目录: {plan['backups_dir']}")
    print("\n以上为 dry-run 预览，未改动任何数据。确认无误后加 --yes 执行删除。")


def print_delete_result(res: dict):
    print(f"备份目录: {res['backup_dir']}")
    print("数据库删除行数:")
    for c in res["deleted"]:
        print(f"  {c['table']}: {c['rows']}")
    print(f"索引删除行数: {json.dumps(res['index'], ensure_ascii=False)}")
    d = res["disk"]
    print(f"磁盘清理: {d['files']} 个文件 / {fmt_bytes(d['bytes'])}"
          + (f"，错误: {d['errors']}" if d["errors"] else ""))
    for w in res["warnings"]:
        print(f"警告: {w}")
    it = res["integrity"]
    if it["passed"]:
        print("完整性校验: 通过")
    else:
        print(f"完整性校验: 未通过！restored={it['restored']}")
        for e in it["restore_errors"]:
            print(f"  还原错误: {e}")
        for l in it["restore_log"]:
            print(f"  {l}")


# ---------------------------------------------------------------- CLI


def _resolve_dir(args) -> Paths:
    if args.dir:
        d = Path(args.dir).expanduser()
        if not looks_valid(d):
            raise DeleteRefused("invalid_dir",
                                f"{d} 不像 ZCode 数据目录（需要 cli/db/db.sqlite 或 "
                                f"v2/tasks-index.sqlite）")
        return Paths.from_dir(d)
    p = Paths.default()
    if p is None:
        raise DeleteRefused("no_dir", "在 ~ 下未找到 ~/.zcode，请用 --dir 指定数据目录")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="zsm", description="ZCode 会话管理器（CLI）— 浏览并彻底删除 ZCode 历史会话",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[1] if __doc__ and "用法" in __doc__ else None)
    ap.add_argument("--dir", help="ZCode 数据目录（默认 ~/.zcode）")
    ap.add_argument("--backups-dir", help="备份目录（默认 <zcode>/zsm-backups）")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="列出全部会话（含已归档/ghost）")
    sp.add_argument("--active-only", action="store_true", help="只显示未归档的")
    sp.add_argument("--archived-only", action="store_true", help="只显示已归档的")
    sp.add_argument("--fast", action="store_true",
                    help="跳过每会话磁盘占用统计（跨端 /mnt/... 时显著提速）")

    sp = sub.add_parser("show", help="只读浏览某会话的完整消息流")
    sp.add_argument("id", help="会话 ID 或唯一前缀")
    sp.add_argument("--full", action="store_true", help="不截断正文")

    sp = sub.add_parser("plan", help="删除计划（dry-run）")
    sp.add_argument("ids", nargs="+", help="会话 ID 或唯一前缀")

    sp = sub.add_parser("delete", help="备份 → 删除 → 校验（失败自动还原）")
    sp.add_argument("ids", nargs="+", help="会话 ID 或唯一前缀")
    sp.add_argument("--yes", action="store_true", help="确认执行（必填）")
    sp.add_argument("--limited", action="store_true",
                    help="ZCode 运行时的受限模式（已归档+闲置+未被自动化引用）")
    sp.add_argument("--idle-minutes", type=int, default=60,
                    help="受限模式的闲置阈值（分钟，默认 60）")
    sp.add_argument("--no-vacuum", action="store_true", help="删除后不执行 VACUUM")

    sub.add_parser("compat", help="数据库结构兼容检查（只读）")
    sub.add_parser("integrity", help="两个库的完整性检查（只读）")
    sub.add_parser("selftest", help="在临时目录构造伪数据目录做全流程自检（不碰真实数据）")

    args = ap.parse_args(argv)

    try:
        return _dispatch(args)
    except DeleteRefused as e:
        err = f"[{e.code}] {e.message}"
        if e.problems:
            err += "\n  - " + "\n  - ".join(e.problems)
        print(err, file=sys.stderr)
        return 2


def _dispatch(args) -> int:
    if args.cmd == "selftest":
        return _selftest()

    paths = _resolve_dir(args)

    if args.cmd == "compat":
        r = compat_check(paths)
        if args.json:
            print(json.dumps(r.__dict__, ensure_ascii=False, indent=2))
        else:
            print(f"db.sqlite: {'存在' if r.db_present else '缺失'}，"
                  f"tasks-index.sqlite: {'存在' if r.tasks_present else '缺失'}")
            for p in r.problems:
                print(f"问题: {p}")
            for w in r.warnings:
                print(f"警告: {w}")
            print("结论: " + ("结构兼容，可执行操作" if r.ok else "结构不兼容，禁止破坏性操作"))
        return 0 if r.ok else 1

    if args.cmd == "integrity":
        results = {p.name: integrity_check(p) for p in (paths.db_path, paths.tasks_db)}
        if args.json:
            print(json.dumps({k: {"state": s, "detail": d} for k, (s, d) in results.items()},
                             ensure_ascii=False, indent=2))
        else:
            for k, (s, d) in results.items():
                print(f"{k}: {s}" + (f" — {d}" if d else ""))
        return 0 if all(s == "ok" for s, _ in results.values()) else 1

    store = Store(paths, args.backups_dir)

    if args.cmd == "list":
        sessions = store.list_sessions(with_disk=not args.fast)
        if args.json:
            print(json.dumps([s.__dict__ for s in
                              sorted(sessions, key=lambda x: -x.updated_ms)],
                             ensure_ascii=False, indent=2))
        else:
            print_sessions(sessions, args.active_only, args.archived_only)
        return 0

    if args.cmd == "show":
        sid = store.resolve_ids([args.id])[0]
        detail = store.session_detail(sid)
        if detail is None:
            print(f"{sid} 在 db.sqlite 中没有正文（可能是 ghost），无可浏览内容")
            return 1
        if args.json:
            print(json.dumps({"id": detail["id"], "title": detail["title"],
                              "directory": detail["directory"],
                              "task_type": detail["task_type"],
                              "created_ms": detail["created_ms"],
                              "updated_ms": detail["updated_ms"],
                              "messages": [{"id": m.id, "role": m.role, "visible": m.visible,
                                            "source": m.source, "parts": m.parts}
                                           for m in detail["messages"]]},
                             ensure_ascii=False, indent=2))
        else:
            print_detail(detail, full=args.full)
        return 0

    if args.cmd == "plan":
        ids = store.resolve_ids(args.ids)
        plan = store.plan_delete(ids)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print_plan(plan)
        return 0

    if args.cmd == "delete":
        if not args.yes:
            print("拒绝执行：delete 需要显式 --yes。请先运行 plan 预览删除范围。", file=sys.stderr)
            return 2
        ids = store.resolve_ids(args.ids)
        res = store.execute_delete(ids, running_policy="limited" if args.limited else "refuse",
                                   idle_minutes=args.idle_minutes, vacuum=not args.no_vacuum)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        report = OUT_DIR / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-delete.json"
        report.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print_delete_result(res)
            print(f"执行报告: {report}")
        return 0 if res["integrity"]["passed"] else 1

    raise AssertionError(args.cmd)


# ---------------------------------------------------------------- 自检


def _selftest() -> int:
    """在系统临时目录构造伪 .zcode，跑通 list/plan/delete/limited/还原 全流程。

    断言通过打印 PASS；任何失败打印 FAIL 并返回 2。绝不触碰真实 ~/.zcode。
    """
    import traceback

    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = ""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({extra})" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="zsm-selftest-"))
    print(f"临时数据目录: {tmp}\n")
    try:
        old = os.environ.get(RUNNING_OVERRIDE_ENV)
        os.environ[RUNNING_OVERRIDE_ENV] = "0"
        try:
            _build_fixture(tmp)
            paths = Paths.from_dir(tmp)
            store = Store(paths)

            print("[1] 兼容与完整性")
            rep = compat_check(paths)
            check("compat ok", rep.ok, "; ".join(rep.problems))
            check("integrity ok", all(integrity_check(p)[0] == "ok"
                                      for p in (paths.db_path, paths.tasks_db)))

            print("[2] list")
            sessions = {s.id: s for s in store.list_sessions()}
            check("3 个会话", len(sessions) == 3)
            check("sessA 已归档", sessions.get("sessAAA") and sessions["sessAAA"].archived)
            check("sessC 未归档", sessions.get("sessCCC") and not sessions["sessCCC"].archived)
            check("sessB 是子会话", sessions.get("sessBBB") and
                  sessions["sessBBB"].parent_id == "sessAAA")
            check("磁盘占用统计>0", sessions["sessAAA"].disk_bytes > 0)

            print("[3] 前缀解析")
            check("唯一前缀", store.resolve_ids(["sessA"]) == ["sessAAA"])
            try:
                store.resolve_ids(["sess"])
                check("歧义前缀报错", False)
            except DeleteRefused as e:
                check("歧义前缀报错", e.code == "ambiguous")

            print("[4] plan（级联）")
            plan = store.plan_delete(["sessAAA"])
            check("级联展开含子会话", set(plan["all_ids"]) == {"sessAAA", "sessBBB"})
            counts = {c["table"]: c["rows"] for c in plan["counts"]}
            check("message 行数", counts.get("message") == 2, str(counts))
            check("part 行数", counts.get("part") == 1, str(counts))
            check("session 行数", counts.get("session") == 2, str(counts))
            disk_rels = {d["rel"] for d in plan["disk_targets"]}
            check("计划列出磁盘缓存", {"exec/sessAAA", "agents/sessAAA",
                                  "exec/bash-startup/sessBBB", "artifacts/sessBBB",
                                  "rollout/model-io-sessAAA.jsonl"} <= disk_rels,
                  str(disk_rels))
            check("计划不含未选会话的缓存",
                  not any("sessCCC" in d["rel"] for d in plan["disk_targets"]))

            print("[5] 受限模式拒绝")
            os.environ[RUNNING_OVERRIDE_ENV] = "1"
            try:
                store.execute_delete(["sessCCC"], running_policy="refuse")
                check("运行中默认拒绝", False)
            except DeleteRefused as e:
                check("运行中默认拒绝", e.code == "zcode_running")
            # 违规检查顺序与原版一致：too_recent 先于 not_archived
            try:
                store.execute_delete(["sessCCC"], running_policy="limited", idle_minutes=60)
                check("闲置不足被拒(未归档)", False)
            except DeleteRefused as e:
                check("闲置不足被拒(未归档)", e.code == "limited"
                      and any(r == "too_recent" for _, r in _viol(e)))
            _set_time(paths, "sessCCC", old=True)
            try:
                store.execute_delete(["sessCCC"], running_policy="limited", idle_minutes=60)
                check("未归档根会话被拒", False)
            except DeleteRefused as e:
                check("未归档根会话被拒", e.code == "limited"
                      and any(r == "not_archived" for _, r in _viol(e)))
            _set_time(paths, "sessCCC", old=False)
            try:
                store.execute_delete(["sessAAA"], running_policy="limited", idle_minutes=60)
                check("闲置不足被拒", False)
            except DeleteRefused as e:
                check("闲置不足被拒", any(r == "too_recent" for _, r in _viol(e)))
            _set_time(paths, "sessAAA", old=True)
            try:
                store.execute_delete(["sessAAA"], running_policy="limited", idle_minutes=60)
                check("自动化引用被拒", False)
            except DeleteRefused as e:
                check("自动化引用被拒", any(r == "automation_ref" for _, r in _viol(e)))

            print("[6] 彻底删除（受限模式放行路径）")
            # a2 引用子会话 sessBBB（随级联删除），应触发警告且配置本身保留
            _drop_automation(paths, "sessAAA")
            os.environ[RUNNING_OVERRIDE_ENV] = "0"
            res = store.execute_delete(["sessAAA"], running_policy="limited",
                                       idle_minutes=60, vacuum=True)
            check("完整性通过", res["integrity"]["passed"])
            con = open_ro(paths.db_path)
            left = {r[0] for r in con.execute("SELECT id FROM session")}
            nmsg = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            con.close()
            check("sessAAA/sessBBB 已删", left == {"sessCCC"})
            check("消息已删", nmsg == 0)
            tcon = open_ro(paths.tasks_db)
            tids = {r[0] for r in tcon.execute("SELECT task_id FROM tasks")}
            grp = tcon.execute("SELECT COUNT(*) FROM task_group_members").fetchone()[0]
            auto = tcon.execute("SELECT COUNT(*) FROM automations").fetchone()[0]
            tcon.close()
            check("tasks 行已删", tids == {"sessCCC"})
            check("task_group_members 已删", grp == 0)
            check("automations 保留", auto == 1)
            check("磁盘文件已删", not (paths.cli_dir / "exec" / "sessAAA").exists()
                  and not (paths.cli_dir / "rollout" / "model-io-sessAAA.jsonl").exists()
                  and not (paths.cli_dir / "artifacts" / "sessBBB").exists())
            check("sessCCC 磁盘保留", (paths.cli_dir / "exec" / "sessCCC").exists())
            check("备份目录存在", Path(res["backup_dir"]).is_dir())
            bdir = Path(res["backup_dir"])
            check("备份含两库", (bdir / "db.sqlite").is_file()
                  and (bdir / "tasks-index.sqlite").is_file())
            check("备份含磁盘文件", (bdir / "disk" / "rollout" / "model-io-sessAAA.jsonl").is_file())
            check("manifest 存在", (bdir / "manifest.json").is_file())
            check("级联会话的自动化触发警告", len(res["warnings"]) == 1
                  and "自动化" in res["warnings"][0], str(res["warnings"]))

            print("[7] 从备份整体还原")
            records = [DiskRecord(**r) for r in json.loads(
                (bdir / "manifest.json").read_text(encoding="utf-8"))["disk"]]
            restore_from_backup(bdir, paths, records)
            con = open_ro(paths.db_path)
            left = {r[0] for r in con.execute("SELECT id FROM session")}
            con.close()
            check("还原后会话回来", left == {"sessAAA", "sessBBB", "sessCCC"})
            check("还原后磁盘回来", (paths.cli_dir / "exec" / "sessAAA").exists())

            print("[8] ghost 会话删除")
            tcon = open_rw(paths.tasks_db)
            # open_rw 是 autocommit 连接，INSERT 立即生效，无需（也不能）显式 COMMIT
            tcon.execute("INSERT INTO tasks (task_id, title, archived, pinned, updated_at)"
                         " VALUES ('sessGHOST', '幽灵', 1, 0, 1)")
            tcon.close()
            store2 = Store(paths)
            listed = {s.id for s in store2.list_sessions()}
            check("ghost 出现在列表", "sessGHOST" in listed)
            res = store2.execute_delete(["sessGHOST"], running_policy="limited", idle_minutes=60)
            tcon = open_ro(paths.tasks_db)
            gone = tcon.execute("SELECT COUNT(*) FROM tasks WHERE task_id='sessGHOST'")\
                .fetchone()[0]
            tcon.close()
            check("ghost 索引行已删", gone == 0 and res["integrity"]["passed"])

            print("[9] 不兼容结构拒绝")
            broken = tmp.parent / (tmp.name + "-broken")
            shutil.copytree(tmp, broken)
            con = open_rw(broken / "cli" / "db" / "db.sqlite")
            con.execute("DROP TABLE part")
            con.close()
            bstore = Store(Paths.from_dir(broken))
            try:
                bstore.execute_delete(["sessCCC"])
                check("缺表拒绝", False)
            except DeleteRefused as e:
                check("缺表拒绝", e.code == "compat")
        finally:
            if old is None:
                os.environ.pop(RUNNING_OVERRIDE_ENV, None)
            else:
                os.environ[RUNNING_OVERRIDE_ENV] = old

        print()
        if failures:
            print(f"自检失败 {len(failures)} 项: {failures}")
            return 2
        print("自检全部通过。")
        return 0
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _viol(e: DeleteRefused) -> list[tuple[str, str]]:
    """从受限模式错误信息里解析 (id, reason) 对。"""
    out = []
    for part in e.message.split("：", 1)[-1].split(";"):
        seg = part.strip()
        if "(" in seg and seg.endswith(")"):
            sid, r = seg[:-1].split(" (", 1)
            out.append((sid.strip(), r.strip()))
    return out


def _set_time(paths: Paths, sid: str, old: bool):
    ms = 1000 if old else int(_dt.datetime.now().timestamp() * 1000)
    con = open_rw(paths.db_path)
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE session SET time_updated = ? WHERE id = ?", (ms, sid))
    con.execute("COMMIT")
    con.close()
    con = open_rw(paths.tasks_db)
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (ms, sid))
    con.execute("COMMIT")
    con.close()


def _drop_automation(paths: Paths, sid: str):
    con = open_rw(paths.tasks_db)
    con.execute("BEGIN IMMEDIATE")
    con.execute("DELETE FROM automations WHERE target_task_id = ?", (sid,))
    con.execute("COMMIT")
    con.close()


def _build_fixture(root: Path):
    cli = root / "cli"
    (cli / "db").mkdir(parents=True)
    (root / "v2").mkdir(parents=True)
    now = int(_dt.datetime.now().timestamp() * 1000)
    con = sqlite3.connect(cli / "db" / "db.sqlite")
    con.executescript("""
    CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, slug TEXT, directory TEXT,
        title TEXT, task_type TEXT, time_created INTEGER, time_updated INTEGER);
    CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT, sequence INTEGER);
    CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT,
        sequence INTEGER);
    CREATE TABLE todo (session_id TEXT, content TEXT, status TEXT, priority TEXT,
        position INTEGER, time_created INTEGER, time_updated INTEGER);
    CREATE TABLE session_entry (id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
        time_created INTEGER, time_updated INTEGER, data TEXT);
    CREATE TABLE session_input (id TEXT PRIMARY KEY, session_id TEXT, kind TEXT);
    CREATE TABLE session_target (session_id TEXT, target_id TEXT);
    CREATE TABLE tool_usage (id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT);
    CREATE TABLE turn_usage (session_id TEXT, turn_id TEXT);
    CREATE TABLE model_usage (id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT);
    CREATE TABLE input_history (id TEXT PRIMARY KEY, project_id TEXT, session_id TEXT, text TEXT);
    CREATE TABLE session_task_link (id TEXT PRIMARY KEY, parent_session_id TEXT,
        child_session_id TEXT, role TEXT);
    CREATE TABLE workflow_run (id TEXT PRIMARY KEY, parent_session_id TEXT, name TEXT);
    CREATE TABLE workflow_event (id TEXT PRIMARY KEY, run_id TEXT);
    CREATE TABLE workflow_activity (id TEXT PRIMARY KEY, run_id TEXT);
    CREATE TABLE dwf_run (id TEXT PRIMARY KEY, parent_session_id TEXT, name TEXT);
    CREATE TABLE dwf_node (id TEXT PRIMARY KEY, run_id TEXT);
    CREATE TABLE dwf_event (id TEXT PRIMARY KEY, run_id TEXT);
    CREATE TABLE dwf_actor (id TEXT PRIMARY KEY, run_id TEXT, session_id TEXT);
    """)
    data = json.dumps({"role": "user", "semantics": {"origin": "real_user"}})
    pdata = json.dumps({"type": "text", "text": "hello"})
    rows = [
        # id, parent, title, archived(在 tasks 里), updated
        ("sessAAA", None, "测试根会话A", 1, now),
        ("sessBBB", "sessAAA", "子会话B", 0, now),
        ("sessCCC", None, "活跃会话C", 0, now),
    ]
    for sid, parent, title, _a, ts in rows:
        con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                    (sid, parent, sid, f"/proj/{sid[-1]}", title, "std", now - 1000, ts))
    con.execute("INSERT INTO message VALUES ('m1','sessAAA',?,1)", (data,))
    con.execute("INSERT INTO message VALUES ('m2','sessAAA',?,2)",
                (json.dumps({"role": "assistant", "semantics": {"origin": "agent_runtime"}}),))
    con.execute("INSERT INTO part VALUES ('p1','m1','sessAAA',?,1)", (pdata,))
    con.execute("INSERT INTO session_task_link VALUES ('l1','sessAAA','sessX','fork')")
    con.execute("INSERT INTO workflow_run VALUES ('w1','sessAAA','wf')")
    con.execute("INSERT INTO workflow_event VALUES ('we1','w1')")
    con.execute("INSERT INTO workflow_activity VALUES ('wa1','w1')")
    con.execute("INSERT INTO dwf_run VALUES ('d1','sessAAA','dwf')")
    con.execute("INSERT INTO dwf_node VALUES ('dn1','d1')")
    con.execute("INSERT INTO dwf_event VALUES ('de1','d1')")
    con.execute("INSERT INTO dwf_actor VALUES ('da1','d1','sessAAA')")
    con.execute("COMMIT")
    con.close()

    tcon = sqlite3.connect(root / "v2" / "tasks-index.sqlite")
    tcon.executescript("""
    CREATE TABLE tasks (workspace_key TEXT, task_id TEXT PRIMARY KEY, title TEXT,
        archived INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, updated_at INTEGER);
    CREATE TABLE task_group_members (group_id TEXT, task_id TEXT);
    CREATE TABLE automations (automation_id TEXT PRIMARY KEY, title TEXT,
        target_task_id TEXT, enabled INTEGER);
    CREATE TABLE automation_runs (run_id TEXT PRIMARY KEY, automation_id TEXT, session_id TEXT);
    CREATE TABLE off_peak_tasks (off_peak_task_id TEXT PRIMARY KEY, session_id TEXT);
    """)
    for sid, parent, title, archived, ts in rows:
        tcon.execute("INSERT INTO tasks VALUES ('wk',?,?,?,?,?)", (sid, title, archived, 0, ts))
    tcon.execute("INSERT INTO task_group_members VALUES ('g1','sessAAA')")
    tcon.execute("INSERT INTO automations VALUES ('a1','每日报','sessAAA',1)")
    tcon.execute("INSERT INTO automations VALUES ('a2','自动二','sessBBB',1)")
    tcon.execute("INSERT INTO automation_runs VALUES ('r1','a1','sessAAA')")
    tcon.execute("COMMIT")
    tcon.close()

    for rel in ("exec/sessAAA/log.txt", "exec/sessCCC/keep.txt",
                "exec/bash-startup/sessBBB/x.sh", "artifacts/sessBBB/out.bin",
                "agents/sessAAA/a.json"):
        p = cli / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"data" * 10)
    (cli / "rollout").mkdir()
    (cli / "rollout" / "model-io-sessAAA.jsonl").write_text("{}\n", encoding="utf-8")
    (cli / "rollout" / "model-io-sessCCC.jsonl").write_text("{}\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
