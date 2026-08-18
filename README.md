# Arcx Auto Golden

RC extraction 自動化提交、監控、判定與重跑系統。

目標：把「人工盯梢 + 事後撈問題」變成「系統盯梢 + 人只做決策」，縮短 TAT。

完整設計請看 **[docs/architecture.md](docs/architecture.md)**。

---

## 目前進度

| Phase | 內容 | 狀態 |
|---|---|---|
| **0** | Domain + FsAdapter + Collector + StateEngine + `status` CLI | ✅ 已完成 |
| **2a** | ArcxAdapter (dir_map / special.cfg) + WavePlanner + `plan` CLI | ✅ 已完成 |
| **1a** | arcx.cfg parser + QA Registry + StateResolver + PRE 檢查 | ✅ 已完成 |
| **1b** | Store + LockManager + Daemon + 唯讀 Web UI | ✅ 已完成 |
| 2b | Preflight + WorkspaceBuilder + Launcher + SubmissionController | 待做 |
| 3 | Rerun Drain 狀態機 + Triage Queue | 待做 |
| 4 | PolicyEngine 自動 remediation | 待做 |
| 5 | Status Exporter + 公用碟總覽頁 | 待做 |

> **Phase 0 與 2a 全部是唯讀的**：不寫入任何 run folder、不提交任何 job。
> 目的是先驗證系統對 marker / log / dir_map / special.cfg 的理解是否正確 ——
> 理解錯了，現在改最便宜。測試中有明確斷言保證這一點。

---

## 需求

* Python 3.9.10（或以上）
* **零第三方相依**。PyYAML 只在讀 `.yaml` 設定檔時才需要；
  沒有 PyYAML 就改用 `.json` 設定檔，結構完全相同。

---

## 快速開始

```bash
# 1. 造一份假的 run folder（涵蓋完成/執行中/卡住/marker 不一致/孤兒目錄等情境）
python3 tests/fixtures/fake_run.py /tmp/arcx-demo

# 2. 驗證 dir_map 解析
python3 -m arcx_auto inspect dir-map /tmp/arcx-demo/dir_map --verify

# 3. 看 run folder 狀態
python3 -m arcx_auto status --wave-dir /tmp/arcx-demo/wave_001 --no-lsf --detail

# 4. 產生分波計畫（只計算，不建目錄、不提交）
python3 -m arcx_auto plan --dir-map /tmp/arcx-demo/dir_map --all \
        --max-slots 100 --show-command
```

## 對真實資料使用

```bash
# 解析真實的 dir_map，並檢查每個 index path 是否存在
python3 -m arcx_auto inspect dir-map /path/to/dir_map --verify

# 檢查某個 index 的資源需求（讀 special.cfg 的 O_QCAP_LSF_NUM + 數 GDS）
python3 -m arcx_auto inspect index /path/to/index1000/

# 監控正在跑的 index run folder（會查 bjobs）
python3 -m arcx_auto status --run-folder /path/to/run/1000 --detail

# 持續監控，並把狀態存進快取檔（stall 計時才能跨次呼叫累積）
python3 -m arcx_auto status --wave-dir /path/to/wave_001 \
        --state-file ~/.arcx-auto/scan.json --watch 30

# 分波計畫
python3 -m arcx_auto plan --dir-map /path/to/dir_map \
        --index 1000 1001 1002 --max-slots 200 --show-command
```

所有指令都支援 `--json`，方便接後續工具或存檔比對。

## 持續監控 + Web UI

```bash
# 終端機 1：daemon（唯一的寫入者）
python3 -m arcx_auto daemon --run-id nightly --wave-dir /path/to/wave_001

# 終端機 2：Web UI（唯讀，隨開隨關）
python3 -m arcx_auto web            # 然後用 Chrome 開 http://127.0.0.1:8765/
```

Web UI 只用標準庫 `http.server`，預設只綁 `127.0.0.1`、沒有任何寫入端點。
四層 drill-down：所有 run → index 列表 + issue 摘要 → case 表格 → 單一 case 的證據。

daemon 可以隨時 Ctrl-C 再重啟 —— 它會從 `state.json` 接續上次的判定，
即使那份檔案不見了也能從 run folder 重建。

## 提交前檢查 arcx.cfg

```bash
python3 -m arcx_auto check-cfg /path/to/arcx.cfg
```

檢查 block 名稱是否重複、`QC_FLOW` 是否認得、引用的檔案是否存在。
**行首旗標 `0` 或被 `#` 註解掉的行不會被檢查** —— 那些設定不生效，
驗證它們只會製造假警報。

---

## 指令總覽

| 指令 | 用途 |
|---|---|
| `status --run-folder PATH...` | 掃描指定的 index run folder |
| `status --wave-dir PATH` | 掃描整個 wave 目錄底下所有 index run folder |
| `plan --dir-map FILE --index ...` | 產生分波計畫（**不執行**） |
| `inspect dir-map FILE` | 解析 dir_map，檢查 index 是否有缺漏 |
| `inspect index PATH...` | 解析 special.cfg 與 GDS 數，算出 slot 需求 |
| `check-cfg FILE` | 提交前檢查 arcx.cfg（有 FATAL 時 exit code 1） |
| `daemon --wave-dir PATH` | 持續監控，把狀態寫進 state root |
| `web` | 啟動本機 Web UI（唯讀，只綁 127.0.0.1） |

常用選項：`--json`、`--detail`、`--watch SEC`、`--state-file`、`--no-lsf`、`-c CONFIG`。

---

## 設定

設定檔是**可選的**——所有欄位都有內建預設值。尋找順序：

1. `-c/--config` 明確指定
2. `./arcx_auto.yaml`
3. `~/.arcx-auto/config/default.yaml`

範本見 [`config/default.yaml`](config/default.yaml)。
所有「對外部世界的假設」（marker 命名、log 命名、`special.cfg` 的欄位名、
LSF 指令、公用碟路徑）都在設定裡，不寫死在程式邏輯中。

---

## 開發

```bash
python3 -m unittest discover -s tests -v     # 全部測試
python3 -m unittest tests.test_state_engine  # 單一模組
```

測試**完全離線**：不需要 LSF、不需要 NFS、不需要 Arcx。
`tests/fixtures/fake_run.py` 可以在毫秒內造出各種 run folder 情境，
包含真實 job 要跑三天才會出現的狀況（卡住、job 消失、marker 不一致）。

### run folder 的兩個關鍵慣例

```
.queue.NDIO_1  .run.PDIO_1  .complete.NTN_1     marker，case id 是 cell 名稱
NDIO_1/  PDIO_1/  NTN_1/                        case run dir（rerun 時刪這些）
QC_Cc/  QC_Ct/  QC_Spice/                       Arcx 的 report，不是 case
submit_bjob_cmd_file_1.log                      log，檔名只有流水號
cmd_folder/cmd_file_1                           script，內含 `cd <case run dir>`
```

**① case id 是 cell 名稱，沒有共同樣式** → case run dir 用排除法辨識，
排除清單在 `layout.non_case_dir_regexes`。

**② log 檔名與 case 沒有關係** → `submit_bjob_cmd_file_1.log` 依編號配對
`cmd_folder/cmd_file_1`，再從 script 的 `cd <path>` 取 basename 得到 case id。
編號順序不保證等於任何排序，測試中有專門案例擋住「用編號猜」的偷懶實作。

無法解析的 log 會進 `unresolved_logs` 並顯示在 `status` 的「掃描異常」區 ——
代表有一個 case 我們監控不到，不能靜默忽略。

### QA：期望產出物由 arcx.cfg 推導

```
arcx.cfg                                case run dir
1 BEGIN_SETTINGS: blocking_naming_qcap    NTN_1/
1   QC_FLOW = calQCAP                       blocking_naming_qcap_calQCAP/
END_SETTINGS                                  work_calQCAP/
                                                CCI_DB.spice
```

規則：`<block>_<QC_FLOW>/work_<QC_FLOW>/<該 flow 的 netlist>`。
每個 flow 產出什麼定義在 `qa.flows` —— **新增一種 EDA tool 只要改設定，不用改程式**。

要加一條檢查，在 `arcx_auto/services/qa/checks_case.py` 複製一個既有的來改：

```python
@qa_check(id="NETLIST_MISSING", title="netlist 缺失",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def netlist_missing(case: CaseContext) -> Optional[Issue]:
    """這段 docstring 會直接顯示在 UI 上。"""
    missing = [a for a in case.expected_artifacts if not case.exists(a.relpath)]
    if not missing:
        return None
    return case.fail("缺少 %d 個 netlist" % len(missing),
                     evidence={"missing": [a.relpath for a in missing]})
```

要停用一條：設定裡的 `qa.disabled_checks` 加上它的 id，不用刪程式。

QA 的三個不可妥協性質：**「不知道」絕不當成通過**（`Severity.UNKNOWN`）、
**一條壞規則不能拖垮監控**（每條檢查獨立 try）、**id 是介面**（重複註冊直接報錯）。

### 架構鐵律

依賴方向單向往下，絕不反向：

```
L4 cli/       介面（薄層，無業務邏輯）
L3 daemon/    編排（唯一寫入者）           [Phase 1]
L2 services/  業務邏輯（可單元測試）
L1 adapters/  唯一有 side effect 的地方
L0 domain/    純資料 + 純函數，零 I/O
```

`StateEngine` 與 `WavePlanner` 是**完全純函數** —— 這是本專案最重要的設計投資，
因為真實 job 要跑好幾天，靠實跑來驗證判定邏輯的迭代速度無法接受。
