"""L4 Interface Layer —— CLI。

薄層: 只做參數解析、呼叫 service、渲染輸出。不含任何業務邏輯。
Phase 0/2a 的所有指令都是**唯讀**的, 不會寫入任何 run folder。
"""
