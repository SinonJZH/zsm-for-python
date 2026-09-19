#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""zsm — ZCode 会话管理器（CLI）。

浏览 ZCode（智谱 ADE）的全部历史会话（含 UI 上已删除/归档的），并把选定的会话从
ZCode 数据库与磁盘中彻底删除。删除前自动备份，删除后完整性校验，失败自动还原。

完整用法见 `python zsm.py --help`、README.md 与 zsm/cli/__init__.py 的模块文档。

版权说明：本工具为 zcode-session-manager（MIT License, Copyright (c) 2026 wooooooooolf,
https://github.com/woooooooooolf/zcode-session-manager）核心逻辑的 Python 移植衍生作品，
依 MIT 许可保留原版权声明；原 LICENSE 全文见同目录 LICENSE.zsm-core。
"""

import sys

from zsm.cli import main

if __name__ == "__main__":
    sys.exit(main())
