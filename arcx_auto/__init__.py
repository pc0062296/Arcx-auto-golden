"""Arcx Auto Golden - RC extraction 自動化提交、監控、判定與重跑系統。

分層架構 (依賴方向 L4 -> L3 -> L2 -> L1 -> L0, 單向):

    L4  cli/            介面層 (薄層, 無業務邏輯)
    L3  daemon/         編排層 (唯一寫入者)          [Phase 1]
    L2  services/       業務邏輯 (可單元測試)
    L1  adapters/       唯一有 side effect 的地方
    L0  domain/         純資料 + 純函數, 零 I/O

詳見 docs/architecture.md。
"""

__version__ = "0.1.0"
