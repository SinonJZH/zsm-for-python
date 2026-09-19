"""zsm.webui — 本地 Web 界面。

零依赖（仅 Python 标准库）：stdlib http.server 提供只绑定 127.0.0.1 的本地服务，
前端是包内自带的单文件页面（webui.html，原生 JS），API 直调 zsm.core。
安全模型：
  * 只监听 127.0.0.1，不对外网暴露；
  * 校验 Host 头（防 DNS rebinding）；
  * 每次启动生成随机 token，POST 必须携带 X-ZSM-Token 头——同时强制浏览器跨站
    请求的 CORS 预检失败（防恶意网页 CSRF）；
  * 破坏性操作仅接受 POST，且页面内需输入 yes 二次确认；
  * 会话 ID 严格校验字符集（防路径穿越）。
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .core.compat import compat_check
from .core.integrity import integrity_check
from .core.paths import Paths
from .core.probe import zcode_running
from .core.store import DeleteRefused, Store

HTML_PATH = Path(__file__).resolve().parent / "webui.html"
_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _valid_sid(sid) -> str:
    if not isinstance(sid, str) or not _SID_RE.fullmatch(sid):
        raise DeleteRefused("bad_ids", f"非法的会话 ID：{sid!r}")
    return sid


class App:
    """WebUI 后端：一次请求一个纯函数式的 core 调用。"""

    def __init__(self, paths: Paths, backups_dir: str | None, token: str):
        self.paths = paths
        self.store = Store(paths, backups_dir)
        self.token = token
        self._html = HTML_PATH.read_bytes().replace(b"__ZSM_TOKEN__", token.encode())

    def state(self) -> dict:
        compat = compat_check(self.paths)
        return {
            "dir": str(self.paths.zcode_dir),
            "compat": {"ok": compat.ok, "problems": compat.problems},
            "integrity": {p.name: {"state": s, "detail": d}
                          for p, (s, d) in (
                              (self.paths.db_path, integrity_check(self.paths.db_path)),
                              (self.paths.tasks_db, integrity_check(self.paths.tasks_db)))},
            "zcode_running": zcode_running(self.paths),
            "backups_dir": str(self.paths.backups_base(self.store.backups_dir)),
        }

    def sessions(self) -> list[dict]:
        return [s.__dict__ for s in sorted(self.store.list_sessions(),
                                           key=lambda x: -x.updated_ms)]

    def session_detail(self, sid: str) -> dict:
        _valid_sid(sid)
        detail = self.store.session_detail(sid)
        if detail is None:
            raise DeleteRefused("no_match", f"{sid} 在 db.sqlite 中没有正文（可能是 ghost）")
        detail["messages"] = [{"id": m.id, "role": m.role, "visible": m.visible,
                               "source": m.source, "parts": m.parts}
                              for m in detail["messages"]]
        return detail

    def plan(self, ids: list) -> dict:
        return self.store.plan_delete([_valid_sid(i) for i in ids])

    def delete(self, body: dict) -> dict:
        ids = [_valid_sid(i) for i in body.get("ids", [])]
        if not ids:
            raise DeleteRefused("bad_ids", "ids 不能为空")
        idle = body.get("idle_minutes", 60)
        try:
            idle = max(1, int(idle))
        except (TypeError, ValueError):
            idle = 60
        return self.store.execute_delete(
            ids,
            running_policy="limited" if body.get("limited") else "refuse",
            idle_minutes=idle,
            vacuum=bool(body.get("vacuum", True)))


def _host_ok(handler: BaseHTTPRequestHandler) -> bool:
    host = (handler.headers.get("Host") or "").split(":")[0].strip("[]").lower()
    return host in _ALLOWED_HOSTS


def _make_handler(app: App) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默默认访问日志，保持终端干净
            pass

        # ---------- helpers ----------

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _forbidden(self, reason: str):
            self._json({"error": "forbidden", "message": reason}, 403)

        def _refused(self, e: DeleteRefused):
            self._json({"error": e.code, "message": e.message,
                        "problems": e.problems}, 409)

        def _check_host(self) -> bool:
            if _host_ok(self):
                return True
            self._forbidden("Host 头不是 127.0.0.1/localhost（防 DNS rebinding）")
            return False

        def _check_token(self) -> bool:
            if self.headers.get("X-ZSM-Token") == app.token:
                return True
            self._forbidden("缺少或错误的 X-ZSM-Token")
            return False

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 4 * 1024 * 1024:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")

        # ---------- routes ----------

        def do_GET(self):
            if not self._check_host():
                return
            try:
                path = urlparse(self.path).path
                if path == "/":
                    html = app._html
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                elif path == "/api/state":
                    self._json(app.state())
                elif path == "/api/sessions":
                    self._json(app.sessions())
                elif (m := re.fullmatch(r"/api/session/([A-Za-z0-9_-]+)", path)):
                    self._json(app.session_detail(m.group(1)))
                else:
                    self._json({"error": "not_found"}, 404)
            except DeleteRefused as e:
                self._refused(e)
            except Exception as e:  # pragma: no cover
                self._json({"error": "internal", "message": str(e)}, 500)

        def do_POST(self):
            if not self._check_host():
                return
            if not self._check_token():
                return
            try:
                body = self._read_body()
                path = urlparse(self.path).path
                if path == "/api/plan":
                    ids = body.get("ids")
                    if not isinstance(ids, list) or not ids:
                        raise DeleteRefused("bad_ids", "ids 必须是非空数组")
                    self._json(app.plan(ids))
                elif path == "/api/delete":
                    self._json(app.delete(body))
                else:
                    self._json({"error": "not_found"}, 404)
            except DeleteRefused as e:
                self._refused(e)
            except Exception as e:  # pragma: no cover
                self._json({"error": "internal", "message": str(e)}, 500)

    return Handler


def serve(paths: Paths, backups_dir: str | None = None, port: int = 8765,
          open_browser: bool = True) -> int:
    token = secrets.token_urlsafe(16)
    app = App(paths, backups_dir, token)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(app))
    except OSError as e:
        print(f"无法监听 127.0.0.1:{port}（{e}）。请用 --port 换一个端口。", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"ZCode 会话管理器 WebUI 已启动：{url}", flush=True)
    print("数据目录: " + str(paths.zcode_dir), flush=True)
    print("仅监听 127.0.0.1；删除操作需页面内输入 yes 确认。Ctrl+C 退出。", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0
