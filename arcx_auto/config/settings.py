"""設定模型與載入。

設計原則:
  * 預設值全部寫在 dataclass 裡, **不依賴設定檔存在**。沒有設定檔也能跑。
  * PyYAML 是選用相依。內網環境若沒有 PyYAML, 可以改用 .json 設定檔。
  * 使用者設定與預設值做 deep merge, 只覆寫有寫到的欄位。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SETTINGS_PATHS: Tuple[str, ...] = (
    "./arcx_auto.yaml",
    "~/.arcx-auto/config/default.yaml",
)


# --------------------------------------------------------------------------
# 各區段
# --------------------------------------------------------------------------

@dataclass
class LayoutSettings:
    """index run folder 內的檔案慣例 (architecture §9.3)。

    全部以 regex 描述, 因為 Arcx 版本或設定不同時, 命名可能微調。
    """

    # .queue.NDIO_1 / .run.PDIO_1 / .complete.NTN_1
    # case id 是 cell 名稱, 不是流水號, 所以這裡必須用寬鬆的 .+
    marker_regex: str = r"^\.(?P<kind>queue|run|complete)\.(?P<case>.+)$"
    # 比 marker_regex 更寬鬆: 任何 .<word>.<something> 都算「看起來像 marker」。
    # 用來抓出已知三種以外的 marker —— 那不正常, 必須明確顯示而不是當成雜訊。
    marker_any_regex: str = r"^\.(?P<kind>[A-Za-z_][A-Za-z0-9_]*)\.(?P<case>.+)$"

    # submit_bjob_cmd_file_1.log
    #   -> 依編號配對 cmd_folder/cmd_file_1
    #   -> 讀該 script 的 `cd <path>` 得知這個 log 屬於哪個 case
    # 檔名本身**不含** case 名稱, 編號與 case 之間也沒有固定關係,
    # 所以唯一可靠的對應方式就是讀 cmd_file。
    log_regex: str = r"^submit_bjob_cmd_file_(?P<num>\d+)\.log$"
    cmd_dir_name: str = "cmd_folder"
    cmd_file_template: str = "cmd_file_{num}"
    cmd_cd_regex: str = r"^\s*cd\s+[\"']?(?P<path>[^\s\"';#]+)"
    cmd_file_head_bytes: int = 16384

    # QC_Cc/ QC_Ct/ QC_Spice/  —— Arcx 整理的 report, 不是 case dir
    report_dir_regex: str = r"^QC_.+$"

    # case run dir 的名字就是 cell 名稱, 沒有共同樣式可以比對,
    # 因此改用排除法: run folder 底下不符合這些樣式的目錄就是 case run dir。
    # (rerun 時要刪的就是這些目錄)
    non_case_dir_regexes: List[str] = field(
        default_factory=lambda: [r"^QC_.+$", r"^cmd_folder$", r"^\..*$"]
    )

    # Arcx 自己在 wave 目錄下建立的 index run folder
    index_run_folder_regex: str = r"^(?P<index>[^./].*)$"

    # index path 內的 GDS 檔 (用來算 case 數)
    gds_globs: List[str] = field(
        default_factory=lambda: ["*.gds", "*.gds.gz", "*.GDS", "*.gds.bz2"]
    )
    # index path 內的 Arcx 資源設定檔
    special_cfg_name: str = "special.cfg"
    special_cfg_cpu_key: str = "O_QCAP_LSF_NUM"

    # dir_map 內不是真實 index 的保留 key
    dir_map_reserved_keys: List[str] = field(default_factory=lambda: ["min", "max"])


@dataclass
class MonitorSettings:
    """監控與狀態判定門檻。"""

    # log 連續多久沒有成長就視為 STALLED
    stall_threshold_sec: float = 3600.0
    # QUEUED 超過多久值得注意 (僅提示, 不改變狀態)
    long_pend_warn_sec: float = 7200.0
    # LSF job 消失後, 等多久才敢判定 LOST
    # (給 NFS attribute cache 與 Arcx 收尾一點緩衝)
    lost_grace_sec: float = 300.0
    # 讀 log 開頭幾個 byte 來推斷 case 歸屬
    log_head_bytes: int = 8192
    # 分層 polling 間隔
    poll_active_sec: float = 30.0
    poll_idle_sec: float = 300.0


@dataclass
class PlanSettings:
    """分波計畫參數 (architecture §5)。"""

    # 單一 wave 的 slot 上限。slots = O_QCAP_LSF_NUM x gds_count
    max_slots_per_wave: int = 200
    # path 中含有這些關鍵字的 index 優先排入前面的 wave
    priority_keywords: List[str] = field(default_factory=lambda: ["sram", "ro"])
    # 關鍵字比對是否忽略大小寫
    keyword_ignore_case: bool = True
    # 找不到 special.cfg 時的保守預設 (0 = 視為錯誤, 不猜)
    default_cpu_per_case: int = 0


@dataclass
class GateSettings:
    """提交閘門 (architecture §5.4)。

    放行 = 已過 min_interval AND ( NJOBS < quota_threshold OR 已過 max_wait )

    純 OR 有漏洞: 時間到了但 quota 仍滿, 照送會塞爆 queue。
    這個組合同時涵蓋「不會太密集」「不會塞爆」「不會無限期卡住」。
    """

    min_interval_sec: float = 600.0
    quota_threshold: int = 100
    max_wait_sec: float = 7200.0


@dataclass
class LsfSettings:
    """LSF 相關指令 (architecture §9.4)。"""

    bjobs_cmd: str = "bjobs"
    busers_cmd: str = "busers"
    bkill_cmd: str = "bkill"
    bsub_cmd: str = "bsub"
    # 列出/刪除某路徑底下所有 job 的自製工具
    bjobs_manage_cmd: str = "bjobs_manage.py"
    bjobs_manage_list_flag: str = "-jp"
    bjobs_manage_delete_flag: str = "-djp"
    # 外部指令逾時
    command_timeout_sec: float = 60.0
    # Arcx 固定參數 (使用者確認可寫死)
    arcx_cmd: str = "Arcx"
    arcx_fixed_args: List[str] = field(default_factory=lambda: ["-lsf0", "-nt", "50"])
    arcx_rerun_args: List[str] = field(default_factory=lambda: ["-keep_dir"])

    # drain 安全門: 連續幾次確認 0 job 才算靜止
    quiescent_confirm_times: int = 3
    quiescent_interval_sec: float = 30.0
    quiescent_timeout_sec: float = 900.0


@dataclass
class FlowProfile:
    """一個 EDA tool flow 在 case run dir 內留下什麼。

    路徑樣板可用的變數:
        {flow}   QC_FLOW 的值, 例如 calQCAP
        {block}  arcx.cfg 內的 block 名稱
        {case}   case id (cell 名稱), 例如 NTN_1

    新增一種 EDA tool = 在設定裡加一個 profile, 不需要改程式。
    """

    work_dir: str = "work_{flow}"
    netlists: List[str] = field(default_factory=list)
    min_bytes: int = 1


@dataclass
class ReportSettings:
    """index run folder 底下的 QC_* report 目錄。"""

    # 一定會存在的, 缺了就是 FATAL
    required_dirs: List[str] = field(default_factory=lambda: ["QC_Cc", "QC_Ct"])
    # 有的話要檢查內容, 沒有也不算錯 (未來再補其他 QC_* 的邏輯)
    optional_dirs: List[str] = field(default_factory=lambda: ["QC_Spice"])
    # QC_Cc/Report_QC_Cc
    main_file_template: str = "Report_{dir}"
    # QC_Cc/Report_QC_Cc_Summary_SCCB3  —— 後綴會變, 所以用 glob
    summary_glob_template: str = "Report_{dir}_Summary_*"


@dataclass
class QuietSettings:
    """「多久沒寫 log」的分級門檻。

    刻意做成**分級顯示**而不是二元判定: 單一 case 的 runtime 從 10 分鐘到
    3 天都有, 而且存在「不寫 log 但產出物已經齊了」的正常情況。
    系統負責把「安靜多久」講清楚並隨時間升級醒目程度, 判斷交給人。
    """

    warn_after_sec: float = 14400.0      # 4h  —— 少見, 值得看一眼
    stalled_after_sec: float = 28800.0   # 8h  —— 基本上可判定卡住
    # 產出物都齊了才安靜下來, 通常只是在收尾 -> 降一級, 避免誤報
    downgrade_when_artifacts_ready: bool = True


@dataclass
class QaSettings:
    """QA 檢查設定。"""

    flows: Dict[str, FlowProfile] = field(default_factory=lambda: {
        "calQCAP": FlowProfile(netlists=["CCI_DB.spice"]),
        "calQRCFS": FlowProfile(netlists=["{case}.spf"]),
    })
    min_netlist_bytes: int = 1
    reports: ReportSettings = field(default_factory=ReportSettings)
    quiet: QuietSettings = field(default_factory=QuietSettings)
    # arcx.cfg 中值看起來像絕對路徑的設定, 提交前要檢查檔案是否存在
    verify_cfg_paths: bool = True
    # 這些 key 即使長得像路徑也不檢查 (輸出路徑等執行後才會產生)
    cfg_path_check_skip_keys: List[str] = field(default_factory=list)
    # 要停用的檢查 id。放在設定裡, 這樣不需要刪程式就能關掉一條規則。
    disabled_checks: List[str] = field(default_factory=list)


@dataclass
class ExportSettings:
    """公用碟匯出 (architecture §9.5)。

    公用碟只放衍生資料 —— 真相永遠在 ~/.arcx-auto/ 與 run folder,
    這裡隨時可以整個刪掉重建。路徑之後會換碟, 所以必須可設定。
    """

    shared_root: str = "/tmp1/.auto_golden"
    interval_sec: float = 60.0
    dir_mode: int = 0o755
    file_mode: int = 0o644


@dataclass
class Settings:
    """根設定。"""

    state_root: str = "~/.arcx-auto"
    run_root: str = "./arcx_runs"
    layout: LayoutSettings = field(default_factory=LayoutSettings)
    monitor: MonitorSettings = field(default_factory=MonitorSettings)
    plan: PlanSettings = field(default_factory=PlanSettings)
    gate: GateSettings = field(default_factory=GateSettings)
    lsf: LsfSettings = field(default_factory=LsfSettings)
    qa: QaSettings = field(default_factory=QaSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    source_path: Optional[str] = None

    def expanded_state_root(self) -> str:
        return os.path.abspath(os.path.expanduser(self.state_root))

    def expanded_run_root(self) -> str:
        return os.path.abspath(os.path.expanduser(self.run_root))


# --------------------------------------------------------------------------
# 載入
# --------------------------------------------------------------------------

def _apply_overrides(target: Any, data: Dict[str, Any], path: str = "") -> List[str]:
    """把 dict 套用到 dataclass 實例上, 回傳未知欄位的警告清單。"""
    warnings: List[str] = []
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        where = "%s.%s" % (path, key) if path else key
        if key not in known:
            warnings.append("未知設定欄位: %s" % where)
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            warnings.extend(_apply_overrides(current, value, where))
        elif isinstance(current, dict) and isinstance(value, dict):
            # 例如 qa.flows: {calQCAP: {...}} —— 值本身是 dataclass 時,
            # 逐項合併而不是整個換掉, 這樣使用者只覆寫一個 flow 的一個欄位
            # 也不會把其他預設值弄丟。
            merged = dict(current)
            for sub_key, sub_value in value.items():
                existing = merged.get(sub_key)
                if is_dataclass(existing) and isinstance(sub_value, dict):
                    warnings.extend(_apply_overrides(
                        existing, sub_value, "%s.%s" % (where, sub_key)))
                elif isinstance(sub_value, dict) and _prototype(current) is not None:
                    prototype = _prototype(current)
                    fresh = prototype()
                    warnings.extend(_apply_overrides(
                        fresh, sub_value, "%s.%s" % (where, sub_key)))
                    merged[sub_key] = fresh
                else:
                    merged[sub_key] = sub_value
            setattr(target, key, merged)
        else:
            setattr(target, key, value)
    return warnings


def _prototype(mapping: Dict[str, Any]) -> Optional[type]:
    """從既有的值推斷這個 dict 裡裝的是哪種 dataclass。

    用於「使用者新增了一個預設值裡沒有的 flow」的情況 —— 我們需要知道
    該用哪個型別去建立它。
    """
    for value in mapping.values():
        if is_dataclass(value) and not isinstance(value, type):
            return type(value)
    return None


def _read_config_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if path.endswith(".json"):
        return json.loads(text) or {}
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取決於環境
        raise RuntimeError(
            "讀取 YAML 設定需要 PyYAML。請改用 .json 設定檔, 或安裝 PyYAML。"
        ) from exc
    return yaml.safe_load(text) or {}


def load_settings(
    path: Optional[str] = None,
    search_defaults: bool = True,
) -> Tuple[Settings, List[str]]:
    """載入設定。

    回傳 (settings, warnings)。找不到任何設定檔時回傳純預設值 ——
    這是刻意的: 沒有設定檔也要能跑。
    """
    settings = Settings()
    warnings: List[str] = []

    candidates: List[str] = []
    if path:
        candidates.append(path)
    elif search_defaults:
        candidates.extend(DEFAULT_SETTINGS_PATHS)

    for candidate in candidates:
        resolved = os.path.abspath(os.path.expanduser(candidate))
        if not os.path.isfile(resolved):
            if path:  # 使用者明確指定卻不存在 -> 應該報錯而不是靜默忽略
                raise FileNotFoundError("設定檔不存在: %s" % resolved)
            continue
        data = _read_config_file(resolved)
        warnings.extend(_apply_overrides(settings, data))
        settings.source_path = resolved
        break

    return settings, warnings
