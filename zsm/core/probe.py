"""ZCode 进程探测。None 表示无法探测——对删除操作等同于拒绝执行。"""

from __future__ import annotations

import os
import shutil
import subprocess

try:
    import psutil
except ImportError:  # pragma: no cover - 环境缺失时只读功能仍可用
    psutil = None

from .paths import Paths

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
