"""設定層。

所有「對外部世界的假設」都集中在這裡, 不寫死在程式邏輯中 ——
Arcx 或環境的慣例有變動時, 只需要改設定, 不需要動程式碼。
"""

from arcx_auto.config.settings import (
    Settings,
    LayoutSettings,
    MonitorSettings,
    PlanSettings,
    GateSettings,
    LsfSettings,
    ExportSettings,
    load_settings,
    DEFAULT_SETTINGS_PATHS,
)

__all__ = [
    "Settings",
    "LayoutSettings",
    "MonitorSettings",
    "PlanSettings",
    "GateSettings",
    "LsfSettings",
    "ExportSettings",
    "load_settings",
    "DEFAULT_SETTINGS_PATHS",
]
