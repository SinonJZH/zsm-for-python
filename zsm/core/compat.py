"""兼容性门：数据库布局与本工具预期不符时，阻断一切破坏性操作。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .dbutil import columns, existing_tables, has_table, open_ro
from .paths import Paths

# db.sqlite 里按 session_id 删除的子表。运行时缺表可跳过，但**存在**的表必须有 session_id 列。
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
            # dwf_* 工作流日志（ZCode 0.16.5+）：经 dwf_run.parent_session_id /
            # dwf_actor.session_id 关联会话，子表经 run_id 关联。
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
