# Arcx Auto Golden -- Project Handover

This document is written so that somebody (or some model) with **no prior
context** can understand what this project is, why every significant decision
was made, and rebuild it from scratch.

It contains no source code on purpose. It contains the **file formats**,
because those are the part that cannot be guessed: they belong to somebody
else's tools.

Status at handover: **30 commits, ~18k lines of implementation, ~9.5k lines of
tests, 763 tests passing.** Version 0.1.0.

---

# 1. Project overview

## 1.1 Name and goal

**Arcx Auto Golden** -- automated submission, monitoring, QA and rerun for RC
extraction runs driven by a tool called **Arcx** on an **LSF** cluster.

The one-sentence goal, in the words it was given:

> Replace "watch it by hand, and dig the problems out afterwards" with "the
> system watches, and people only make decisions", and so cut turnaround time.

The problem it solves is not "Arcx fails". It is that **Arcx can succeed and
still be wrong**: it writes a `.complete` marker for a case whose netlist was
never produced, or was truncated, or whose comparison table is full of failure
sentinels. Those runs ship as successes and are discovered days later. The
system exists to catch that class of failure -- "false success" -- and to stop
a person having to read hundreds of directories by hand to find it.

Three secondary goals, in order:

1. **Batch submission without flooding the LSF queue.** A person ticking fifty
   indices has not thought about the queue; the system sizes and paces it.
2. **Make a failure one click from its evidence**, instead of a path to copy
   into a terminal.
3. **Make a rerun safe**: never overwrite evidence, never redo finished work,
   never act when the state is ambiguous.

## 1.2 Technology

| Aspect | Decision |
|---|---|
| Language | **Python 3.9.10** (the exact version available on the target machines) |
| Dependencies | **Zero third-party packages.** Standard library only |
| Character set | **Pure ASCII.** Every byte of every file in the repository |
| Storage | JSON snapshots + append-only JSONL. **Explicitly not SQLite** |
| Web | `http.server` from the standard library, bound to `127.0.0.1` |
| Config | YAML if PyYAML happens to exist, otherwise the same structure as JSON |
| Tests | `unittest`, no test framework dependency |

These are hard constraints from the deployment environment, not preferences:

- The network is **air-gapped**. Installing a package is a procurement request,
  not a command. Anything requiring `pip install` cannot be deployed.
- The terminal and editors on those machines **cannot display non-ASCII**.
  A single non-ASCII byte anywhere in the project is a defect. There is a test
  (`tests/test_ascii_only.py`) that walks every file and fails on one.
- Python 3.9 means: no `match`, no `X | Y` type unions, no
  `dict[str, int]` at runtime in annotations without `from __future__ import
  annotations` (which every module does), no `str.removeprefix` in some places.
  A compatibility test pins this.

**Conversation language note:** all discussion with the project owner happens in
Traditional Chinese; all artifacts are ASCII English. These do not conflict and
should not be confused.

## 1.3 Runtime and deployment

**A local, single-user tool.** There is no server, no multi-user story, and no
authentication:

- Every user runs their own daemon under their own account, on their own disk.
- Nobody can reach anybody else's run directory. Sharing happens one way only:
  the daemon publishes a static HTML page to a shared disk that others *read*.
- The web UI binds to `127.0.0.1`. Because a page in the same browser can still
  POST to loopback, every POST checks the `Origin` header. That is an **origin
  check, not authentication** -- it stops a stray tab, not a person.

Installation is a symlink:

```
ln -s /path/to/arcx-auto-golden/bin/arcx-auto ~/bin/arcx-auto
```

`bin/arcx-auto` is a POSIX `sh` launcher that resolves its own symlink, sets
`PYTHONPATH` to the repository, and `exec`s `python3 -m arcx_auto "$@"`. This
is what makes the tool runnable from any directory without installation.

Daily use is one command per working directory:

```
cd /proj/chipA  &&  arcx-auto start      # daemon + web UI + browser
cd /proj/chipB  &&  arcx-auto daemon     # daemon only
```

**Reruns only ever run from one fixed machine**, which is why `flock` is
sufficient for the wave lock and NFS lock reliability across hosts was never a
design problem.

---

# 2. System architecture

## 2.1 Layering

Dependencies point one way only. This is the single most important structural
rule in the project:

```
L4  cli/  web/        interfaces. Thin. No business logic.
L3  daemon/           orchestration. THE ONLY WRITER.
L2  services/         business logic. Unit testable, mostly pure.
L1  adapters/         the only place with side effects (fs, LSF, locks).
L0  domain/           pure data and pure functions. Imports nothing.
```

L0 imports nothing from the project. L1 may import L0. L2 may import L0 and
L1. And so on. There is no upward import anywhere.

The payoff is that the interesting logic -- which state a case is in, which
wave an index belongs to, whether a run passed -- is a set of **pure functions
over plain data**, testable in milliseconds without a filesystem, without LSF,
and without Arcx. Real jobs take days; validating decision logic by running
them is hopeless.

## 2.2 The single-writer rule

**The daemon is the only process that writes anything.** The web UI and the CLI
never create a directory, never submit a job, never delete anything.

When somebody presses a button, the UI writes an **intent** -- a small JSON
file into a command queue -- and returns immediately. The daemon picks it up
(within about 2 seconds) and does the work.

Two reasons, and the second is the plain one:

1. Two writers on one run folder is the failure this entire system exists to
   avoid, and "the UI only writes when the daemon is not looking" is not an
   invariant anybody can maintain.
2. **The submission gate can wait hours** for the LSF quota to drop. A browser
   request cannot.

## 2.3 Directory structure

```
arcx_auto/
  __init__.py            version, layering docstring
  __main__.py            python -m arcx_auto

  domain/                L0 -- pure, zero I/O
    enums.py             MarkerKind, LsfState, CaseState, Completeness,
                         Severity, IssueScope, IssueStage, WaveState, PlanMode
    models.py            every dataclass: observations, snapshots, specs,
                         groups, batches, waves, plans
    qa.py                Issue, QaResult
    policy.py            PolicyMode, ActionKind, IssueClass, Budgets,
                         ActionHistory, Decision
    rerun.py             rerun plan data types

  adapters/              L1 -- the only side effects
    fs.py                scanning run folders: markers, logs, cmd_files,
                         case roster, index run folder discovery
    arcx.py              dir_map parsing, special.cfg parsing, IndexSpec
                         construction, Arcx command assembly
    arcx_cfg.py          arcx.cfg block parsing, cfg discovery
    lsf.py               bjobs / busers / bkill / bsub / bjobs_manage.py
    lock.py              FileLock (flock), LockBusy
    store.py             RunStore (state.json, events.jsonl, audit.jsonl,
                         policy.jsonl), SnapshotStore

  services/              L2 -- business logic
    collector.py         observe a wave / an index run folder; attach LSF jobs
    state_engine.py      PURE. observation + previous -> CaseSnapshot
    state_resolver.py    PURE. base state + issues -> final state
    monitor.py           one complete scan: collect -> transition -> QA ->
                         resolve
    wave_planner.py      PURE. IndexSpec list -> WavePlan
    workspace.py         WorkspaceBuilder: create wave and batch directories
    launcher.py          bsub the Arcx parent job, record launch.json
    submission.py        PURE gate evaluation + SubmissionController
    submitter.py         wire preflight -> workspace -> gate -> launch
    commands.py          the intent queue (pending/running/done)
    executor.py          execute a queued command; validate every path first
    drafts.py            a submission being built in the UI
    browse.py            directory listing for the file picker
    autogroup.py         form groups from a directory automatically
    workspaces.py        the workspace registry (which daemon owns which root)
    fileview.py          bounded, confined file reading for the UI
    rerun_planner.py     PURE. decide which case dirs a rerun would move aside
    remediator.py        execute a rerun: stop, drain, gate, move, resubmit
    policy.py            PURE policy evaluation + budget reconstruction
    exporter.py          publish the shared-disk status page
    qa/
      registry.py        the @qa_check decorator and the registry
      context.py         CaseContext / IndexContext / PreflightContext
      runner.py          run the registry against a snapshot, cache POST
      expectations.py    arcx.cfg -> which artifacts a case must produce
      checks_case.py     per-case checks
      checks_index.py    per-index checks
      checks_config.py   arcx.cfg validation
      checks_preflight.py  whole-batch pre-submission checks
      summary_table.py   PURE parser for the QC comparison table

  daemon/                L3 -- orchestration
    loop.py              the tick loop, signal handling, command serving
    state.py             build the state.json payload

  web/                   L4
    server.py            routing, Origin check, request -> page
    html.py              primitives: page(), table(), cards(), progress_bar()
    pages.py             monitoring pages (home, run, index, case, file view)
    submit_pages.py      submission flow pages
    export_page.py       the standalone shared-disk page

  cli/
    main.py              argparse, one cmd_* function per command
    render.py            text rendering for the terminal

  util/
    atomic.py            atomic_write_json, read_json (tmp + os.replace)
    textfmt.py           small text helpers

bin/arcx-auto            POSIX sh launcher (resolves its own symlink)
config/default.yaml      a full, commented copy of every default
docs/
  architecture.md        the long-form design document
  usage.md               the engineer-facing guide
  flow.md                ASCII diagrams of the whole mechanism
  handover.md            this file
tests/
  fixtures/fake_run.py   THE MOST IMPORTANT TEST TOOL (see 10.2)
  test_*.py              23 test modules
```

## 2.4 Module relationships (the main flows)

**Monitoring (every daemon tick):**

```
Collector (fs + lsf adapters)
    |  observations: markers, log sizes, cmd_file exec paths, LSF jobs
    v
StateEngine  (pure)          previous snapshot + observation -> base state
    |                        QUEUED RUNNING STALLED LOST SUSPENDED
    v                        COMPLETED_MARKER PENDING
QaRunner  (registry)         LIVE checks every tick
    |                        POST checks once per case after .complete
    |                        INDEX POST only once EVERY case has finished
    v
StateResolver (pure)         base state + issues -> final state
    |                        COMPLETED_MARKER + clean   -> DONE
    |                        COMPLETED_MARKER + problem -> FAILED
    |                        RUNNING + quiet too long   -> STALLED
    v
RunStore  ->  state.json (the UI reads this), events.jsonl, audit.jsonl
Exporter  ->  shared disk (every 60s)
PolicyEngine (shadow) -> policy.jsonl
```

**Submission:**

```
UI draft  ->  groups (dir_map + arcx.cfg + index keys, per group)
    v
WavePlanner (pure)       -> WavePlan: waves, each with batches
    v
QA PRE checks            -> FATAL removes the submit button
    v
command queue (intent)
    v
Daemon -> CommandExecutor -> WorkspaceBuilder -> gate -> Launcher (bsub)
```

---

# 3. Data model

All of these are frozen dataclasses in `domain/models.py` unless noted.

## 3.1 Observation (what was seen)

| Type | Meaning |
|---|---|
| `CaseObservation` | One case at one moment: markers present, case dir, log path/size/mtime, exec path from the cmd_file, attached LSF job |
| `IndexRunObservation` | One index run folder: its cases, plus anomalies (unknown markers, logs that map to no case, directories that are not cases, unclassified entries) |
| `LsfJobView` | One row of `bjobs`: job id, state, exec_cwd, sub_cwd, output_file, exec_host, job_name, and `truncated` (bjobs cut a path short) |

**Observations record facts and decide nothing.** That separation is what lets
the deciding be pure.

## 3.2 Verdict (what it means)

| Type | Key fields |
|---|---|
| `CaseSnapshot` | `state` (final), `base_state` (structural), `entered_state_at`, `last_progress_at`, `last_progress_size`, `lsf_job_id` (sticky), `lsf_job_matched` (right now), `lsf_missing_since`, `case_dir`, `log_path`, `exec_path`, `marker_inconsistent`, `note` |
| `IndexRunSnapshot` | index key, run folder, cases, updated_at, error |
| `StateEvent` | ts, index key, case id, from_state, to_state, reason, evidence |

Two fields deserve explanation:

- **`base_state` vs `state`.** `base_state` is what StateEngine derived from
  markers and LSF alone. `state` is what StateResolver produced after QA.
  Transitions compare **base to base**; comparing against the resolved state
  would make `COMPLETED_MARKER -> DONE` look like a change on every tick and
  reset the clock forever.
- **`lsf_job_id` is sticky, `lsf_job_matched` is not.** The id survives ticks
  where nothing matched, because "which job was this" stays useful. The boolean
  says whether that id is live or a memory. Without both, the UI cannot tell
  "no job was ever found" from "the job has gone" -- and those mean completely
  different things (see 7.17).

## 3.3 Planning

| Type | Meaning |
|---|---|
| `DirMap` | parsed dir_map: `entries` (key -> path), warnings, reserved keys skipped |
| `IndexSource` | an index path plus its GDS count, when a dir_map was supplied |
| `IndexSpec` | the resource footprint: index key, path, `gds_count`, `cpu_per_case`, `slots = cpu_per_case * gds_count`, keywords, priority, `cpu_estimated`, `error`, and the derived `folder` (the parent directory of the index path) |
| `SubmitGroup` | one selection a person made: name, dir_map, arcx.cfg, index keys, `keep_folders_together` |
| `WaveBatch` | one source folder's share of a wave: name, folder, indices |
| `Wave` | seq, indices, group, dir_map, arcx_cfg; derives `name` (`wave_001`), `total_slots`, `total_cases`, `folders`, `batches` |
| `WavePlan` | mode, max_slots_per_wave, waves, excluded, warnings, created_at |

## 3.4 QA

| Type | Meaning |
|---|---|
| `Issue` | id, severity, title, message, scope, stage, index_key, case_id, evidence (a dict), doc (the check's docstring) |
| `QaResult` | issues for one target, with `passed` and `completeness()` |

`Issue.doc` is the check function's own docstring, surfaced in the UI. That is
deliberate: the explanation of a check lives exactly once, next to the check.

## 3.5 Policy

`PolicyMode` (OFF / SHADOW / ACTIVE), `ActionKind` (rerun_wave / escalate /
ignore), `IssueClass` (transient / infra / setup / tool / verify_fail /
unknown), `Budgets`, `ActionHistory`, `Decision`.

`IssueClass` answers "why does this happen", which decides whether retrying can
ever help. A `setup` failure retried a hundred times fails a hundred times.

## 3.6 On-disk state

```
~/.arcx-auto/                        state root; only the daemon writes here
  workspaces/<run_id>.json           which run_root each daemon owns
  runs/<run_id>/
    state.json                       what the UI reads (a CACHE, deletable)
    events.jsonl                     state changes, append only
    audit.jsonl                      every write action and why
    policy.jsonl                     what automatic handling decided
    daemon.lock                      flock + pid, so `arcx-auto stop` can find it
  drafts/<draft_id>.json             a submission being built (the UI writes)
  commands/
    pending/<ts>-<id>.json           written by the UI
    running/<ts>-<id>.json           claimed by the daemon via os.replace
    done/<ts>-<id>.json              finished, with the result appended
```

**`state.json` is a cache.** Delete it and the next scan rebuilds it from the
run folders. The filesystem is the only truth.

---

# 4. External file formats

This section is the part that cannot be reinvented. Every format below belongs
to Arcx, LSF, or the team's conventions. All of it is configurable in
`config/default.yaml` -- an assumption about the outside world belongs in
settings, never in code.

## 4.1 dir_map (Perl hash, always named exactly `dir_map`)

```
%dir_map =(
"1000" => "/proj/chipA/corner_v2g/Cbest_T/blockA/index1000"  ,
"1001" => "/proj/chipA/corner_v2g/Cbest_T/blockA/index1001"  ,
"1002" => "/proj/chipA/corner_v2g/Cworst_T/blockA/index1002"  ,
"min" => "1000"
"max" => "1002"
);
return 1 ;
```

- One dir_map per working directory, always that exact name.
- `min` and `max` are **reserved keys, not indices**. They must be skipped.
- Real-world quirk: the `min` line has **no trailing comma**. The parser must
  not rely on commas.
- Keys are numeric strings but must be treated as opaque strings.
- The path points at the **index directory**. The level **above** it is the
  classification (see 7.15).

## 4.2 arcx.cfg (the extraction configuration)

```
1 BEGIN_SETTINGS: blocking_naming_qcap
1 QC_FLOW = calQCAP
1 RCX_TECH_QTF = /path/to/calQCAP.qtf
1 RCX_LAYER_NAME_MAP = /path/to/calQCAP.map
END_SETTINGS

1 BEGIN_SETTINGS: blocking_naming_qrcfs
1 QC_FLOW = calQRCFS
1 RCX_TECH_QTF = /path/to/calQRCFS.qtf
END_SETTINGS
```

- The **leading column is an enable flag**: `1` enables the line, `0` disables
  it, `#` comments it out. A disabled line never takes effect, so validating it
  would be pure noise -- checks must skip disabled keys.
- A file holds several blocks. **Block name + `QC_FLOW` decide which artifacts
  a case must produce** (see 4.6).
- Paths inside the cfg **must be absolute**, because the cfg is snapshotted into
  the wave directory and a relative path would then resolve somewhere else.
  There is a FATAL preflight check for this.
- One working directory holds a **family** of cfgs, one per corner:
  `chipA_typical.cfg`, `chipA_Cbest_T.cfg`, `chipA_cbt.cfg`, ...

## 4.3 special.cfg (one per index directory)

```
# Arcx special config
O_QCAP_LSF_NUM = 4
O_EXTARCTION = PARA
O_SOMETHING_ELSE = foo
```

- `O_QCAP_LSF_NUM` is the CPU count per case. `slots = O_QCAP_LSF_NUM *
  GDS count`, and slots are what the wave cap counts.
- **`O_EXTARCTION` is spelled that way in the real files.** It is not a typo to
  fix; matching the real spelling is the requirement.
- Values may be quoted, may use `=` or `:`, may carry a scope prefix such as
  `g:O_EXTARCTION = PARA`, and `#` starts a comment.
- The file may be missing or unreadable. That is **not** a reason to exclude the
  index (see 7.20).

## 4.4 Markers (inside an index run folder)

```
.queue.NDIO_1
.run.PDIO_1
.complete.NTN_1
```

- The case id is a **cell name**, never a sequence number. It can contain
  underscores and digits. The regex must stay loose.
- Precedence is `complete > run > queue`. All three can exist at once if Arcx's
  tidy-up did not finish; that is recorded as `marker_inconsistent`, not
  resolved silently.
- A marker outside these three is **abnormal and must be reported**
  (`INDEX_HAS_UNKNOWN_MARKER`, severity UNKNOWN -- we do not know what it means,
  so a human decides).
- **NFS caveat:** killed jobs leave `.nfsXXXX` files. Any fingerprint of the
  directory used for a quiescence gate must ignore them, and must not recurse.

## 4.5 Logs and the cmd_file mapping

```
submit_bjob_cmd_file_1.log          the log -- its name carries ONLY a number
cmd_folder/cmd_file_1               the script that produced it
```

The log filename says nothing about which case it belongs to. The mapping is:

```
submit_bjob_cmd_file_N.log  ->  cmd_folder/cmd_file_N  ->  `cd <path>`  ->  case
```

A cmd_file looks like:

```
#!/bin/csh -f
source /some/env/setup.csh
setenv ARCX_SOMETHING 1
cd /proj/.../wave_001/Cbest_T_blockA/1000_run/NDIO_1
<tool> -in NDIO_1.gds -out NDIO_1.spf
```

**The rule is: take the FIRST absolute `cd` path, and the case is its
basename.** This was corrected during development -- taking the last `cd` broke
on cases whose scripts change directory again (the `QC_Cc` bug). If a `cd` under
the run folder exists it is preferred, otherwise the first absolute one is used.

**The case roster comes from markers UNION cmd_files -- never from the
directory listing.** A case with no directory yet is still a case; a directory
that is not a case must not be turned into one.

## 4.6 Artifacts a case must produce

```
<case_dir>/<block>_<flow>/work_<flow>/<netlist>
```

For the two known flows:

```
blocking_naming_qcap_calQCAP/work_calQCAP/CCI_DB.spice
blocking_naming_qrcfs_calQRCFS/work_calQRCFS/<case_id>.spf
```

`<block>` and `<flow>` come from the arcx.cfg blocks, so **what a case must
produce is derived from the cfg that ran**, not hard coded.

## 4.7 The QuickCap signature (false-success rule 1)

A `calQCAP` netlist's **first line must mention "QuickCap"**. A netlist can
exist, be large, and still be truncated -- existence and size checks both pass.
The cheapest real evidence that the engine ran is the banner it stamps.

Conditional on `special.cfg`:

| Signature | `O_EXTARCTION` | Verdict |
|---|---|---|
| present | anything | pass |
| absent | `R` (resistance only) | pass -- that engine never runs |
| absent | anything else | **FATAL** -- the netlist is not complete |
| absent | cannot read special.cfg | **UNKNOWN** -- we genuinely cannot tell |

Reading special.cfg only when the signature is missing is what keeps this quiet:
a healthy netlist never needs the file at all.

## 4.8 The QC comparison table (false-success rule 2)

Found in `QC_Spice/Report_QC_spice_Summary*`. The table **follows a
`refReport =` line**; everything before it is input bookkeeping:

```
input report ... (preamble, ignored)
refReport = /path/to/reference
rep item refReport cmpReport1 diffCmp1
cell_a total_cap 1.234 1.240 0.006
cell_b total_cap 2.000 2.010 0.010
########
```

- The header is the first non-empty line after the marker. **Its width defines
  the table** -- `cmpReport2`, `cmpReport3` and their diffs may or may not be
  present, so the number of columns must never be assumed.
- Rows run until a blank line or an end marker (`########`).
- The first two columns are names; the rest are values.
- A value is bad when it is **empty**, contains **"fail"**, or equals the
  **`1e+15` default-failure sentinel**.

> The `1e+15` case is the reason this parser exists. It **parses as a float**,
> so a naive "is it numeric" check waves it straight through. That is exactly
> the false success the system is for.

- If no table is found at all, that is `SUMMARY_TABLE_UNREADABLE` with severity
  **UNKNOWN**, not FATAL: an unfamiliar report shape says nothing about the run.
- This rule currently applies to **QC_Spice only**. The other `QC_*` comparisons
  need string handling that could not be specified yet.

## 4.9 LSF commands

```
bjobs -u <user> -o "jobid stat exec_cwd:512 sub_cwd:512 output_file:512 exec_host job_name" -noheader
```

Sample output:

```
101 RUN /work/run/wave_001/1000_run/case1 /work/sub /work/run/wave_001/1000_run/submit_bjob_cmd_file_1.log host01 arcx_child
102 PEND - /work/sub - - arcx_child
103 SSUSP /work/run/wave_001/1001_run/case2 /work/sub /work/log2 host02 arcx_child
```

- `-` means "no value".
- **The explicit column widths matter.** Without them bjobs truncates paths to
  its own default width, and a truncated path matches no case -- which shows up
  as jobs that cannot be found, not as an error. A value that still comes back
  cut short is marked `truncated`, used as the prefix it is, and **never
  compared for equality**.
- `bjobs` lists **unfinished jobs only**. A job that finishes simply disappears.
- One call returns every job the account owns, and filtering happens in memory.
  **Never call bjobs per job** -- a few hundred invocations hammer the master.

```
busers <user>              -> NJOBS column, located by header, not by position
bkill <job_id>             -> stop the Arcx parent
bjobs_manage.py -djp <dir> -> drain jobs under a path
bjobs_manage.py -jp  <dir> -> count jobs under a path
```

`bjobs_manage.py` prints a summary, not a job list. The number that matters is:

```
grep all jobs...
finished, total 304 jobs
total 299 jobs in path          <- this one, scoped to the requested path
```

## 4.10 The Arcx invocation

```
Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 --run
Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 -keep_dir --run      (rerun)
```

Submitted through bsub, **as one shell string**:

```
bsub -q <queue> -oo Arcx.log -J <job name> "Arcx -p arcx.cfg -d 1000 -lsf0 -nt 50 --run"
```

Two details that are expensive to get wrong:

1. The Arcx invocation is **one argument** to bsub, not separate argv entries.
2. **bsub must be executed from inside the directory the run belongs to.** LSF
   records the submission directory and Arcx creates its per-index run folders
   relative to it. Isolation depends entirely on the cwd.

`-nt 50` limits **how many cases Arcx runs at once within one index**. It is
per-Arcx-invocation, and the project owner confirmed that multiplying it across
several invocations is not a problem.

**Arcx is submitted rather than spawned** so the daemon can be restarted,
crash, or be killed without touching work that is already running -- and these
runs last for days. The outer job is only a coordinator; the real work is
submitted by Arcx as further LSF jobs.

## 4.11 Run directory layout (what the tool creates)

```
<run_root>/<run_id>/wave_001/          the unit the gate releases
  chipA_typical.cfg   dir_map          snapshots taken at submit time
  .arcx_auto/
    manifest.json                      this wave's full intent, incl. batches
    lock                               stops the same wave being reworked twice
    special_cfg/<index>.cfg            snapshot of each index's special.cfg
    attempts/                          where a rerun backs up failed state
  Cbest_T_blockA/                      ONE PER SOURCE FOLDER -- Arcx runs here
    chipA_typical.cfg   dir_map        its own copies
    .arcx_auto/manifest.json launch.json lock special_cfg/ attempts/
    1000_run/  1001_run/               created by Arcx itself
      .queue.X .run.Y .complete.Z
      submit_bjob_cmd_file_N.log
      cmd_folder/cmd_file_N
      <case_id>/<block>_<flow>/work_<flow>/<netlist>
      QC_Cc/ QC_Ct/ QC_Spice/          reports Arcx assembles at the end
  Cworst_T_blockA/
    ...
```

- An index run folder is named `<basename of the index path>_run`. Discovery
  **matches that pattern** rather than listing every directory -- listing swept
  up unrelated folders and reported QA failures against things that were never
  cases.
- A **batch directory is told from a case directory by its own `.arcx_auto/`**,
  never by name: a source folder called `something_run` produces a batch
  directory whose name matches the run-folder pattern.
- Snapshotting rather than referencing the originals is deliberate: QA three
  days later has to read the cfg the run actually used, and the original being
  edited in the meantime is normal.

---

# 5. Feature list and status

| # | Feature | Status | Notes |
|---|---|---|---|
| 1 | dir_map / special.cfg / arcx.cfg parsing | done | with the real-world quirks above |
| 2 | Case roster from markers + cmd_files | done | never from the directory listing |
| 3 | StateEngine (pure state machine) | done | 10 case states |
| 4 | LSF job attachment by path | done | see 7.17 for the matching rules |
| 5 | QA registry, 39 checks | done | CASE / INDEX / WAVE / GLOBAL x PRE / LIVE / POST |
| 6 | False-success detection: netlist exists / size / banner | done | rule 1 of 2 |
| 7 | False-success detection: QC_Spice summary table | done | rule 2 of 2, QC_Spice only |
| 8 | Wave planning with slot cap | done | pure |
| 9 | Folder-atomic wave packing | done | per group, default on |
| 10 | Batch directories (one Arcx run per source folder) | done | |
| 11 | Workspace builder + snapshots | done | wave and batch level |
| 12 | Submission gate (quota / interval / max wait) | done | pure evaluation |
| 13 | Launcher (bsub the Arcx parent) | done | records launch.json per batch |
| 14 | Preflight checks blocking submission | done | FATAL removes the button |
| 15 | Daemon tick loop, tiered polling | done | 30s active / 300s idle |
| 16 | Command queue (UI intents) | done | claim via os.replace, ~2s latency |
| 17 | Rerun: plan, drain, safety gate, move aside, resubmit | done | manual trigger only |
| 18 | Rerun scope = one source folder | done | was per wave before batches |
| 19 | Policy engine | **shadow only** | every rule ships as `escalate` |
| 20 | Shared-disk export | done | static HTML, every 60s |
| 21 | Web UI: monitoring pages | done | home / run / index / case |
| 22 | Web UI: full submission flow | done | file picker, groups, checks, submit |
| 23 | Auto select group | done | corner -> cfg, with alias table |
| 24 | Workspace registry (several directories, one browser) | done | |
| 25 | Log / file viewer in the browser | done | confined and bounded |
| 26 | Case table filtering | done | URL state, no JavaScript |
| 27 | Run page: grouped view + global flat filter/sort | done | |
| 28 | Progress bars (run, cfg, folder, global) | done | bar + numeric legend |
| 29 | Refresh that keeps scroll and open sections | done | the only real JavaScript |
| 30 | CLI: status, plan, submit, rerun, daemon, start, stop, web, policy, export, check-cfg, inspect | done | |

**Explicitly deferred** (asked about, decided against for now):

- Notification on completion (email / message).
- Run-to-run comparison.
- A "run this again" shortcut.
- Displaying QC summary values in the UI.
- Per-case rerun (argued against: the wave/batch is the safe unit).
- Time estimates (argued against: no basis for an honest number).

---

# 6. UI design and concepts

## 6.1 Principles

1. **Problems first, everywhere.** Every page answers "is anything wrong"
   before it shows any table. The front page names every case needing a person
   across every run, before the run list.
2. **The UI never acts.** Every button writes an intent. See 2.2.
3. **No build chain, no framework, no CDN.** Inlined CSS, no external fetches.
   The only JavaScript is a checkbox helper and the refresh-position keeper.
4. **State lives in the URL.** Filters, sorts and views are query parameters, so
   a view can be sent to a colleague and the auto-refresh reloads into the same
   view instead of jumping back to the default.
5. **A destructive button always shows what it will touch first**, and the
   confirmation carries the exact list back, so the executor can refuse if the
   world moved in between.
6. **A FATAL check removes the submit button** rather than refusing the press.
   A button you are allowed to press and then told off for is worse than no
   button.
7. **Say what is unknown.** "Could not check" is displayed as UNKNOWN, never as
   a pass and never as a failure.

## 6.2 Routes

```
GET  /                          home: all runs, workspaces, global progress
                                ?sort=<col>&dir=asc|desc
GET  /run/<run_id>              one run: issue summary, index list
                                ?view=flat  ?show=<filter>  ?sort= ?dir=
GET  /run/<run_id>/index/<key>  case table   ?show=<state|attention>
GET  /run/<run_id>/index/<key>/case/<case_id>
                                evidence, log tail, files, QA issues
GET  /view?path=&mode=&lines=&back=      the confined file viewer
GET  /api/state/<run_id>        raw state.json
GET  /healthz
GET  /submit                    drafts list
GET  /submit/<draft>            the draft being built
GET  /submit/<draft>/auto       directory picker for auto grouping
GET  /pick/<draft>/<field>      file picker (dir_map / arcx_cfg)
GET  /commands                  the intent queue
GET  /rerun?wave_dir=&run_id=&back=      rerun confirmation
POST /submit/new | /submit/<draft>/{browse,pending,add,drop,workspace,
     folders,autoplan,autoadd,check,go} | /rerun | /pick/<d>/<f> |
     /commands/cancel | /submit/discard
```

Every POST checks `Origin`. A missing `Origin` is accepted (some browsers omit
it for same-origin form posts, and refusing those breaks the UI for the person
it serves).

## 6.3 Page concepts

**Home.** Cards (attention, runs), a global progress bar with a numeric legend,
the cross-run attention list, the workspace table (which daemon owns which
directory), then the sortable runs table.

**Run page.** Cards, progress bar + legend, finished / LSF / daemon banners, the
issue summary, then the index list.

- The **issue summary** groups by issue id (`NETLIST_MISSING x 200`, not 200
  identical lines). Every target is a link straight to that case or index.
  Past twelve targets the rest go behind a `+N more` disclosure -- nothing is
  dropped.
- The **index list** has two shapes. Grouped (cfg -> source folder -> the index
  table) shows the shape of the run. A filter chip flattens it into one table,
  because "show me everything unfinished" is a global question whose answer is
  otherwise spread across every container. `grouped` restores it in one click.
- Each container carries **its own progress bar on its closed line**, so a
  section can be read without opening it. A collapsed view is only better than
  a flat one if that is true.

**Index page.** State count cards, the rerun link (only when something is
actually wrong), filter chips over the case table, the case table, scan
anomalies.

**Case page.** Cards (state, in state, log quiet, log size), the facts table,
QA issues worst-first with their docstring and evidence -- **every path in the
evidence is a link to the file viewer** -- then the tail of the log inline, then
the files the case produced. The file list is shown **even when empty**, because
"produced nothing" is the finding.

**File viewer.** Bounded and confined. See 7.13.

## 6.4 Refresh behaviour

A monitoring page must update itself, but a plain meta refresh throws away the
reader's scroll position and every section they opened -- every 30 seconds,
which makes reviewing a long run impossible.

The reload is driven by a small inline script that saves the scroll position and
the open/closed state of every `<details data-key=...>` into `sessionStorage`
before reloading, and restores them after. Sections that appear later keep
whatever the server decided for them. The header carries an `auto refresh: 30s`
toggle and a `refresh now` button. `sessionStorage` access is wrapped in
try/catch throughout, and a `<noscript>` meta refresh remains as the fallback.

---

# 7. Key design decisions and why

This is the most valuable section for a rewrite. Each of these was paid for.

**7.1 The filesystem is the only truth.** `state.json`, the shared-disk page and
the export are all caches. Delete any of them and the next scan rebuilds. No
piece of state exists only in memory, so the daemon can be killed at any moment.

**7.2 One writer.** See 2.2. A rerun additionally takes a lock on the wave (now
batch) directory, so two of them cannot both move the same directories aside.
The lock was originally defined and never acquired -- two reruns a minute apart
both passed the quiescent gate and both moved directories. Do not repeat that.

**7.3 State and Issue are different kinds of thing.** A **state** is unique and
exclusive (a case is in exactly one). An **issue** is one of many that coexist.
They are computed by different machinery (a pure state machine vs a pluggable
registry) and combined one-way only: issues can narrow a state, never the
reverse. This is what keeps "add a new check" from being a state machine change.

**7.4 "Could not check" is never a pass.** `Severity.UNKNOWN` is first class and
blocks success exactly like FATAL. If special.cfg cannot be read, if the report
shape is unfamiliar, if LSF is unreachable -- the answer is UNKNOWN, and a human
decides.

**7.5 The one deliberate exception is the rerun delete list.** There, unsure
means **delete**: wasting a run is recoverable, shipping a truncated result is
not. This asymmetry is intentional and documented; everywhere else uncertainty
stops and asks.

**7.6 Pure functions are the design investment.** StateEngine, WavePlanner,
StateResolver, gate evaluation, policy evaluation and the summary-table parser
are all pure. Real jobs take days; a pure function lets "the job ran for three
days and then got stuck" be enumerated in milliseconds.

**7.7 Group vs Wave vs Batch.**
- A **group** is what a person said belongs together: one dir_map, one
  arcx.cfg, the indices they ticked.
- A **wave** is what may go out at once: the slot cap splits a group, and the
  gate releases one wave at a time. Wave numbering is global across groups,
  because the number is the release order.
- A **batch** is where Arcx actually runs: one per source folder inside a wave.
  Arcx creates its run folders relative to its cwd, so keeping two source
  folders apart on disk means starting Arcx twice.

**7.8 A batch directory has the same shape as a wave directory**, so it is the
same type. The launcher, the rerun planner and the remediator work on one
without knowing batches exist -- and a rerun became per-folder for free.

**7.9 The command queue uses `os.replace` and nothing else.** Claiming is an
atomic rename from `pending/` to `running/`; two daemons racing, one wins and
the other gets `FileNotFoundError`. **An interrupted command is never retried**
-- a submit that was interrupted may already have created directories and sent
jobs, and repeating it would double-submit. It is retired to `done/` with an
explanation instead. Being visible and wrong is better than being repeated.

**7.10 The submission gate is a pure function** of (now, NJOBS, previous submit
time, settings): release when the minimum interval has elapsed AND (NJOBS is
below the threshold OR the maximum wait has elapsed). It can wait hours, which
is the whole reason the UI cannot do the work itself.

**7.11 Shadow mode for policy.** Every rule ships as `escalate`. The engine
decides what it *would* do and journals it; it has no way to reach LSF or the
filesystem, so "decides but does not act" is the absence of a capability rather
than a flag something might forget to check. Budgets are rebuilt from
`policy.jsonl` on every evaluation, never held in memory, so a restart cannot
reset them. The risk of an automated action is never doing the wrong thing
once; it is doing the right thing two hundred times.

**7.12 Origin check, not authentication.** Binding to loopback stops the network
reaching the server; it does not stop a page open in the same browser from
posting to it. Without the check, an open tab could trigger a rerun.

**7.13 The file viewer is confined and bounded.** Confined: the path is resolved
with `realpath` **first** -- anyone who can write into a run folder can drop a
symlink, and a check on the literal path would see something under the root and
say yes -- and must land inside `run_root` or inside a wave directory the daemon
is actually monitoring. No roots configured denies everything. Bounded: a tail
seeks to the end and reads backwards in blocks, so a 400MB netlist costs what a
log costs.

**7.14 The case roster never comes from the directory listing.** It is markers
UNION cmd_files. A directory that is not a case must not become one, and a case
that has no directory yet must not disappear.

**7.15 Waves are cut between directories, not through them.** The parent
directory of an index path is how the work is classified, so:
- the keyword priority moves a **whole folder** (a folder is as urgent as its
  most urgent index) -- sorting individual indices is itself what tears folders
  apart;
- small folders still share a wave, because the gate has a minimum interval and
  one wave per folder would turn twenty small folders into hours of waiting;
- a folder over the cap is **kept whole anyway** and reported
  (`PREFLIGHT_WAVE_OVERSIZED`), because over the cap is preferable to cut in
  half -- but deliberate is not the same as invisible;
- no reordering to fill the gaps: first fit in the chosen order keeps "why is
  this index in this wave" answerable, which is worth more than a few percent of
  slot utilisation.

**7.16 Batch directory names carry two path components** (`Cbest_T_blockA`).
The last component alone collides constantly -- `blockA` exists under every
corner -- and one directory holding two different `blockA`s is exactly the
mixing this prevents. A collision that survives that gets a suffix derived from
the path, never a counter, so the same plan always produces the same names.

**7.17 LOST requires evidence that a job existed.** This was a real bug that
made the UI cry wolf constantly. Two parts:
- **A `.queue` marker is Arcx's own queue, not LSF's.** Arcx runs only so many
  cases at a time within one index, so a queued case has no LSF job **by
  design**. Counting `.queue` as "a job should exist" made every case waiting
  its turn LOST after five minutes.
- **Never having found a job is not evidence that one is gone.** A case reaches
  LOST only if a job was matched to it at some point and has since left
  `bjobs`. One that was never matched stays RUNNING and says so.
- The grace period (30 minutes) covers the lag inside a normal case: an LSF job
  leaves `bjobs` the moment it finishes and Arcx writes `.complete` some time
  afterwards.
- The gap this leaves -- nothing watching a queued case -- is covered by
  `INDEX_QUEUE_NOT_MOVING`: cases queued while **nothing in that index is
  running**. The measure is how long the index has been idle, never how long a
  case has been queued, because "queued for six hours" is a fact about the size
  of the index while "six hours with nothing running" is a fact about the run.

**7.18 A job is matched to a case by path**, because the child jobs Arcx submits
carry no identifiable name. Matching accepts only "the job cwd is **under** the
case path", never the reverse: matching the other way would attach the parent
Arcx job (which runs in the index run folder) to every case beneath it, and the
whole table would show one job id. Not matching is better than matching wrongly.

**7.19 A workspace is a `run_root`.** `run_root` defaults to `./arcx_runs`,
which means "beside the work" -- one workspace per project directory. That only
works if the pieces can see each other, so each daemon registers the root it
owns in `<state_root>/workspaces/`, the UI lists them, and a submission carries
its chosen `run_root` **in the command payload**. `state_root` is the opposite
and must be one fixed place per person: the queue and the locks live there. The
daemon's run id is derived from its `run_root` (a readable label plus a short
hash of the path) rather than from the clock, so restarting continues the same
page instead of creating a ghost.

**7.20 An index that cannot be sized is still runnable.** `special.cfg` missing
means the *sizing* is unknown, not the work. The configured default is used, the
spec records `cpu_estimated`, and `PREFLIGHT_SLOTS_ESTIMATED` says so before
submission. Only an index with **no GDS at all** cannot run.

**7.21 `config/default.yaml` must be a faithful transcription of the code
defaults**, enforced by a test. The file is copied into `~/.arcx-auto/config/`
during setup, so every value in it silently overrides a code default from then
on. This drift wasted a week: `default_cpu_per_case` was changed from 0 to 4 in
code, the shipped file still said 0, and indices kept being excluded exactly as
before.

**7.22 An error message names the setting that caused it.** "cannot read
O_QCAP_LSF_NUM, cannot size the work" reads as a fact about the index and sends
somebody looking for a missing file. It was a setting.

**7.23 Write files encode-first.** `open(p, "wb").write(s.encode("ascii"))`
**truncates the file before the encode raises** -- Python evaluates `open()`
before the argument. A stray non-ASCII character in a docstring destroyed a
source file this way, twice. Always:
```
data = s.encode("ascii")     # raises here, before anything is opened
with open(p, "wb") as fh: fh.write(data)
```

---

# 8. Known problems and limitations

**8.1 The policy engine has never run in ACTIVE mode.** Every rule ships as
`escalate`. Turning any of it on is a decision to make against the shadow log,
after it has watched real failures. Treat the ACTIVE path as unproven code.

**8.2 QA covers QC_Spice only.** The `QC_Cc` and `QC_Ct` comparison reports are
checked for existence and file shape, but their contents are not parsed. The
owner could not specify those string formats yet.

**8.3 The shared-disk export page is static.** No filtering or sorting there --
there is no server to re-render it. Adding either means shipping JavaScript into
that page, which is a different trade-off from the rest of the UI.

**8.4 No pagination anywhere.** A run with thousands of cases renders one large
page, and `state.json` is read whole on every request. This has not bitten yet
at the real scale (hundreds of cases) but it is the obvious scaling wall.

**8.5 `bjobs` output is parsed by whitespace splitting.** A `job_name`
containing a space would shift columns. `job_name` is not used for anything, so
this is currently harmless -- but it is a latent trap.

**8.6 The `disable_qcap_golden` ancestor walk climbs to the filesystem root.**
It is memoised per directory, so the cost is a stat per directory in the tree
rather than per level per index, but there is no configured ceiling.

**8.7 A `.queue` case is now watched only by `INDEX_QUEUE_NOT_MOVING`.** That
check fires only when the whole index is idle. A single case that Arcx silently
drops while other cases keep running would not be reported. This was a conscious
trade against a rule that cried wolf constantly.

**8.8 One wave is now several `bsub` calls**, one per source folder. Each Arcx
parent carries the same `-nt` value. The owner confirmed this is fine because
`-nt` is per-index, but it does mean more coordinator jobs than before.

**8.9 Rerun assumes a single machine.** `flock` is used for the wave/batch lock;
that is sufficient only because reruns are executed from one fixed host.

**8.10 There is no test against a real Arcx or a real LSF.** Everything is
driven by the fixture generator and a fake LSF. The formats in section 4 are the
contract; if they drift, the tests will still pass and the tool will be wrong.

**8.11 `arcx-auto stop` finds daemons by scanning run stores for lock files.**
It does not consult the workspace registry, so a daemon whose run store was
removed by hand cannot be stopped by name.

---

# 9. Original requirements, as given

Collected from the whole conversation, in the owner's own framing.

## 9.1 Hard constraints (never negotiable)

- Pure ASCII everywhere in the project; all conversation in Traditional Chinese.
- Python 3.9.10, air-gapped, **zero third-party dependencies**.
- Simple storage. **Explicitly not SQLite.**
- Single user, local. Each person works under their own account and disk, and
  cannot reach anybody else's run path. Others only *view* exported files.
- Git: develop, commit and push only to the designated feature branch.

## 9.2 Non-negotiable design principles (stated up front)

1. Never operate on the same run folder twice at once -- file lock.
2. Automatic actions must have budget limits and must never flood the LSF queue.
3. When uncertain, stop and hand to a human. **Better to miss than to act
   wrongly.**
4. Every write action must have an audit log.

## 9.3 The operating model the owner described

1. Open the daemon / web UI / browser (one script, one terminal, Chrome).
2. Enter a daemon process.
3. Select the arcx.cfg and dir_map paths -- with the daemon's current path
   offered for clicking.
4. PRE check.
5. Submit.
6. Monitor.
7. Decide about failures.

## 9.4 Requirements added during development

- False-success rules 1 and 2 (sections 4.7, 4.8). "These failures must be
  checked by an engineer before any rerun; automatic rerun comes later, once it
  is stable."
- Report QA must run **only after every marker is complete**, because Arcx
  writes the reports after all cases finish.
- Index run directories are named `{index basename}_run`, and only those.
- The case for a cmd_file is the basename of the **first** absolute `cd`.
- Default CPU per case is 4 when `special.cfg` has none -- do **not** make the
  index unselectable.
- Default LSF quota threshold 10000, not 100.
- A way to stop the daemon; Ctrl-C must work even while waiting at the gate.
- Relative `run_root` (`./arcx_runs`) is the preferred style; several working
  directories at once, one browser.
- Auto select group: point at a directory, read its `dir_map` and cfg family,
  propose the groups. Rules:
  - corner = the path component **after** `corner_v2g` (not always the last);
  - corner -> `{naming}_{corner}.cfg`, matched through a user-defined alias
    table (`Cbest_T`, `cbest_t`, `cbt`, `c_best_t` are one corner);
  - no corner -> `{naming}_typical.cfg`;
  - **no matching corner cfg -> do not assign**;
  - `disable_qcap_golden` in an index directory **or any directory above it**
    excludes it (read only -- those directories are not ours to write to);
  - keyword exclusion list, default `*_old *bak *backup *back`, user-editable;
  - **manual override of every automatic decision is required.**
- Indices from the same folder must go out in the same wave, with a per-group
  switch, default on. A folder over the cap is **not** split -- go over the cap
  and warn instead.
- One directory per source folder inside a wave, with its own dir_map and
  arcx.cfg copies; named with two path components to avoid collisions.
- The run page must group indices by cfg and folder, collapsible, keeping the
  existing index table format.
- Issue summary targets must be links; no truncation, or expandable.
- Global filtering and sorting by status (unfinished / fatal / pend / done),
  flat rather than per-container, with a way back to the original view.
- An overall progress bar over all cases, and one per corner.
- The refresh must not destroy the reader's position and open sections.

## 9.5 Judgement calls the owner made (do not re-litigate)

- "Do not make the index unselectable; just default to 4."
- "The case is the first `cd`, not the last."
- "Report QA only after all markers are complete."
- "Remembering the last selection is not acceptable -- it does not survive a
  path change or a different user. Use a flag file plus keyword filtering."
- "Rather exceed the slot cap than split a folder, but warn about it."
- "`-nt` is not a problem; it only limits concurrency within one index."
- "Only build features 1 and 3" (when offered six) -- the owner prunes scope
  deliberately and expects proposals to be pruned, not expanded.

---

# 10. Advice for the rebuild

## 10.1 What to keep without question

- **The layering and the one-way dependency rule.** It survived every change in
  this project, including two that reshaped the on-disk layout.
- **Purity of the decision logic.** Every hard bug that was caught cheaply was
  caught because the logic was a pure function with a test.
- **Observation / verdict separation.** Facts in one type, judgement in another.
- **Severity.UNKNOWN as a first-class outcome.**
- **The settings file as the home of every external assumption.** Every regex,
  every filename, every threshold. Plus the drift test from 7.21 on day one.
- **The intent queue.** It is twenty lines of `os.replace` and it removed an
  entire class of concurrency problem.

## 10.2 The fixture generator is the most important test tool

`tests/fixtures/fake_run.py` builds a complete, realistic run directory --
markers, logs, cmd_files, nested artifacts, report directories, deliberate
anomalies -- in milliseconds. It can produce, per case: complete, running,
queued, stalled, inconsistent markers, an orphan directory, an orphan marker, a
case with no cmd_file; and per artifact set: full, missing netlist, empty
netlist, no signature, missing flow directory, nothing at all.

Build this **first**, before the checks it will test. Every QA rule, every state
transition and every false-success scenario in this project is tested against
it. Without it, none of this logic could have been validated at all, because a
real run takes days.

## 10.3 What to design differently from the start

1. **Make the batch (one source folder) the primary unit, not the wave.** This
   project grew waves first and retrofitted batches, and although the retrofit
   was clean -- because a batch directory has the same shape as a wave -- the
   naming still reads as though a wave is where Arcx runs. It is not. A wave is
   only the gate's release unit.
2. **Carry the grouping metadata (cfg, source folder, batch) into the state
   payload from the beginning.** The UI grouping in section 6.3 needed it, and
   it had to be threaded back through the manifest afterwards.
3. **Decide the URL-state pattern for the UI up front** (filter, sort, view as
   query parameters). It was applied inconsistently at first and then had to be
   unified.
4. **Do not build the policy engine until the QA checks are trusted.** It is the
   most speculative subsystem, it has never been used in anger, and it can be
   added later without disturbing anything -- which is itself evidence that it
   did not need to be early.
5. **Treat `state.json` as a versioned schema from commit one.** It has a
   `schema_version` field; use it, and write the migration path before it is
   needed.
6. **Consider a "watched paths" ceiling** for any upward directory walk.

## 10.4 Working style that fitted this owner

- Every commit message states the problem it solves before the change, and
  explains *why* in prose. The owner reads them.
- Comments explain **why**, never what. Several reviewers of this code will be
  models with no context; a docstring that explains a decision is the highest
  value text in the file.
- Deliver a tarball plus a sha256 after each change; the environment is
  air-gapped and files move by hand.
- When proposing features, expect roughly half to be rejected. Propose with
  reasons and accept the pruning.
- Raise a concern once, clearly, then do what was asked.
- Ask for real samples of any external format before writing a parser for it.
  Every format in section 4 came from a real file the owner supplied, and the
  guesses that preceded them were all wrong.

## 10.5 Test inventory (what each module proves)

| Test module | What it protects |
|---|---|
| `test_state_engine` | the pure state machine, including LOST semantics |
| `test_qa` | the registry, index checks, the queue-not-moving rule |
| `test_false_success` | the two false-success rules and the table parser |
| `test_wave_planner` | slot capping, priority, folder-atomic packing |
| `test_submit` | gate, workspace, launcher, batch layout, preflight |
| `test_rerun` | plan, drain gate, move-aside, resubmit |
| `test_autogroup` | corner extraction, aliases, exclusions, disable flag |
| `test_workspaces` | the registry, stable ids, liveness |
| `test_ui_flow` | the browser flow end to end, Origin, intent validation |
| `test_web` | routing, rendering, grouping, filters, sorting, the viewer |
| `test_fileview` | confinement (symlink escape, `..`, prefix), bounds |
| `test_collector` / `test_fs_adapter` / `test_lsf_adapter` | the adapters |
| `test_cli` | every command, and that read-only commands write nothing |
| `test_ascii_only` | every byte of every file is ASCII |
| `test_py39_compat` | no newer syntax; no writes to real shared paths |

Two guard tests are worth copying verbatim in spirit:

- **The hermeticity guard**: fail the suite if the configured shared-disk path
  or the default `run_root` exists after the tests run. It immediately caught
  two test classes writing to a real path.
- **The config drift guard**: see 7.21.

---

# 11. Glossary

| Term | Meaning |
|---|---|
| **Arcx** | the RC extraction driver being automated. Not ours |
| **index** | one unit of work in a dir_map: a directory of GDS files |
| **case** | one cell inside an index. Identified by cell name, never a number |
| **slot** | `O_QCAP_LSF_NUM * GDS count`. The unit the wave cap counts |
| **group** | one (dir_map, arcx.cfg, index keys) selection a person made |
| **wave** | what may be submitted at once. The gate releases one at a time |
| **batch** | one source folder's directory inside a wave. Where Arcx runs |
| **workspace** | a `run_root`, owned by one daemon. Usually one project directory |
| **marker** | `.queue.X` / `.run.X` / `.complete.X` -- Arcx's own progress record |
| **false success** | a run that reports completion and produced a wrong or missing result |
| **LIVE / POST / PRE** | QA stages: while running / after completion / before submission |
| **shadow mode** | the policy engine decides and records, but never acts |
