"""Arcx 相關檔案格式與指令組裝。

涵蓋三件事:
  1. dir_map  (Perl hash)          -> DirMap
  2. special.cfg (key = value)     -> cpu_per_case
  3. Arcx 指令組裝                  -> List[str]

解析一律採「寬鬆比對 + 明確警告」: 格式稍有出入時盡量讀出能讀的部分,
把疑點放進 warnings 讓人看到, 而不是丟例外讓整個流程停擺。
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.config.settings import LayoutSettings, PlanSettings
from arcx_auto.domain.models import DirMap, IndexSpec

# "1000" => "/path/to/index1000/"   支援單/雙引號, 容忍空白與缺漏逗號
_DIR_MAP_ENTRY_RE = re.compile(
    r"""["'](?P<key>[^"']+)["']\s*=>\s*["'](?P<value>[^"']*)["']"""
)
# O_QCAP_LSF_NUM = 4    支援 = 或 :, 可含引號, # 之後為註解
_KV_RE = re.compile(
    r"""^\s*(?P<key>[A-Za-z_][A-Za-z0-9_.]*)\s*[=:]\s*(?P<value>.*?)\s*$"""
)


class ArcxAdapter:
    """Arcx 的檔案格式與指令介面。"""

    def __init__(
        self,
        layout: Optional[LayoutSettings] = None,
        plan: Optional[PlanSettings] = None,
        fs: Optional[FsAdapter] = None,
    ) -> None:
        self.layout = layout or LayoutSettings()
        self.plan = plan or PlanSettings()
        self.fs = fs or FsAdapter(self.layout)

    # ------------------------------------------------------------------
    # dir_map
    # ------------------------------------------------------------------

    def parse_dir_map(self, path: str) -> DirMap:
        """解析 Perl 格式的 dir_map。

            %dir_map =(
            "1000" => "/path/to/index1000/"  ,
            "1001" => "/path/to/index1001/" ,
            "min" => "1000"
            "max" => "1014"
            );
            return 1 ;

        注意 min/max 是保留 meta key, **不是真實 index**, 不可拿去跑。
        原始檔案中 "min" 那行甚至沒有結尾逗號 —— 這正是要用 regex 逐項抽取
        而不是嚴格解析語法的原因。
        """
        resolved = os.path.abspath(os.path.expanduser(path))
        warnings: List[str] = []

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            return DirMap(
                source_path=resolved,
                warnings=("無法讀取 dir_map: %s" % exc,),
            )

        # 去掉整行註解, 避免被註解掉的條目被誤抓
        lines = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            lines.append(line.split("#", 1)[0] if "#" in line else line)
        cleaned = "\n".join(lines)

        reserved = {k.lower() for k in self.layout.dir_map_reserved_keys}
        entries: Dict[str, str] = {}
        meta: Dict[str, str] = {}

        for match in _DIR_MAP_ENTRY_RE.finditer(cleaned):
            key = match.group("key").strip()
            value = match.group("value").strip()
            if key.lower() in reserved:
                meta[key] = value
                continue
            if key in entries and entries[key] != value:
                warnings.append(
                    "index %s 重複定義且值不同, 採用最後一筆" % key
                )
            entries[key] = value

        if not entries:
            warnings.append("dir_map 內沒有解析到任何 index 條目")

        warnings.extend(self._check_dir_map_range(entries, meta))

        return DirMap(
            source_path=resolved,
            entries=entries,
            meta=meta,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _check_dir_map_range(
        entries: Dict[str, str], meta: Dict[str, str]
    ) -> List[str]:
        """用 min/max 檢查 index 是否有缺漏。

        min/max 宣告了預期的範圍, 實際條目少於範圍代表 dir_map 可能被改壞 ——
        這種問題人工看不出來, 但會導致某些 index 靜默地跑不到。
        """
        warnings: List[str] = []
        low_raw = meta.get("min")
        high_raw = meta.get("max")
        if low_raw is None or high_raw is None:
            return warnings
        try:
            low, high = int(low_raw), int(high_raw)
        except ValueError:
            warnings.append("dir_map 的 min/max 不是整數: %r / %r" % (low_raw, high_raw))
            return warnings
        if low > high:
            warnings.append("dir_map 的 min(%d) 大於 max(%d)" % (low, high))
            return warnings

        numeric = set()
        for key in entries:
            try:
                numeric.add(int(key))
            except ValueError:
                continue
        missing = [n for n in range(low, high + 1) if n not in numeric]
        if missing:
            preview = ", ".join(str(n) for n in missing[:10])
            suffix = " ..." if len(missing) > 10 else ""
            warnings.append(
                "min/max 宣告 %d-%d, 但缺少 %d 個 index: %s%s"
                % (low, high, len(missing), preview, suffix)
            )
        return warnings

    # ------------------------------------------------------------------
    # special.cfg
    # ------------------------------------------------------------------

    def parse_key_values(self, path: str) -> Tuple[Dict[str, str], List[str]]:
        """解析 key = value 形式的設定檔。回傳 (values, warnings)。"""
        warnings: List[str] = []
        values: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                raw_lines = handle.readlines()
        except OSError as exc:
            return values, ["無法讀取 %s: %s" % (path, exc)]

        for raw in raw_lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            match = _KV_RE.match(line)
            if not match:
                continue
            value = match.group("value").strip().strip(";").strip()
            value = value.strip("'\"")
            values[match.group("key")] = value
        return values, warnings

    def read_cpu_per_case(self, index_path: str) -> Tuple[int, List[str]]:
        """從 <index_path>/special.cfg 讀出每個 case 需要的 CPU 數。

        對應欄位 O_QCAP_LSF_NUM (可在 settings 覆寫)。
        """
        warnings: List[str] = []
        cfg_path = os.path.join(index_path, self.layout.special_cfg_name)
        key = self.layout.special_cfg_cpu_key

        if not os.path.isfile(cfg_path):
            warnings.append("找不到 %s" % cfg_path)
            return (self.plan.default_cpu_per_case, warnings)

        values, parse_warnings = self.parse_key_values(cfg_path)
        warnings.extend(parse_warnings)

        if key not in values:
            warnings.append("%s 內找不到 %s" % (cfg_path, key))
            return (self.plan.default_cpu_per_case, warnings)

        raw = values[key]
        try:
            cpu = int(float(raw))
        except ValueError:
            warnings.append("%s = %r 不是數字" % (key, raw))
            return (self.plan.default_cpu_per_case, warnings)

        if cpu <= 0:
            warnings.append("%s = %d 不是正數" % (key, cpu))
        return (cpu, warnings)

    # ------------------------------------------------------------------
    # IndexSpec
    # ------------------------------------------------------------------

    def build_index_spec(self, index_key: str, index_path: str) -> IndexSpec:
        """把一個 index 變成 WavePlanner 可以吃的資源描述。

            slots = O_QCAP_LSF_NUM x gds_count
        """
        resolved = os.path.abspath(os.path.expanduser(index_path))
        warnings: List[str] = []

        if not os.path.isdir(resolved):
            return IndexSpec(
                index_key=index_key,
                path=resolved,
                gds_count=0,
                cpu_per_case=0,
                error="index path 不存在或不是目錄",
            )

        gds_count, _names = self.fs.count_gds(resolved)
        if gds_count == 0:
            warnings.append("index path 內找不到任何 GDS")

        cpu, cpu_warnings = self.read_cpu_per_case(resolved)
        warnings.extend(cpu_warnings)

        keywords = self.match_keywords(resolved)

        error: Optional[str] = None
        if gds_count == 0:
            error = "沒有 GDS, 無法估算資源需求"
        elif cpu <= 0:
            error = "無法取得 %s, 無法估算資源需求" % self.layout.special_cfg_cpu_key

        return IndexSpec(
            index_key=index_key,
            path=resolved,
            gds_count=gds_count,
            cpu_per_case=cpu,
            keywords=keywords,
            priority=1 if keywords else 0,
            warnings=tuple(warnings),
            error=error,
        )

    def match_keywords(self, path: str) -> Tuple[str, ...]:
        """比對 path 中的優先關鍵字 (sram / ro / ...)。"""
        haystack = path.lower() if self.plan.keyword_ignore_case else path
        hits: List[str] = []
        for keyword in self.plan.priority_keywords:
            needle = keyword.lower() if self.plan.keyword_ignore_case else keyword
            if needle and needle in haystack:
                hits.append(keyword)
        return tuple(hits)

    # ------------------------------------------------------------------
    # 指令組裝
    # ------------------------------------------------------------------

    def build_run_command(
        self,
        cfg_path: str,
        index_keys: List[str],
        rerun: bool = False,
        lsf_settings: Optional["object"] = None,
    ) -> List[str]:
        """組出 Arcx 執行指令。

            Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 --run
            Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 -keep_dir --run   (rerun)
        """
        from arcx_auto.config.settings import LsfSettings

        lsf = lsf_settings if lsf_settings is not None else LsfSettings()
        assert isinstance(lsf, LsfSettings)

        cmd: List[str] = [lsf.arcx_cmd, "-p", cfg_path, "-d"]
        cmd.extend(index_keys)
        cmd.extend(lsf.arcx_fixed_args)
        if rerun:
            cmd.extend(lsf.arcx_rerun_args)
        cmd.append("--run")
        return cmd
