"""L4 —— 本機 Web UI。

**只用標準庫。** 這是單人、綁 127.0.0.1、唯讀的儀表板, 不是對外服務,
所以 http.server 完全夠用, 而且在內網不需要處理任何套件安裝。

唯讀是硬性的: UI 只讀 daemon 寫出的 state.json, 不碰 run folder,
也不自己算任何東西。所有寫入型動作 (Phase 3) 會透過 commands/ 投遞給 daemon。
"""

from arcx_auto.web.server import WebOptions, serve

__all__ = ["WebOptions", "serve"]
