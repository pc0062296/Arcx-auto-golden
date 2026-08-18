# Arcx Auto Golden — 系統架構

> RC extraction 自動化提交、監控、判定與重跑系統
> 目標：把「人工盯梢 + 事後撈問題」變成「系統盯梢 + 人只做決策」，縮短 TAT。

---

## 0. 一頁摘要

| 面向 | 決定 |
|---|---|
| 部署形態 | **純 local 單人工具**，無服務端、無多人、無認證 |
| 介面 | 綁 `127.0.0.1` 的本機 web app，用 Chrome 開 |
| Arcx 啟動 | **`bsub` 出去**，job id 寫進 wave 目錄的 `launch.json` |
| 分批 | **Wave（分波）**：依 `special.cfg` 的 CPU 需求切波，逐波提交 |
| 重跑粒度 | **wave 級**（drain → 清理未完成 case run dir → `-keep_dir --run`） |
| 成功判定 | **產出物存在性為主**，log 只看「多久沒更新」，不做 error regex |
| 失敗分類 | QA function → issue ID → YAML policy → action |
| 儲存 | JSON 快照 + JSONL append-only（不用 SQLite） |
| 執行環境 | Python 3.9.10、內網、無外部網路 |

---

## 1. 分層架構

```
╔══════════════════════════════════════════════════════════════════════╗
║  L4  Interface Layer          （薄層，可替換，不含任何業務邏輯）        ║
║  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────────────┐  ║
║  │  Local Web   │ │     CLI      │ │  Status Exporter             │  ║
║  │  (127.0.0.1) │ │  arcx-auto   │ │  → 公用碟 status.json/html   │  ║
║  └──────┬───────┘ └──────┬───────┘ └──────────────▲───────────────┘  ║
╚═════════│════════════════│═══════════════════════ │══════════════════╝
     讀 state.json     讀 state.json                │
     寫 commands/*.json 寫 commands/*.json          │
          └────────────────┴───────────┐            │
╔═════════════════════════════════════ │ ══════════ │ ══════════════════╗
║  L3  Orchestration                   ▼            │                   ║
║  ┌────────────────────────────────────────────────┴────────────────┐  ║
║  │  Daemon  (唯一的寫入者 / single writer)                          │  ║
║  │  tick: collect → transition → qa → policy → act → persist       │  ║
║  └───┬──────────┬──────────┬──────────┬──────────┬────────────┬────┘  ║
╚══════│══════════│══════════│══════════│══════════│════════════│═══════╝
       ▼          ▼          ▼          ▼          ▼            ▼
╔══════════════════════════════════════════════════════════════════════╗
║  L2  Service Layer         （業務邏輯，可單元測試）                    ║
║  ┌───────────┐┌──────────┐┌─────────┐┌─────────┐┌────────┐┌────────┐ ║
║  │WavePlanner││Submission││Preflight││Collector││ State  ││   QA   │ ║
║  │ (純函數)  ││Controller││ (檢查)  ││ (觀測)  ││ Engine ││Registry│ ║
║  └─────┬─────┘└────┬─────┘└────┬────┘└────┬────┘└───┬────┘└───┬────┘ ║
║        │  ┌────────▼────────┐  │          │         │         │      ║
║        └─►│ WorkspaceBuilder│◄─┘          │    ┌────▼─────────▼────┐ ║
║           │   + Launcher    │             │    │ Policy + Remedy   │ ║
║           └────────┬────────┘             │    └─────────┬─────────┘ ║
╚════════════════════│══════════════════════│══════════════│══════════╝
                     ▼                      ▼              ▼
╔══════════════════════════════════════════════════════════════════════╗
║  L1  Adapter Layer         （唯一有 side effect 的地方）               ║
║  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  ║
║  │ FsAdapter│ │LsfAdapter│ │ArcxAdapt.│ │  Store   │ │ LockManager│  ║
║  │scandir/  │ │bsub bjobs│ │dir_map   │ │json/jsonl│ │  flock     │  ║
║  │stat/tail │ │busers    │ │special   │ │ atomic   │ │            │  ║
║  │          │ │bjobs_mgr │ │cfg/cmd   │ │          │ │            │  ║
║  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └────────────┘  ║
╚══════════════════════════════════════════════════════════════════════╝
                                   ▼
╔══════════════════════════════════════════════════════════════════════╗
║  L0  Domain Model          （純資料 + 純函數，零 I/O、零依賴）          ║
║  Run / Wave / IndexRun / Case / Observation / Issue / Action / Event  ║
╚══════════════════════════════════════════════════════════════════════╝

依賴方向：L4 → L3 → L2 → L1 → L0   （單向，絕不反向）
```

**唯一的架構鐵律：依賴只能往下。** L0 不 import 任何東西，L2 只透過 L1 的介面碰外界。
具體好處：可以在沒有 LSF、沒有 NFS、沒有 Arcx 的機器上，用假的 run folder 跑完整狀態機與 QA 邏輯測試。

---

## 2. 五個關鍵設計決策

### 決策 1：Daemon 是唯一寫入者（Single Writer）

UI / CLI 全部唯讀，動作透過 `commands/*.json` 投遞，daemon 消化後執行並刪除。

**為什麼**：不可妥協原則要求「絕不對同一 run folder 同時動兩個手」與「所有寫入型動作都有 audit log」。
與其在多個進入點各自加鎖、各自記 audit（總有一天漏掉一個），不如在架構上讓寫入只有一條路徑。
副作用皆為正面：UI 可隨時關掉、崩潰、替換，daemon 照跑；audit 天然完整，因為沒有旁路。

### 決策 2：檔案系統是唯一真相，`state.json` 只是快取

daemon 被 kill、機器重開、`state.json` 損毀 —— 重新掃一次 run folder 就能重建全部狀態。

**為什麼**：job 要跑好幾天。任何「狀態只活在記憶體或 DB」的設計，第一次意外重啟就會產生
「系統以為在跑、實際早就死了」的鬼故事，而這比原本的問題更難查。

要求：`state.json` 每個欄位都必須能從 run folder + LSF 重新推導。
唯一例外是歷史性資訊（retry 次數、audit、誰按了什麼），放在 append-only 的 JSONL，永不改寫。

### 決策 3：Adapter 層隔離所有外部世界

`FsAdapter` / `LsfAdapter` / `ArcxAdapter` 是唯三會碰到 NFS、LSF、Arcx 的模組。

**為什麼**：這是唯一能離線開發、離線測試的方法。搭配 `FakeRunFolder` 產生器
（能生出 queued / running / stalled / partial / 產出物截斷等情境），
可以在幾秒內驗證邏輯，而不是等三天 job 跑完才發現判斷寫錯。

### 決策 4：觀測 → 狀態轉移 → QA → 決策，四段嚴格分離

```
Observation (事實)  →  State (解釋)  →  Issue (判斷)  →  Action (決策)
   純 I/O              純函數           可插拔函數        YAML 規則
```

**為什麼**：這四件事的變更頻率完全不同。觀測方式幾乎不變；狀態機偶爾調；
QA function 會一直加；policy 會天天調。混在一起的話，改一條 policy 要動 I/O 程式碼，改久了沒人敢動。

### 決策 5：WavePlanner 產出的是「純資料的計畫」，不是直接動作

`WavePlan` 可序列化、可在 UI 檢視、可手動編輯、可存檔重放。算完不會立刻建目錄。

**為什麼**：分波決策牽涉 `special.cfg` 解析與 path 關鍵字推斷，本質上是推估。
做成可視、可改、可存的資料，工程師才能推翻它，也才能事後比對「估算 vs 實際」。

---

## 3. 資料流向

### 3.1 主流程：從選 index 到 job 完成

```
┌─ 使用者輸入 ────────────────────────────────────────────┐
│  dir_map 路徑 │ arcx.cfg │ 勾選的 index 清單 │ 分波參數  │
└──────────────────────┬─────────────────────────────────┘
                       ▼
        ┌──────────────────────────────┐
   ①    │  WavePlanner  (純函數)        │◄──── <index>/special.cfg (O_QCAP_LSF_NUM)
        │  算 slot → 排序 → 切波        │◄──── config: max_slots_per_wave / 關鍵字
        └──────────────┬───────────────┘
                       ▼
              ┌────────────────┐
              │   WavePlan     │  ← 純資料，UI 可預覽/拖曳編輯
              │ wave1: idxA,B  │
              │ wave2: idxC ⚠  │
              └────────┬───────┘
                       ▼
        ┌──────────────────────────────┐
   ②    │  Preflight  (提交前檢查)      │  任一 BLOCKER → 停，不建立任何東西
        └──────────────┬───────────────┘
                       ▼ (全綠)
        ┌──────────────────────────────┐      ┌──────────────────────────┐
   ③    │  SubmissionController         │─────►│ <run_root>/<run_id>/     │
        │  每 tick 檢查閘門，逐波放行    │      │   wave_001/  ← 隔離目錄   │
        └──────────────┬───────────────┘      │   wave_002/              │
                       ▼                      └──────────────────────────┘
        ┌──────────────────────────────┐
   ④    │  WorkspaceBuilder + Launcher │
        │  建目錄/快照 cfg/bsub Arcx    │──► arcx_job_id → wave/.arcx_auto/launch.json
        └──────────────────────────────┘
```

### 3.2 監控迴圈：Daemon 每個 tick

```
┌──────────────────────── TICK (每 15~60s，分層頻率) ────────────────────────┐
│                                                                            │
│   ┌─────────────┐   ┌─────────────┐   ┌──────────────────┐                │
│   │  FsProbe    │   │  LsfProbe   │   │  LogHeadParser   │                │
│   │ scandir 取  │   │ bjobs 批次  │   │ 讀 log 前 N 行   │                │
│   │ .queue/.run/│   │ busers      │   │ 抓執行路徑       │                │
│   │ .complete   │   │ (一次拿全部)│   │ → 對映到 case    │                │
│   │ + log 大小  │   │             │   │ (快取，不重讀)   │                │
│   └──────┬──────┘   └──────┬──────┘   └────────┬─────────┘                │
│          └─────────────────┼───────────────────┘                          │
│                            ▼                                              │
│                   ┌─────────────────┐                                     │
│                   │  Observation    │  不可變快照（純資料）                 │
│                   └────────┬────────┘                                     │
│                            ▼                                              │
│        prev_state ──► ┌─────────────┐ ──► new_state + Event[]              │
│                       │ StateEngine │     (純函數)                         │
│                       └──────┬──────┘                                      │
│              ┌───────────────┴───────────────┐                             │
│              ▼ (進入終態的 case)              ▼                             │
│      ┌───────────────┐                  ┌──────────┐                       │
│      │  QA Registry  │                  │  Store   │                       │
│      └───────┬───────┘                  └──────────┘                       │
│              ▼  Issue[] (id, severity, evidence)                           │
│      ┌───────────────┐◄──── config/policy.yaml  (issue_id → action)        │
│      │  PolicyEngine │◄──── budgets / cooldown / kill-switch               │
│      └───┬───────┬───┘                                                     │
│   auto ▼         ▼ escalate                                                │
│   ┌──────────┐  ┌──────────────┐                                           │
│   │Remediator│  │ TriageQueue  │──► UI「需要你決定：N 件」                  │
│   └────┬─────┘  └──────────────┘                                           │
│        ▼  Rerun 狀態機（見 §6）                                             │
│                                                                            │
│   ─────► 統一寫入: state.json(atomic) / events.jsonl / audit.jsonl          │
└────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼ (每 60s)
                    Status Exporter → 公用碟 status.json + status.html
```

### 3.3 使用者動作的流向

```
  UI 按下「重跑 wave_002」
        │
        ▼  寫入 commands/<uuid>.json  {type: rerun, target: wave_002, by, ts}
        ▼
  Daemon 下個 tick 讀取 → 驗證 → 記 audit → 執行 → 刪除 command file
        │
        ▼  結果回寫 state.json → UI 下次刷新看到
```

UI 永遠不直接動 run folder（決策 1）。

---

## 4. 模組依賴關係

| 模組 | 層 | 依賴 | 可否純函數測試 |
|---|---|---|---|
| `domain/` | L0 | 無 | — |
| `FsAdapter` | L1 | domain | 需 tmpdir |
| `LsfAdapter` | L1 | domain | 需 mock |
| `ArcxAdapter` | L1 | domain | 需 tmpdir |
| `Store` | L1 | domain | 需 tmpdir |
| `LockManager` | L1 | — | 需 tmpdir |
| `WavePlanner` | L2 | domain, ArcxAdapter, FsAdapter | ✅ **完全純**（給定 IndexSpec） |
| `SubmissionController` | L2 | domain, LsfAdapter, WorkspaceBuilder, Launcher | ✅ 閘門判定為純函數 |
| `Preflight` | L2 | 全部 L1 | 需 mock |
| `WorkspaceBuilder` | L2 | FsAdapter, ArcxAdapter | 需 tmpdir |
| `Launcher` | L2 | LsfAdapter, ArcxAdapter | 需 mock |
| `Collector` | L2 | FsAdapter, LsfAdapter | 需 mock |
| `StateEngine` | L2 | domain | ✅ **完全純** |
| `QaRegistry` | L2 | domain, FsAdapter | ✅ 幾乎純 |
| `PolicyEngine` | L2 | domain, Store | ✅ **完全純** |
| `Remediator` | L2 | LsfAdapter, FsAdapter, Launcher | 需 mock |
| `Daemon` | L3 | 全部 L2 | 整合測試 |
| `WebUI / CLI` | L4 | Store(讀), domain | — |

打 ✅ 的模組是系統的大腦，也最容易出錯 —— 設計成無 I/O 的純函數是這個架構最重要的一筆投資。

**無循環依賴**：WavePlanner 不知道 Launcher 存在；QA 不知道 Policy 存在；Policy 不知道 Remediator 存在。
串接全部由 Daemon 在 L3 完成。要換掉任何模組，只需改 Daemon 的接線。

---

## 5. 分波（Wave Scheduling）

### 5.1 為什麼分波

一次撒出所有 index 會塞爆 LSF queue。分波把提交攤平在時間軸上，並且
**每個 wave 必須有自己的隔離目錄**，否則 Arcx 會出錯（多個 Arcx 實例共用同一 cwd 會互相干擾）。

### 5.2 計算流程

```
Step 1  對每個 index:
          讀 <index_path>/special.cfg      → O_QCAP_LSF_NUM = cpu_per_case
          數 <index_path>/*.gds*            → gds_count
          index_slots = cpu_per_case × gds_count
          比對 path 關鍵字 (sram / ro / ...) → priority

Step 2  排序：priority 高的先（stable sort，同 priority 保持使用者選取順序）

Step 3  依 max_slots_per_wave 依序切波
          for idx in ordered:
              if wave.slots + idx.slots > max_slots_per_wave and wave 非空:
                  開新 wave
              wave.add(idx)
          # 單一 index 就超過上限 → 自己一波，標記 OVERSIZED 警告

Step 4  產出 WavePlan（純資料，可預覽 / 可編輯 / 可存檔）
```

Step 3 刻意不做 bin-packing 最佳化：使用者的選取順序與關鍵字優先權是明確意圖，重排會讓結果不可預期。

### 5.3 三種模式

| 模式 | 行為 | 使用時機 |
|---|---|---|
| `AUTO` | 依 slot 上限自動切波，依閘門逐波提交 | 大量 index 的日常情境 |
| `MANUAL` | UI 上自己把 index 拖進不同 wave | 想控制順序 / 優先權 |
| `OFF` | 全部 index 一條 Arcx 指令、一個目錄，一次送出 | 少量 index，或還原現行行為 |

三者共用同一個 `WavePlan` 結構 —— `OFF` 只是「只有一個 wave」的特例，
`MANUAL` 只是「分組由人指定」。下游（Submission / Workspace / Launcher / 監控）完全不需要分支處理。

### 5.4 Wave 狀態機與提交閘門

```
PLANNED ──► WAITING_GATE ──► SUBMITTING ──► SUBMITTED ──► MONITORING ──► DONE
                  ▲                                            │
                  └────────────────────────────────────────────┘
```

```yaml
gate:
  min_interval_sec: 600      # 硬性最小間隔（防抖）
  quota_threshold: 100       # busers 的 NJOBS 低於此值才放行
  max_wait_sec: 7200         # 逾時強制放行（防止 quota 永不下降而卡死）

# 放行 =  已過 min_interval  AND  ( NJOBS < quota_threshold  OR  已過 max_wait )
```

純 OR 有漏洞：時間到了但 quota 仍滿，照送會塞爆。上述組合同時涵蓋
「不會太密集」「不會塞爆」「不會無限期卡住」三件事。
`max_wait` 觸發強制放行時，UI 與 audit 必須留明確記錄。

閘門狀態（`gate_entered_at` / `last_quota_sample` / `next_check_at`）存進 `state.json`，
daemon 重啟後接續，不重新計時。

### 5.5 Wave 失敗不阻塞後續 wave

wave_001 有 case 失敗，wave_002 照常提交。只有觸發 `same_issue_burst_limit`
（代表系統性問題，例如 cfg 寫錯）時才暫停所有後續提交並升級。
否則單一小失敗會擋住整批，違背縮短 TAT 的初衷。

---

## 6. Rerun / Drain 狀態機

Arcx 的完成判定邏輯複雜，但有一個確定的契約：
**刪掉 case 的 run dir → `-keep_dir --run` 必定重跑它。**
因此我們不需要理解 Arcx 內部邏輯，只需要控制「刪哪些目錄」。

```
RERUN_REQUESTED
    │
    ▼
STOPPING_PARENT       bkill <arcx_job_id>          ← 先殺 parent，避免它補送新 job
    │
    ▼
DRAINING_CHILDREN     bjobs_manage.py -djp <wave_dir>/
    │
    ▼
VERIFY_QUIESCENT ◄──【安全門】連續 K 次（預設 3 × 30s）確認：
    │                 ① bjobs_manage.py -jp <wave_dir>/ 回報 0 個 job
    │                 ② marker 檔案集合在這段期間無變動
    │  逾時 (預設 15min) 或任一次不通過
    │       └────────► ABORT + escalate（絕不硬闖）
    ▼
DECIDE_CLEAN_SET      QA Registry 判定哪些 case 未完成 → 刪除清單寫入 audit
    │                 人工模式下在 UI 顯示清單供勾選確認
    ▼
BACKUP                未完成 case run dir → <wave>/.arcx_auto/attempts/N/   （強制）
    │
    ▼
CLEAN                 刪除這些 case run dir
    │                 中間檔 / database / QC_* 不動 —— `-keep_dir --run` 會重建
    ▼
RESUBMIT              bsub "Arcx -p cfg -d <同一批 index> -keep_dir --run"
    │                 cwd 仍是同一個 <wave_dir>，attempt +1
    ▼
MONITORING
```

### 6.1 一個刻意的例外：不確定時傾向刪除

`DECIDE_CLEAN_SET` 判錯的兩個方向後果**不對稱**：

| 判錯方向 | 後果 | 嚴重度 |
|---|---|---|
| 已完成 → 誤判未完成 → 刪掉重跑 | 浪費一次運算，**結果仍正確** | 低 |
| 未完成 → 誤判完成 → 沒刪 | Arcx 跳過它，**殘缺結果被當成功交付** | **高** |

因此在這個特定決策上，預設是「**不確定 → 傾向刪掉重跑**」，
與系統其他地方的「不確定就停手」相反。這是刻意的例外，理由是誤刪的代價可回收、漏刪的不可回收。

實作：QA 判定分 `COMPLETE` / `INCOMPLETE` / `UNKNOWN` 三態。
`UNKNOWN` 預設進刪除清單，但在 UI 以不同顏色標示、可取消勾選。BACKUP 兜底，刪錯也留得住現場。

### 6.2 每一步都可重入

daemon 在任何一步被 kill，重啟後從 `state.json` 的 `rerun_phase` 接續。
`VERIFY_QUIESCENT` 只能通過不能跳過；UI 不提供 force，真要 force 走 CLI
`--i-know-what-i-am-doing` 並在 audit 留大字記錄。

---

## 7. QA Registry 與 Policy

EDA tool 的 error/warning 訊息又雜又常變，**用產出物存在性判斷比 parse log 可靠一個數量級**。
因此 QA 以產出物與 marker 為主，log 只用來判斷「多久沒更新」。

```python
@qa_check(id="ARTIFACT_MISSING",    severity=FATAL, scope=CASE,  stage=POST)
@qa_check(id="ARTIFACT_EMPTY",      severity=FATAL, scope=CASE,  stage=POST)
@qa_check(id="ARTIFACT_TRUNCATED",  severity=FATAL, scope=CASE,  stage=POST)
@qa_check(id="MARKER_INCONSISTENT", severity=FATAL, scope=CASE,  stage=POST)
@qa_check(id="CASE_NEVER_STARTED",  severity=FATAL, scope=INDEX, stage=POST)
@qa_check(id="CASE_STALLED",        severity=WARN,  scope=CASE,  stage=LIVE)
@qa_check(id="LSF_MEMLIMIT",        severity=FATAL, scope=CASE,  stage=POST)
@qa_check(id="DISK_LOW",            severity=WARN,  scope=GLOBAL,stage=LIVE)
```

Registry 的三個性質：
- **ID 是介面**。YAML 只認 ID 不認實作，改 QA 實作不需動 policy。
- **scope × stage 決定何時被呼叫**（case/index/global × pre/live/post），Daemon 不需知道有哪些檢查。
- **每個 Issue 帶 evidence**（檔案路徑、實際值 vs 期望值），直接餵給 UI。

```yaml
policies:
  ARTIFACT_MISSING:    {action: rerun_wave, max_auto: 1}
  ARTIFACT_TRUNCATED:  {action: rerun_wave, max_auto: 1}
  LSF_MEMLIMIT:        {action: rerun_wave, mem_multiplier: 2.0, max_auto: 2}
  CASE_NEVER_STARTED:  {action: escalate}
  CASE_STALLED:        {action: escalate}
  MARKER_INCONSISTENT: {action: escalate}
  DISK_LOW:            {action: pause_submission}

default: {action: escalate}          # 原則：不確定就停手

budgets:
  max_auto_actions_per_run: 20
  cooldown_sec: 900
  same_issue_burst_limit: 5          # 同一 ID 短時間爆量 → 停自動、全面升級
  global_kill_switch: false
```

---

## 8. 儲存與行程模型

```
~/.arcx-auto/                        ← 個人 local
  daemon.lock                        ← flock，保證單一 daemon
  config/
    default.yaml  policy.yaml
  runs/<run_id>/
    manifest.json    ← 不可變：時間、cfg hash、WavePlan、原始選擇
    state.json       ← 可變快照，atomic write (tmp → fsync → os.replace)
    events.jsonl     ← append-only 狀態轉移
    audit.jsonl      ← append-only 所有寫入型動作（誰/何時/對什麼/為什麼）
    qa/<case>.json   ← QA 結果與證據
  commands/          ← UI/CLI 投遞的動作意圖，daemon 消化後刪除
```

```
<run_root>/<run_id>/                 ← run_id = <timestamp>_<label>
  wave_001/                          ← 每個 wave 一個隔離目錄（Arcx 的 cwd）
    arcx.cfg                         cfg 快照（含 hash）
    dir_map                          dir_map 快照
    .arcx_auto/
      launch.json                    bsub job id、完整指令、cwd、時間
      special_cfg/<index>.cfg        special.cfg 快照
      lock                           防止同一 wave 被跑兩次
      attempts/1/                    rerun 前備份的失敗現場
    <index_run_folder>/              ← Arcx 自己建立
      .complete.case1
      .queue.case2
      case1/  case2/  case3/         ← 每個 case 的 run dir
      QC_Cc/  QC_Ct/  QC_Spice/      ← Arcx 整理的 report
      submit_bjob_cmd_file_1.log     ← 每個 case 的 log
  wave_002/
```

**wave 目錄同時是三個邊界**：Arcx 的隔離邊界、`bjobs_manage.py -jp/-djp` 的操作邊界、rerun 的作用域。
三者用同一個路徑前綴定義，不需要額外對映表。

**為什麼 JSON + JSONL 夠用**：單人、單 daemon、單寫入者，量級數千筆。
JSONL 的 append-only 天然抗損毀（斷電最多壞最後一行），JSON 快照用 `os.replace` 保證原子性。
> 觸發改用 SQLite 的門檻：`state.json` > ~10MB，或單次 tick 序列化 > 1 秒。
> Store 是 L1 adapter，換掉不影響上層。

**行程模型**：兩個獨立 process。
```
process A: arcx-auto daemon    ← setsid/nohup 常駐，唯一寫入者，threading + sleep loop
process B: arcx-auto ui        ← 隨開隨關，唯讀 state.json，動作寫 commands/
```
不用 asyncio：`scandir` / `bjobs` / `stat` 全是 blocking I/O，同步程式碼 + thread pool 更好懂也更好除錯。

---

## 9. 外部介面契約

系統對外部世界的所有假設集中在這裡。Arcx 或環境有變動時，只需要改這一節對應的實作。

### 9.1 dir_map（Perl hash）

```perl
%dir_map =(
"1000" => "/path/to/index1000/"  ,
"1001" => "/path/to/index1001/" ,
"min" => "1000"
"max" => "1014"
);
return 1 ;
```
- 用寬鬆的 regex 抽取所有 `"KEY" => "VALUE"`（容忍缺漏逗號、單雙引號、行內註解）
- `min` / `max` 是保留 meta key，不是真實 index

### 9.2 `special.cfg`（位於每個 index path 內）

```
O_QCAP_LSF_NUM = 4
```
- `cpu_per_case = O_QCAP_LSF_NUM`
- `index_slots = cpu_per_case × gds_count`

### 9.3 Index run folder 內的檔案慣例

| 型態 | 樣式 | 意義 |
|---|---|---|
| marker | `.queue.<case>` / `.run.<case>` / `.complete.<case>` | case 狀態 |
| case run dir | `case<N>/` | 單一 case 的工作目錄（rerun 時刪這個） |
| log | `submit_bjob_cmd_file_<N>.log` | 對應 `case<N>` 的 Arcx + EDA tool 輸出 |
| report | `QC_Cc/` `QC_Ct/` `QC_Spice/` | Arcx 整理的 report，**不是 case dir** |

全部樣式都可在 config 覆寫，不寫死在程式邏輯裡。

### 9.4 LSF 指令

| 用途 | 指令 |
|---|---|
| 提交 Arcx | `bsub ... Arcx -p <cfg> -d <idx...> -lsf0 -nt 50 --run` |
| 帳號 quota | `busers` 的 `NJOBS` 欄位 |
| 列出路徑下的 job | `bjobs_manage.py -jp /abs/path/` |
| 刪除路徑下的 job | `bjobs_manage.py -djp /abs/path/` |
| 殺 parent | `bkill <arcx_job_id>` |

Drain 順序固定為：**先 `bkill` parent，再 `-djp` 子 job**（反過來的話 parent 會補送新 job）。

### 9.5 Status Exporter（公用碟）

```
<shared_root>/                  預設 /tmp1/.auto_golden （路徑可設定，之後會換碟）
  index.html                    所有 user 的總覽頁
  <user>/
    status.json                 完整欄位
    status.html                 自包含單檔，Chrome 直接開
    updated_at                  純文字時間戳
```
- 寫入一律 `tmp → os.replace`（別人可能正在讀）
- 權限：目錄 `0755`、檔案 `0644`
- 更新頻率獨立於 daemon tick（預設 60s）
- **公用碟只放衍生資料**。真相永遠在 `~/.arcx-auto/` 與 run folder；公用碟隨時可以整個刪掉重建。

---

## 10. 落地順序

| Phase | 內容 | 狀態 |
|---|---|---|
| **0** | Domain + FsAdapter + Collector + StateEngine + `status` CLI | ✅ 本次實作 |
| **2a** | ArcxAdapter(dir_map/special.cfg) + WavePlanner + `plan` CLI | ✅ 本次實作 |
| 1 | Store + Daemon 骨架 + QA Registry + 唯讀 Web UI | 待做 |
| 2b | Preflight + WorkspaceBuilder + Launcher + SubmissionController + Launch Wizard | 待做 |
| 3 | Rerun Drain 狀態機 + 人工觸發一鍵重跑 + Triage Queue | 待做 |
| 4 | PolicyEngine 自動 remediation（先 shadow mode 兩週） | 待做 |
| 5 | Status Exporter + 公用碟總覽頁 + 歷史趨勢 | 待做 |

**Phase 0 與 2a 刻意先做且純唯讀**：兩者都不寫任何東西到 run folder，
用來驗證「系統對 marker / log / dir_map / special.cfg 的理解是否正確」。
理解錯了，現在改最便宜。
