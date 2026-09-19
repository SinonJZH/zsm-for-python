"""zsm.core — ZCode 会话存储访问、兼容检查、备份、级联删除与完整性校验。

本包与任何 UI 无关：CLI、将来的 WebUI 都只是它的薄外壳。
"""
from .compat import CompatReport, compat_check
from .integrity import integrity_check
from .paths import Paths, looks_valid
from .probe import zcode_running
from .store import DeleteRefused, Msg, SessionSummary, Store

__all__ = [
    "CompatReport", "compat_check", "integrity_check", "Paths", "looks_valid",
    "zcode_running", "DeleteRefused", "Msg", "SessionSummary", "Store",
]
