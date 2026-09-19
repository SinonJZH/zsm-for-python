"""简单拷贝式备份 + 整体还原。

一个备份目录自包含撤销一次删除所需的全部内容：
  <zcode>/zsm-backups/<时间戳>/
    db.sqlite / tasks-index.sqlite (+ -wal 若存在)
    disk/<相对 cli 目录的路径>   每会话磁盘残留
    manifest.json                备了什么、从哪来
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .dbutil import checkpoint, open_rw
from .disk import disk_targets
from .paths import Paths


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
    """拷贝 sqlite 库（尽力先做 WAL checkpoint），连同 -wal sidecar。"""
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
    source: str       # 原始绝对路径
    backup_rel: str   # 备份目录内相对路径
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
    """撤销一次删除：把库与磁盘残留从备份目录拷回原位。返回动作日志。"""
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
