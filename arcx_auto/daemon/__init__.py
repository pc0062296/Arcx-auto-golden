"""L3 Orchestration —— daemon。

**系統的唯一寫入者** (architecture 決策 1)。
UI / CLI 全部唯讀, 動作透過 commands/ 目錄投遞, 由 daemon 序列化執行。

這讓「絕不對同一 run folder 同時動兩個手」與「所有寫入型動作都有 audit log」
兩條原則在架構上成立, 而不是靠每個進入點自己記得加鎖。
"""

from arcx_auto.daemon.loop import Daemon, DaemonOptions
from arcx_auto.daemon.state import build_state_payload

__all__ = ["Daemon", "DaemonOptions", "build_state_payload"]
