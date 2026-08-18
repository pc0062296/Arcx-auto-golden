"""arcx.cfg 解析。

格式:

    1 BEGIN_SETTINGS: blocking_naming_qcap
    1 QC_FLOW = calQCAP
    1 RCX_TECH_QTF = /path/to/file
    1 RCX_LAYER_NAME_MAP = /path/to/file
    END_SETTINGS

    1 BEGIN_SETTINGS: blocking_naming_qrcfs
    1 QC_FLOW = calQRCFS
    END_SETTINGS

一個 cfg 可以有多個 block, 每個 block 代表一組 EDA tool 設定,
由 ``QC_FLOW`` 指定用哪個 flow (目前是 calQCAP / calQRCFS)。

**這個檔案是 QA 的地圖。** case run dir 內該有哪些產出物, 完全由 cfg 的
block 決定 (見 services/qa/expectations.py):

    <block_name>_<QC_FLOW>/work_<QC_FLOW>/<該 flow 的 netlist>

因此提交時必須把 cfg 快照下來 —— 三天後回頭做 QA, 用的必須是當時那份 cfg。

行首的整數是 enable/disable 旗標: 1 = 啟用, 0 = 停用。
被停用的行 (旗標 0) 與註解行 (#) 一律不生效, 因此 PRE 檢查**不會**去驗證
它們引用的檔案是否存在 —— 那些路徑根本不會被用到, 檢查它們只會製造假警報。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 行首可選的整數旗標, 其餘為內容
_LINE_RE = re.compile(r"^\s*(?:(?P<flag>-?\d+)\s+)?(?P<body>.*?)\s*$")
# BEGIN_SETTINGS: <name>
# 正式拼法是 BEGIN_SETTINGS。仍然接受 BEGIN_SETTIMGS 這種筆誤,
# 但會發出警告 —— 拼錯的關鍵字若被 Arcx 當成無效行, 整個 block 會被忽略,
# 那是很難查的失敗。
_BEGIN_RE = re.compile(
    r"^(?P<kw>BEGIN_SETT(?:INGS|IMGS))\s*:\s*(?P<name>\S+)\s*$", re.IGNORECASE
)
_END_RE = re.compile(r"^(?P<kw>END_SETT(?:INGS|IMGS))\s*$", re.IGNORECASE)
_CANONICAL_BEGIN = "BEGIN_SETTINGS"
_CANONICAL_END = "END_SETTINGS"
_KV_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(?P<value>.*)$")


@dataclass(frozen=True)
class CfgBlock:
    """arcx.cfg 內的一個 settings block。"""

    name: str                       # blocking_naming_qcap
    enabled: bool = True
    line_no: int = 0
    settings: Dict[str, str] = field(default_factory=dict)
    # 行首旗標為 0 的設定。這些行不生效, 所以 PRE 檢查不會驗證它們的路徑。
    disabled_keys: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def flow(self) -> Optional[str]:
        """QC_FLOW —— 決定用哪個 EDA tool flow。"""
        return self.settings.get("QC_FLOW")

    @property
    def output_dir_name(self) -> Optional[str]:
        """case run dir 底下對應這個 block 的目錄名: <block>_<flow>。"""
        if not self.flow:
            return None
        return "%s_%s" % (self.name, self.flow)


@dataclass(frozen=True)
class ArcxConfig:
    """解析後的 arcx.cfg。"""

    source_path: str
    blocks: Tuple[CfgBlock, ...] = ()
    warnings: Tuple[str, ...] = ()
    error: Optional[str] = None

    @property
    def enabled_blocks(self) -> Tuple[CfgBlock, ...]:
        return tuple(b for b in self.blocks if b.enabled)

    def block(self, name: str) -> Optional[CfgBlock]:
        for candidate in self.blocks:
            if candidate.name == name:
                return candidate
        return None


def parse_arcx_cfg(path: str) -> ArcxConfig:
    """解析 arcx.cfg。

    採「寬鬆比對 + 明確警告」: 格式稍有出入時盡量讀出能讀的部分, 把疑點
    放進 warnings 讓人看到, 而不是丟例外讓整個流程停擺。
    """
    resolved = os.path.abspath(os.path.expanduser(path))
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
            raw_lines = handle.readlines()
    except OSError as exc:
        return ArcxConfig(source_path=resolved, error="無法讀取 arcx.cfg: %s" % exc)

    blocks: List[CfgBlock] = []
    warnings: List[str] = []

    current_name: Optional[str] = None
    current_line_no = 0
    current_enabled = True
    current_settings: Dict[str, str] = {}
    current_disabled: List[str] = []
    current_warnings: List[str] = []

    def close_block(end_line: int) -> None:
        if current_name is None:
            return
        blocks.append(CfgBlock(
            name=current_name,
            enabled=current_enabled,
            line_no=current_line_no,
            settings=dict(current_settings),
            disabled_keys=tuple(current_disabled),
            warnings=tuple(current_warnings),
        ))

    for index, raw in enumerate(raw_lines, start=1):
        line = raw.split("#", 1)[0].rstrip("\n")
        if not line.strip():
            continue

        match = _LINE_RE.match(line)
        body = match.group("body") if match else line.strip()
        flag_raw = match.group("flag") if match else None
        if not body:
            continue

        begin = _BEGIN_RE.match(body)
        if begin:
            if begin.group("kw").upper() != _CANONICAL_BEGIN:
                warnings.append(
                    "第 %d 行的關鍵字拼成 %s, 正式拼法是 %s"
                    % (index, begin.group("kw"), _CANONICAL_BEGIN)
                )
            if current_name is not None:
                current_warnings.append(
                    "第 %d 行出現新的 BEGIN_SETTINGS, 但前一個 block 沒有 END_SETTINGS"
                    % index
                )
                close_block(index)
            current_name = begin.group("name")
            current_line_no = index
            current_enabled = flag_raw is None or flag_raw != "0"
            current_settings = {}
            current_disabled = []
            current_warnings = []
            continue

        end_match = _END_RE.match(body)
        if end_match:
            if end_match.group("kw").upper() != _CANONICAL_END:
                warnings.append(
                    "第 %d 行的關鍵字拼成 %s, 正式拼法是 %s"
                    % (index, end_match.group("kw"), _CANONICAL_END)
                )
            if current_name is None:
                warnings.append("第 %d 行有 END_SETTINGS 但沒有對應的 BEGIN" % index)
            else:
                close_block(index)
                current_name = None
            continue

        kv = _KV_RE.match(body)
        if not kv:
            if current_name is None:
                warnings.append("第 %d 行不在任何 block 內且無法解析: %r" % (index, body))
            else:
                current_warnings.append("第 %d 行無法解析: %r" % (index, body))
            continue

        key = kv.group("key")
        value = kv.group("value").strip().strip(";").strip().strip("'\"")

        if current_name is None:
            warnings.append("第 %d 行的設定 %s 不在任何 block 內, 已忽略" % (index, key))
            continue

        if flag_raw == "0":
            current_disabled.append(key)
            continue

        if key in current_settings and current_settings[key] != value:
            current_warnings.append("%s 重複定義且值不同, 採用最後一筆" % key)
        current_settings[key] = value

    if current_name is not None:
        current_warnings.append("檔案結束時 block %s 沒有 END_SETTINGS" % current_name)
        close_block(len(raw_lines))

    seen: Dict[str, int] = {}
    for block in blocks:
        seen[block.name] = seen.get(block.name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            warnings.append(
                "block 名稱 %s 出現 %d 次 —— 產出目錄會互相覆蓋" % (name, count)
            )

    if not blocks:
        warnings.append("arcx.cfg 內沒有解析到任何 settings block")

    return ArcxConfig(
        source_path=resolved,
        blocks=tuple(blocks),
        warnings=tuple(warnings),
    )
