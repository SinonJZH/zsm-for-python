"""zsm — ZCode 会话管理器（Python）。

浏览 ZCode（智谱 ADE）的全部历史会话（含 UI 上已删除/归档的），并把选定的会话从
ZCode 数据库与磁盘中彻底删除。删除前自动备份，删除后完整性校验，失败自动还原。

包结构：
  zsm.core      纯逻辑：数据目录定位、数据库访问、兼容检查、进程探测、备份还原、
                级联删除（与任何 UI 无关，可被 CLI / WebUI / 其他前端复用）
  zsm.cli       命令行入口（python zsm.py <子命令>）

版权：zcode-session-manager（MIT License, Copyright (c) 2026 wooooooooolf,
https://github.com/woooooooooolf/zcode-session-manager）核心逻辑的 Python 移植
衍生作品，依 MIT 许可保留原版权声明；详见根目录 LICENSE 与 LICENSE.zsm-core。
"""
from .core import (CompatReport, DeleteRefused, Msg, Paths, SessionSummary,
                   Store, compat_check, integrity_check, looks_valid,
                   zcode_running)

__all__ = [
    "CompatReport", "DeleteRefused", "Msg", "Paths", "SessionSummary", "Store",
    "compat_check", "integrity_check", "looks_valid", "zcode_running",
]
