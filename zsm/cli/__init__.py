"""zsm.cli — 命令行入口。

用法（python = ~/py_global_venv/bin/python）:
  python zsm.py list                        # 列出全部会话（含已归档/已删除/ghost）
  python zsm.py list --active-only          # 只看未归档且未删除的
  python zsm.py show <id|前缀>              # 只读浏览某会话完整消息流
  python zsm.py plan <id...>                # 删除计划（dry-run，不动任何数据）
  python zsm.py delete <id...> --yes        # 备份 → 删除 → 校验（失败自动还原）
  python zsm.py clean                       # 交互式批量删除：编号选择（1,3,5-8），
                                            #   默认仅已删除会话，-a 连同仅归档的
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
  * ZCode 正在运行 → 默认拒绝；--limited 进入受限模式（仅"已从界面移除（已删除/
    已归档）+ 闲置超过 --idle-minutes（默认 60）+ 未被启用的自动化引用"的会话，
    根会话必须已从界面移除）；
  * 先备份后删除：备份含两个 sqlite 库（连同 -wal）+ 会话磁盘文件 + manifest.json，
    整体还原 = 把备份目录内容拷回原位；
  * 删除后 PRAGMA integrity_check，失败自动从本次备份整体还原。

注意事项:
  * 会话 ID 支持唯一前缀匹配（有歧义时报错并列出候选）。
  * delete 成功后默认对两个库执行 VACUUM 真正回收文件空间（--no-vacuum 跳过）。
  * delete 的执行报告写入仓库根 out/ 目录。
  * 首次正式使用前，建议先造一个无意义的测试会话对其执行删除，确认行为符合预期。
  * 跨端写操作（--dir 指向 /mnt/...）在 ZCode（Windows 侧）完全退出后进行；sqlite
    经 drvfs 跨文件系统写入虽有备份兜底，但更稳妥的做法是把目录复制到本地操作。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..core.compat import compat_check
from ..core.integrity import integrity_check
from ..core.paths import Paths, looks_valid
from ..core.store import DeleteRefused, Store
from .interactive import _cmd_clean, _report_delete
from .render import print_detail, print_plan, print_sessions
from .selftest import _selftest


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
    # 控制台编码不可判定时（如 Windows runner 的 cp1252）不因中文输出崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
                stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(
        prog="zsm", description="ZCode 会话管理器（CLI）— 浏览并彻底删除 ZCode 历史会话",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[1] if __doc__ and "用法" in __doc__ else None)
    ap.add_argument("--dir", help="ZCode 数据目录（默认 ~/.zcode）")
    ap.add_argument("--backups-dir", help="备份目录（默认 <zcode>/zsm-backups）")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="列出全部会话（含已归档/已删除/ghost）")
    sp.add_argument("--active-only", action="store_true", help="只显示未归档且未删除的")
    sp.add_argument("--archived-only", action="store_true", help="只显示已归档的")
    sp.add_argument("--deleted-only", action="store_true",
                    help="只显示已删除的（新版 UI 删除，deleted=1）")
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
                    help="ZCode 运行时的受限模式（已移除+闲置+未被自动化引用）")
    sp.add_argument("--idle-minutes", type=int, default=60,
                    help="受限模式的闲置阈值（分钟，默认 60）")
    sp.add_argument("--no-vacuum", action="store_true", help="删除后不执行 VACUUM")

    sp = sub.add_parser("clean",
                        help="交互式批量删除：列出已删除会话，按编号选择（如 1,3,5-8）")
    sp.add_argument("-a", "--include-archive", action="store_true",
                    help="候选连同仅归档（archived=1，旧版 UI 删除）的会话一起列出")
    sp.add_argument("--limited", action="store_true",
                    help="ZCode 运行时的受限模式（已移除+闲置+未被自动化引用）")
    sp.add_argument("--idle-minutes", type=int, default=60,
                    help="受限模式的闲置阈值（分钟，默认 60）")
    sp.add_argument("--no-vacuum", action="store_true", help="删除后不执行 VACUUM")

    sub.add_parser("compat", help="数据库结构兼容检查（只读）")
    sub.add_parser("integrity", help="两个库的完整性检查（只读）")
    sub.add_parser("selftest", help="在临时目录构造伪数据目录做全流程自检（不碰真实数据）")

    sp = sub.add_parser("webui", help="启动本地 Web 界面（http://127.0.0.1:8765）")
    # 与全局参数同名，便于 `zsm webui --dir X` 的自然写法。
    # default=SUPPRESS：未在子命令位置给值时不动 namespace——否则会把全局位置
    # 传入的 --dir 覆盖回 None（argparse 子解析器默认值会覆盖全局值的经典陷阱）。
    sp.add_argument("--dir", default=argparse.SUPPRESS,
                    help="ZCode 数据目录（默认 ~/.zcode）")
    sp.add_argument("--backups-dir", default=argparse.SUPPRESS,
                    help="备份目录（默认 <zcode>/zsm-backups）")
    sp.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

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
            print_sessions(sessions, args.active_only, args.archived_only, args.deleted_only)
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
        return _report_delete(res, args.json)

    if args.cmd == "clean":
        return _cmd_clean(store, args)

    if args.cmd == "webui":
        from ..webui import serve
        return serve(paths, backups_dir=args.backups_dir, port=args.port,
                     open_browser=not args.no_browser)

    raise AssertionError(args.cmd)
