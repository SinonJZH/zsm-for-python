"""会话存储：列表、详情、级联计划与彻底删除。"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass

from .backup import (copy_database, copy_session_disk, new_backup_dir,
                     restore_from_backup, write_manifest)
from .compat import CHILD_TABLES, compat_check
from .dbutil import (checkpoint, col_ms, columns, existing_tables, has_table,
                     open_ro, open_rw, qm)
from .disk import disk_remove, disk_targets, disk_usage, walk_size
from .integrity import integrity_check
from .paths import Paths
from .probe import zcode_running


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
    deleted: bool = False
    ghost: bool = False
    disk_bytes: int = 0


@dataclass
class Msg:
    id: str
    role: str | None
    visible: bool
    source: str
    parts: list


# 合成运行时输入（非真实对话）的 message.source
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

    def index_flags(self) -> dict[str, tuple[bool, bool, bool, int]] | None:
        """task_id → (archived, pinned, deleted, updated_at)。

        deleted 是新版 UI"删除会话"写入的列（旧版只有 archived）；列不存在时按 0 处理，
        保证对旧版数据目录仍然可用。
        """
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
            has_deleted = "deleted" in cols
            sql = (f"SELECT task_id, archived, pinned, {'deleted' if has_deleted else '0'},"
                   f" {'updated_at' if has_updated else '0'} FROM tasks")
            out = {}
            for tid, a, p, d, u in con.execute(sql):
                out[tid] = (bool(a), bool(p), bool(d), col_ms(u) if has_updated else 0)
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
        for tid, (archived, pinned, deleted, updated) in flags.items():
            s = by_id.get(tid)
            if s:
                s.archived, s.pinned, s.deleted = archived, pinned, deleted
                if updated and not s.updated_ms:
                    s.updated_ms = updated
            else:
                g = SessionSummary(id=tid, archived=archived, pinned=pinned, deleted=deleted,
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
        """roots 及其全部子会话（沿 session.parent_id 做 BFS）。"""
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
        titles: dict[str, str] = {}
        try:
            tables = existing_tables(con)
            if all_ids:
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
                  "deleted": flags.get(i, (False, False, False))[2],
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
        """受限模式（ZCode 运行中）下不可删除的会话及原因。"""
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
            deleted = idx.get(sid, (False, False, False))[2]
            effective = updated.get(sid) or idx.get(sid, (False, False, False, 0))[3] or 0
            if effective > cutoff:
                violations.append((sid, "too_recent"))
                continue
            if sid in auto_refs:
                violations.append((sid, "automation_ref"))
                continue
            # 已删除（deleted=1）或已归档均视为"已从界面移除"
            if sid in roots_set and not (archived or deleted):
                violations.append((sid, "not_archived"))
        return violations

    def execute_delete(self, roots: list[str], running_policy: str = "refuse",
                       idle_minutes: int = 60, vacuum: bool = True) -> dict:
        """running_policy: refuse（ZCode 运行中一律拒绝）| limited（受限模式）。"""
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

        # 1. 备份（简单拷贝 + 时间戳目录）
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

        # 2. 正文
        deleted = self._delete_content(plan["all_ids"])

        # 3. 磁盘
        targets = [t for i in plan["all_ids"] for t in disk_targets(self.paths.cli_dir, i)]
        files, bytes_freed, errors = disk_remove(targets)

        # 4. 索引最后删：中途失败只会留下无害孤儿
        index, warnings = self._delete_index(plan["all_ids"])

        # 5. 完整性校验，失败自动从本次备份还原
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
