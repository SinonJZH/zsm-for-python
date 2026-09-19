"""交互式批量清理（clean 子命令）。"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

from ..core.store import SessionSummary, Store
from .render import fmt_bytes, fmt_time, print_delete_result, print_plan, status_label

# 删除执行报告目录（仓库根下的 out/，可再生数据）。
OUT_DIR = Path(__file__).resolve().parents[2] / "out"


def _parse_selection(text: str, n: int) -> list[int] | None:
    """解析 "1,3,5-8" / "all" / "q"，返回 1-based 编号列表；q/空输入返回 None；非法抛 ValueError。"""
    t = text.strip().lower().replace("，", ",")
    if t in ("", "q", "quit", "exit"):
        return None
    if t == "all":
        return list(range(1, n + 1))
    out: set[int] = set()
    for tok in t.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a_s, b_s = tok.split("-", 1)
            a, b = int(a_s), int(b_s)
            if not (1 <= a <= b <= n):
                raise ValueError(f"编号范围 {tok} 超出 1-{n}")
            out.update(range(a, b + 1))
        else:
            k = int(tok)
            if not (1 <= k <= n):
                raise ValueError(f"编号 {tok} 超出 1-{n}")
            out.add(k)
    if not out:
        raise ValueError("没有选择任何编号")
    return sorted(out)


def _clean_candidates(sessions: list[SessionSummary],
                      include_archived: bool = False) -> list[SessionSummary]:
    """清理候选：已删除（deleted=1）+ ghost；include_archived 时连同仅归档的（旧版 UI 删除=归档）。"""
    return [s for s in sessions
            if s.ghost or s.deleted or (include_archived and s.archived)]


def _report_delete(res: dict, json_mode: bool) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-delete.json"
    report.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    if json_mode:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_delete_result(res)
        print(f"执行报告: {report}")
    return 0 if res["integrity"]["passed"] else 1


def _cmd_clean(store: Store, args) -> int:
    if not sys.stdin.isatty():
        print("clean 是交互式命令，需要在终端中运行（当前输入不是 TTY，已拒绝）。",
              file=sys.stderr)
        return 2
    sessions = store.list_sessions(with_disk=False)
    candidates = _clean_candidates(sessions, args.include_archive)
    if not candidates:
        print("没有找到已删除" + ("或已归档" if args.include_archive else "")
              + "的会话，无需清理。")
        return 0
    candidates.sort(key=lambda x: -x.updated_ms)
    scope = "删除或归档" if args.include_archive else "删除"
    print(f"以下 {len(candidates)} 个会话已从 ZCode 界面移除（{scope}），"
          f"编号仅供参考：\n")
    for i, s in enumerate(candidates, 1):
        title = s.title or ("<ghost>" if s.ghost else "-")
        msgs = s.message_count if s.message_count is not None else "?"
        print(f"{i:4}. [{status_label(s) or '-'}] {title}")
        print(f"      {fmt_time(s.updated_ms)}  消息×{msgs}  {s.id}")
    print()
    while True:
        try:
            raw = input("输入要删除的编号（如 1,3,5-8；all=全部；q=取消）: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1
        try:
            picked = _parse_selection(raw, len(candidates))
            break
        except ValueError as e:
            print(f"  输入无效：{e}（示例：1,3,5-8 / all / q）")
    if picked is None:
        print("已取消，未改动任何数据。")
        return 1
    ids = [candidates[n - 1].id for n in picked]
    plan = store.plan_delete(ids)
    print()
    print_plan(plan, hint=False)
    try:
        confirm = input(f"\n确认彻底删除以上 {len(plan['all_ids'])} 个会话？"
                        f"（删除前备份到 {plan['backups_dir']}）输入 yes 执行: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 1
    if confirm != "yes":
        print("已取消，未改动任何数据。")
        return 1
    res = store.execute_delete(ids, running_policy="limited" if args.limited else "refuse",
                               idle_minutes=args.idle_minutes, vacuum=not args.no_vacuum)
    return _report_delete(res, args.json)
