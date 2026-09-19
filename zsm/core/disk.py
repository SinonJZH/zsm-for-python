"""每会话磁盘残留（exec / artifacts / rollout / agents / image-cache / bash-startup）。"""

from __future__ import annotations

import shutil
from pathlib import Path


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
    """删除给定路径，返回 (files_removed, bytes_freed, errors)。"""
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
