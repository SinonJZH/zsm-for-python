"""终端渲染：rich 表格、会话详情、删除计划与结果输出。"""

from __future__ import annotations

import datetime as _dt
import json

try:
    from rich.console import Console
    from rich.table import Table

    _rich = True
except ImportError:  # pragma: no cover
    _rich = False

from ..core.store import SessionSummary


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


def _c() -> "Console":
    import sys as _sys
    if not _sys.stdout.isatty():
        return Console(width=200, highlight=False)
    return Console(highlight=False)


def status_label(s: SessionSummary) -> str:
    if s.ghost:
        return "ghost"
    parts = []
    if s.deleted:
        parts.append("已删除")
    if s.archived:
        parts.append("已归档")
    if s.pinned:
        parts.append("置顶")
    return " ".join(parts)


def print_sessions(sessions: list[SessionSummary], active_only=False, archived_only=False,
                   deleted_only=False):
    rows = []
    for s in sorted(sessions, key=lambda x: -x.updated_ms):
        if active_only and (s.archived or s.deleted or s.ghost):
            continue
        if archived_only and not s.archived:
            continue
        if deleted_only and not s.deleted:
            continue
        rows.append(s)
    if _rich:
        console = _c()
        table = Table(box=None, header_style="bold")
        for col in {"更新时间": 17, "ID": 22, "标题": 40, "状态": 16, "消息": 6,
                    "子会话": 8, "磁盘": 9}.items():
            table.add_column(col[0], max_width=col[1], no_wrap=True)
        for s in rows:
            prefix = "↳ " if s.parent_id else ""
            title = (s.title or ("<ghost>" if s.ghost else "-"))
            table.add_row(fmt_time(s.updated_ms), s.id.removeprefix("sess_")[:18],
                          prefix + title, status_label(s),
                          "-" if s.message_count is None else str(s.message_count),
                          str(s.child_count) if s.child_count else "-", fmt_bytes(s.disk_bytes))
        console.print(table)
    else:
        for s in rows:
            print(f"{fmt_time(s.updated_ms)}  {s.id}  [{status_label(s)}] {s.title}  "
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


def print_plan(plan: dict, hint: bool = True):
    print(f"根会话: {', '.join(plan['roots'])}")
    print(f"级联展开后共 {len(plan['all_ids'])} 个会话：")
    for m in plan["metas"]:
        flags = []
        if m.get("deleted"):
            flags.append("已删除")
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
    if hint:
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
