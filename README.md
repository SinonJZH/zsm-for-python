# ZCode 会话管理器（Python CLI）

把 ZCode 会话的最终控制权，交还给用户 —— 命令行版。

ZCode 的 UI"删除"有两种历史语义，都只是标记、不清数据：旧版把 `tasks` 表的
`archived` 置 1（归档），新版（2026-09 起）把 `deleted` 置 1（删除）。无论哪种，
会话正文都仍留存在 `cli/db/db.sqlite` 中，磁盘文件（命令输出、模型输入输出记录、
产物缓存）也都还在。本工具识别两种标记，补上缺失的另一半：浏览全部历史会话
（含已删除/已归档），并把选定的会话从 ZCode 数据库与磁盘中**彻底删除**。
删除前自动备份，删除后完整性校验，失败自动还原。

> 本项目是 [zcode-session-manager](https://github.com/woooooooooolf/zcode-session-manager)
> （Tauri/Rust 桌面应用，MIT）核心逻辑的 **Python CLI 移植**。会话管理功能与其对齐；
> 未移植「隐私清理」选项卡。感谢原作者的设计与安全模型。

## 功能

- **全量扫描**：自动定位 ZCode 数据目录（或 `--dir` 手动指定，支持从 WSL 经
  `/mnt/c/...` 管理 Windows 侧数据），列出全部会话的标题、状态（已删除/已归档/置顶/
  ghost/子会话）、消息数与磁盘占用
- **交互式批量清理**（`clean`）：列出已删除/已归档的会话并编号，输入 `1,3,5-8`
  这类选择后走完整的"计划 → 确认 → 备份 → 删除 → 校验"流程
- **Web 界面**（`webui`）：零依赖本地 Web 服务（仅标准库），浏览器里筛选、勾选、
  只读浏览消息流、生成删除计划并确认执行；安全模型见下文
- **会话详情**：只读浏览完整消息流——真实提问、回答、思考过程、工具调用（自动过滤
  合成消息并标注）
- **删除计划**：dry-run 预览级联范围（含子会话）、逐表删除行数与磁盘释放量
- **彻底删除**：单删或批量，自动级联清理数据库正文、原始模型输入输出记录（rollout）、
  执行缓存（exec / artifacts / agents / image-cache / bash-startup）与桌面索引行
- **空间回收**：删除成功后自动 `VACUUM`，真正缩小数据库文件
- **自检**：`selftest` 在临时目录构造伪数据目录跑全流程断言（不碰真实数据），
  ZCode 升级后怀疑格式变化时先跑它

## 安装与使用

依赖：Python 3.10+，`pip install psutil rich`（rich 可选，缺失时降级为纯文本输出）。

```bash
python zsm.py list                  # 列出全部会话（含已删除/已归档/ghost）
python zsm.py list --fast           # 跨端（/mnt/...）时跳过磁盘统计，显著提速
python zsm.py list --deleted-only   # 只看新版 UI 删除（deleted=1）的
python zsm.py show <id|前缀>        # 只读浏览某会话完整消息流
python zsm.py plan <id...>          # 删除计划（dry-run，不动任何数据）
python zsm.py delete <id...> --yes  # 备份 → 删除 → 校验（失败自动还原）
python zsm.py clean                 # 交互式批量删除：编号选择 1,3,5-8 / all / q
python zsm.py webui                 # 本地 Web 界面（http://127.0.0.1:8765）
python zsm.py compat                # 数据库结构兼容检查（只读）
python zsm.py integrity             # 两个库的完整性检查（只读）
python zsm.py selftest              # 临时目录全流程自检
```

会话 ID 支持唯一前缀匹配（可省略 `sess_` 前缀，有歧义时报错并列出候选）。
`--json` 获得机器可读输出；`--backups-dir` 自定义备份目录。
`delete`/`clean` 的执行报告（JSON）写入仓库根 `out/` 目录（可再生数据，已 gitignore）。

## 项目结构

```
zsm.py                  # 薄入口：python zsm.py <子命令>
zsm/
├── core/               # 纯逻辑层（与 UI 无关，可被将来的 WebUI 复用）
│   ├── paths.py        #   数据目录定位与结构校验
│   ├── dbutil.py       #   SQLite 打开/内省工具
│   ├── compat.py       #   格式兼容检查（破坏性操作的安全门）
│   ├── integrity.py    #   PRAGMA integrity_check
│   ├── probe.py        #   ZCode 进程探测（psutil + WSL tasklist.exe）
│   ├── disk.py         #   每会话磁盘残留的统计与删除
│   ├── backup.py       #   备份/还原 + manifest
│   └── store.py        #   会话列表/详情/级联计划/彻底删除
└── cli/                # 命令行层
    ├── render.py       #   rich 表格与输出渲染
    ├── interactive.py  #   clean 交互式批量删除
    ├── selftest.py     #   临时伪数据全流程自检
    └── __init__.py     #   argparse 入口与子命令分发
webui.py / webui.html   # 本地 Web 界面（stdlib http.server + 单文件页面，调 zsm.core）
```

## 安全设计

删除是严肃的操作，本工具用四道防线保证它可控、可逆：

1. **先备份** —— 删除前将两个数据库（连同 `-wal`）与全部会话磁盘文件拷贝到
   `<zcode>/zsm-backups/<时间戳>/`，附 `manifest.json` 记录原始路径，随时可手工还原
2. **后校验** —— 删除完成后自动对两个库执行 `PRAGMA integrity_check`，异常时
   **自动从本次备份整体还原**并明确提示
3. **运行保护** —— 检测到 ZCode 正在运行时默认拒绝删除；`--limited` 受限模式只放行
   「已从界面移除（已删除或已归档）+ 闲置超过阈值（默认 60 分钟）+ 未被启用的自动化
   引用」的会话，且根会话必须已从界面移除。WSL 内操作 `/mnt/c/...` 时自动改用
   `tasklist.exe` 探测 Windows 侧进程，探测手段全部不可用则拒绝执行
4. **格式兼容检查** —— 数据库结构与预期不符时（compat 检查），拒绝一切破坏性操作

WebUI（`zsm webui`）额外加了四层防护：只监听 `127.0.0.1`；校验 Host 头（防 DNS
rebinding）；每次启动生成随机 token，所有删除类 POST 必须携带（同时使浏览器跨站
请求的 CORS 预检必然失败，防恶意网页 CSRF）；会话 ID 严格校验字符集（防路径穿越）。
页面内的删除仍需输入 yes 二次确认，服务端复用与 CLI 完全相同的安全轨。

手工还原方法：完全退出 ZCode，把备份目录里的 `db.sqlite`、`tasks-index.sqlite`
拷回 `<zcode>/cli/db/`、`<zcode>/v2/`（先删除原位的 `-wal`/`-shm`），
`disk/` 下的内容按 `manifest.json` 记录的原始路径拷回。

## 注意事项

- ZCode 官方未来调整会话存储格式的可能性无法排除（届时 compat/selftest 会检测到
  差异并拒绝操作）。**正式使用前，请先创建一个无意义的测试会话并对其执行删除，
  确认行为符合预期后再处理真实数据。**
- 跨端（`--dir` 指向 `/mnt/...`）执行**写操作**时，sqlite 经 drvfs 的锁语义无强保证，
  更稳妥的做法是把数据目录复制到本地操作，或在 Windows 侧直接运行本脚本。
- 本工具只操作本地数据；不影响已同步到云端的任何内容。

## 版权与许可

本项目为 [zcode-session-manager](https://github.com/woooooooooolf/zcode-session-manager)
核心逻辑的 Python 移植衍生作品。依 MIT 许可，原项目的版权声明与许可文本被完整保留：
`LICENSE` 同时载明原作者（Rust 原项目）与本移植作者的版权行，`LICENSE.zsm-core`
为原项目 LICENSE 的原样副本。

以相同方式（MIT）发布。使用风险自负——删除操作虽有多重防护，仍请以备份为最后底线。
