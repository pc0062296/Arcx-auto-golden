"""本機 Web server。標準庫 http.server, 綁 127.0.0.1。

為什麼不用 FastAPI/Flask: 這是單人、唯讀、只聽 loopback 的儀表板。
標準庫足夠, 而內網環境安裝套件是實實在在的摩擦。零相依也讓
「複製一份程式碼過去就能跑」成立。

安全性質:
  * 預設只綁 127.0.0.1 —— 不對外提供服務, 不需要認證
  * 全部 GET, 沒有任何寫入端點 (Phase 3 的動作會走 commands/ 檔案投遞)
  * 只讀 state root 底下的 state.json, 路徑由 run_id 查表得出而非拼接,
    因此沒有 path traversal 的空間
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from arcx_auto.adapters.store import RunStore
from arcx_auto.web import pages


@dataclass
class WebOptions:
    state_root: str
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_sec: int = 30


class _Handler(BaseHTTPRequestHandler):
    options: WebOptions = WebOptions(state_root="~/.arcx-auto")
    server_version = "arcx-auto"

    # -- 路由 ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的介面
        path = urllib.parse.urlparse(self.path).path
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]

        try:
            if not parts:
                return self._home()
            if parts[0] == "api" and len(parts) == 3 and parts[1] == "state":
                return self._api_state(parts[2])
            if parts[0] == "healthz":
                return self._send_json({"ok": True})
            if parts[0] == "run":
                return self._run_routes(parts[1:])
        except Exception as exc:  # noqa: BLE001 - 一個壞請求不該弄掉 server
            return self._error(500, "內部錯誤: %s" % exc)

        self._error(404, "找不到這個頁面")

    def _run_routes(self, parts: List[str]) -> None:
        if not parts:
            return self._error(404, "缺少 run id")
        run_id = parts[0]
        state = self._load_state(run_id)
        if state is None:
            return self._error(404, "找不到 run: %s" % run_id)

        if len(parts) == 1:
            return self._html(pages.render_run(state, self.options.refresh_sec))

        if len(parts) >= 3 and parts[1] == "index":
            index = _find(state.get("indexes") or [], "index_key", parts[2])
            if index is None:
                return self._error(404, "找不到 index: %s" % parts[2])

            if len(parts) == 3:
                return self._html(
                    pages.render_index(state, index, self.options.refresh_sec))

            if len(parts) == 5 and parts[3] == "case":
                case = _find(index.get("cases") or [], "case_id", parts[4])
                if case is None:
                    return self._error(404, "找不到 case: %s" % parts[4])
                return self._html(pages.render_case(
                    state, index, case, self.options.refresh_sec))

        self._error(404, "找不到這個頁面")

    def _home(self) -> None:
        states = []
        for run_id in RunStore.list_runs(self.options.state_root):
            state = self._load_state(run_id)
            if state:
                state.setdefault("run_id", run_id)
                states.append(state)
        self._html(pages.render_home(states, self.options.refresh_sec))

    def _api_state(self, run_id: str) -> None:
        state = self._load_state(run_id)
        if state is None:
            return self._error(404, "找不到 run: %s" % run_id)
        self._send_json(state)

    # -- 資料 ----------------------------------------------------------

    def _load_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        """只接受確實存在於 state root 底下的 run id。

        用「列出既有 run 再比對」而不是把使用者輸入拼進路徑, 從根本上
        避免 path traversal。
        """
        if run_id not in RunStore.list_runs(self.options.state_root):
            return None
        state = RunStore(self.options.state_root, run_id).read_state()
        return state or None

    # -- 回應 ----------------------------------------------------------

    def _html(self, body: str, code: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: Any, code: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2,
                             default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, code: int, message: str) -> None:
        body = pages.page(
            "%d" % code,
            "<h2>%d</h2><div class='empty'>%s</div>"
            "<p><a href='/'>回到首頁</a></p>" % (code, pages.esc(message)),
        )
        self._html(body, code=code)

    def log_message(self, fmt: str, *args: Any) -> None:
        """預設會把每個請求印到 stderr。自動刷新每 30 秒一次, 那會變成噪音。"""
        return


def _find(items: List[Dict[str, Any]], key: str,
          value: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if item.get(key) == value:
            return item
    return None


def serve(options: WebOptions, ready: Optional[Any] = None) -> None:
    """啟動 server (阻塞)。``ready`` 是給測試用的 threading.Event。"""
    _Handler.options = options
    httpd = ThreadingHTTPServer((options.host, options.port), _Handler)
    httpd.daemon_threads = True
    if ready is not None:
        ready.port = httpd.server_address[1]
        ready.httpd = httpd
        ready.set()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
