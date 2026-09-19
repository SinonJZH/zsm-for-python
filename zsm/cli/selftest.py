"""selftest：在临时目录构造伪 .zcode 做全流程自检，绝不触碰真实 ~/.zcode。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sqlite3
import tempfile
import traceback
from pathlib import Path

from ..core.backup import DiskRecord, restore_from_backup
from ..core.compat import compat_check
from ..core.dbutil import open_ro, open_rw
from ..core.integrity import integrity_check
from ..core.paths import Paths
from ..core.probe import RUNNING_OVERRIDE_ENV
from ..core.store import DeleteRefused, Store
from .interactive import _clean_candidates, _parse_selection


def _selftest() -> int:
    """构造伪数据目录，跑通 list/plan/delete/limited/还原 全流程。

    断言通过打印 PASS；任何失败打印 FAIL 并返回 2。
    """
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = ""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({extra})" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="zsm-selftest-"))
    # 测试[9]用的损坏副本（复制主夹具后 DROP 表制造格式不兼容），收尾须一并删除
    broken = tmp.parent / (tmp.name + "-broken")
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
            check("4 个会话", len(sessions) == 4)
            check("sessA 已归档", sessions.get("sessAAA") and sessions["sessAAA"].archived)
            check("sessC 未归档", sessions.get("sessCCC") and not sessions["sessCCC"].archived)
            check("sessD deleted=1", sessions.get("sessDDD") and sessions["sessDDD"].deleted)
            check("sessC 未标删除", sessions["sessCCC"] and not sessions["sessCCC"].deleted)
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

            print("[3b] 选择解析与清理候选")
            check("解析 1,3-5", _parse_selection("1,3-5", 10) == [1, 3, 4, 5])
            check("解析 all", _parse_selection("all", 3) == [1, 2, 3])
            check("解析 q 返回 None", _parse_selection("q", 10) is None)
            try:
                _parse_selection("0,11", 10)
                check("越界编号拒绝", False)
            except ValueError:
                check("越界编号拒绝", True)
            try:
                _parse_selection("1,x", 10)
                check("非法字符拒绝", False)
            except ValueError:
                check("非法字符拒绝", True)
            cands = _clean_candidates(list(sessions.values()))
            check("默认候选=已删除+已归档",
                  {s.id for s in cands} == {"sessAAA", "sessDDD"}, str({s.id for s in cands}))
            cands = _clean_candidates(list(sessions.values()), deleted_only=True)
            check("deleted-only 候选", {s.id for s in cands} == {"sessDDD"})

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

            print("[5b] deleted=1 根会话视为已移除，受限模式可删")
            _set_time(paths, "sessDDD", old=True)
            res_d = store.execute_delete(["sessDDD"], running_policy="limited",
                                         idle_minutes=60, vacuum=False)
            check("sessDDD 删除完成", res_d["integrity"]["passed"])
            con = open_ro(paths.db_path)
            left = {r[0] for r in con.execute("SELECT id FROM session")}
            con.close()
            check("sessDDD 正文已删", left == {"sessAAA", "sessBBB", "sessCCC"})
            check("sessDDD rollout 已删",
                  not (paths.cli_dir / "rollout" / "model-io-sessDDD.jsonl").exists())

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
        shutil.rmtree(broken, ignore_errors=True)


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
    CREATE TABLE turn_usage (id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT);
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
        ("sessDDD", None, "新版UI已删除会话D", 0, now),
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
        archived INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
        updated_at INTEGER);
    CREATE TABLE task_group_members (group_id TEXT, task_id TEXT);
    CREATE TABLE automations (automation_id TEXT PRIMARY KEY, title TEXT,
        target_task_id TEXT, enabled INTEGER);
    CREATE TABLE automation_runs (run_id TEXT PRIMARY KEY, automation_id TEXT, session_id TEXT);
    CREATE TABLE off_peak_tasks (off_peak_task_id TEXT PRIMARY KEY, session_id TEXT);
    """)
    for sid, parent, title, archived, ts in rows:
        tcon.execute("INSERT INTO tasks VALUES ('wk',?,?,?,?,?,?)",
                     (sid, title, archived, 0, 0, ts))
    tcon.execute("UPDATE tasks SET deleted = 1 WHERE task_id = 'sessDDD'")
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
    (cli / "rollout" / "model-io-sessDDD.jsonl").write_text("{}\n", encoding="utf-8")
