# Arcx Auto Golden -- Architecture

> Automated submission, monitoring, QA and rerun for RC extraction driven by Arcx.
> The goal is to replace "watch it by hand, dig out problems afterwards" with
> "the system watches, people only make decisions", and so cut turnaround time.

---

## 0. One page

| Aspect | Decision |
|---|---|
| Deployment | **A local, single-user tool.** No server, no multi-user, no auth |
| Interface | A web app bound to `127.0.0.1`, opened in a browser |
| Starting Arcx | **Submitted through bsub**; the job id goes into the wave directory's `launch.json` |
| Batching | **Waves**: sized from `special.cfg` CPU demand, released one at a time |
| Rerun scope | **Per wave**: drain, delete unfinished case run dirs, `-keep_dir --run` |
| Success test | **Artifact existence**, not log regexes; logs only answer "how long has it been quiet" |
| Failure classification | QA function -> issue id -> YAML policy -> action |
| Storage | JSON snapshots plus append-only JSONL. No SQLite |
| Runtime | Python 3.9.10, air-gapped network, **zero third-party dependencies, pure ASCII** |

---

## 1. Layering

```
+======================================================================+
|  L4  Interface Layer        (thin, replaceable, no business logic)   |
|  +--------------+ +--------------+ +------------------------------+ |
|  |  Local Web   | |     CLI      | |  Status Exporter             | |
|  |  (127.0.0.1) | |  arcx-auto   | |  -> shared disk status.json  | |
|  +------+-------+ +------+-------+ +--------------^---------------+ |
+=========|================|========================|==================+
     reads state.json   reads state.json            |
     writes commands/   writes commands/            |
          +----------------+-----------+            |
+========================================|==========|==================+
|  L3  Orchestration                     v          |                  |
|  +-------------------------------------------------+---------------+ |
|  |  Daemon  (the single writer)                                    | |
|  |  tick: collect -> transition -> qa -> policy -> act -> persist  | |
|  +---+----------+----------+----------+----------+------------+----+ |
+======|==========|==========|==========|==========|============|======+
       v          v          v          v          v            v
+======================================================================+
|  L2  Service Layer                    (business logic, testable)     |
|  +-----------++----------++---------++---------++--------++--------+ |
|  |WavePlanner||Submission||Preflight||Collector||  State ||   QA   | |
|  | (pure)    ||Controller||         || (observe)|| Engine ||Registry| |
|  +-----+-----++----+-----++----+----++----+----++---+----++---+----+ |
|        |  +--------v--------+  |         |         |         |       |
|        +->| WorkspaceBuilder|<-+         |    +----v---------v----+  |
|           |   + Launcher    |            |    | Policy + Remedy   |  |
|           +--------+--------+            |    +---------+---------+  |
+====================|=====================|==============|============+
                     v                     v              v
+======================================================================+
|  L1  Adapter Layer               (the only place with side effects)  |
|  +----------+ +----------+ +----------+ +----------+ +------------+  |
|  | FsAdapter| |LsfAdapter| |ArcxAdapt.| |  Store   | | LockManager|  |
|  |scandir/  | |bsub bjobs| |dir_map   | |json/jsonl| |  flock     |  |
|  |stat/tail | |busers    | |special   | | atomic   | |            |  |
|  |          | |bjobs_mgr | |cfg/cmd   | |          | |            |  |
|  +----------+ +----------+ +----------+ +----------+ +------------+  |
+======================================================================+
                                   v
+======================================================================+
|  L0  Domain Model         (pure data, pure functions, zero I/O)      |
|  Run / Wave / IndexRun / Case / Observation / Issue / Action / Event  |
+======================================================================+

Dependencies point one way:  L4 -> L3 -> L2 -> L1 -> L0
```

**The one architectural rule: dependencies only point downwards.** L0 imports
nothing, and L2 reaches the outside world only through L1 interfaces.

The concrete payoff: the whole state machine and QA logic can be exercised on a
machine with **no LSF, no NFS and no Arcx**, against fake run folders.

---

## 2. Five key decisions

### Decision 1: the daemon is the single writer

The UI and the CLI are read only. Actions are posted as `commands/*.json`, which
the daemon consumes, executes and deletes.

**Why.** Two non-negotiable rules say "never touch the same run folder twice at
once" and "every write action is in the audit log". Rather than adding locking
and audit calls at every entry point -- and eventually missing one -- the
architecture gives writes exactly one path.

The side effects are all good: the UI can be closed, crashed or replaced without
disturbing the daemon, and the audit log is complete because there is no bypass.

### Decision 2: the filesystem is the truth; state.json is a cache

Kill the daemon, reboot the machine, corrupt `state.json` -- one rescan of the
run folders rebuilds everything.

**Why.** Jobs run for days. Any design where state lives only in memory or in a
database produces, on its first unexpected restart, the ghost story where the
system believes work is running that died hours ago. That is harder to diagnose
than the original problem.

The requirement this imposes: every field in `state.json` must be derivable from
the run folders plus LSF. The only exception is history -- retry counts, audit,
who clicked what -- which lives in append-only JSONL and is never rewritten.

### Decision 3: adapters isolate the outside world

`FsAdapter`, `LsfAdapter` and `ArcxAdapter` are the only modules that touch NFS,
LSF or Arcx.

**Why.** It is the only way to develop and test offline. Together with a
`FakeRunFolder` generator -- which produces queued, running, stalled, partial and
truncated-artifact situations -- logic can be validated in seconds instead of
after a three-day job reveals the judgement was wrong.

### Decision 4: observe, interpret, judge, decide -- kept apart

```
Observation (fact)  ->  State (interpretation)  ->  Issue (judgement)  ->  Action
    pure I/O             pure function              pluggable function    YAML rules
```

**Why.** These four change at completely different rates. Observation barely
changes; the state machine is tuned occasionally; QA functions keep being added;
policy changes weekly. Mixed together, changing one policy line means editing
I/O code, and eventually nobody dares touch it.

### Decision 5: the planner produces a plan, not an action

`WavePlan` is serialisable, previewable in the UI, editable by hand, and
replayable from storage. Computing it creates nothing.

**Why.** Wave planning depends on parsing `special.cfg` and inferring priority
from path keywords, both of which are estimates. Making the plan visible,
editable data is what lets an engineer overrule it, and lets estimates be
compared with reality afterwards.

---

## 3. Data flow

### 3.1 From selecting indices to running jobs

```
+- user input ------------------------------------------+
|  dir_map | arcx.cfg | chosen indices | wave settings   |
+----------------------------+--------------------------+
                             v
        +---------------------------------+
   (1)  |  WavePlanner (pure)             |<-- <index>/special.cfg (O_QCAP_LSF_NUM)
        |  slots -> order -> split         |<-- config: max_slots_per_wave, keywords
        +----------------+----------------+
                         v
                +----------------+
                |   WavePlan     |  pure data; previewable and editable
                | wave1: idxA,B  |
                | wave2: idxC !  |
                +--------+-------+
                         v
        +---------------------------------+
   (2)  |  Preflight                      |  any FATAL -> stop, create nothing
        +----------------+----------------+
                         v  (all clear)
        +---------------------------------+   +--------------------------+
   (3)  |  SubmissionController            |-->| <run_root>/<run_id>/     |
        |  checks the gate each tick       |   |   wave_001/  (isolated)  |
        +----------------+----------------+   |   wave_002/              |
                         v                    +--------------------------+
        +---------------------------------+
   (4)  |  WorkspaceBuilder + Launcher    |
        |  create dirs, snapshot cfg, bsub|--> arcx_job_id -> launch.json
        +---------------------------------+
```

### 3.2 The monitoring loop, once per tick

```
+---------------------- TICK (15-60s, tiered) --------------------------+
|                                                                       |
|   +-------------+   +-------------+   +------------------+            |
|   |  FsProbe    |   |  LsfProbe   |   |  CmdFileResolver |            |
|   | scandir for |   | one bjobs   |   | read cmd_file    |            |
|   | .queue/.run/|   | call for    |   | to map a log to  |            |
|   | .complete   |   | every job   |   | its case (cached)|            |
|   | + log size  |   |             |   |                  |            |
|   +------+------+   +------+------+   +--------+---------+            |
|          +-----------------+-------------------+                      |
|                            v                                          |
|                   +-----------------+                                 |
|                   |  Observation    |  an immutable snapshot          |
|                   +--------+--------+                                 |
|                            v                                          |
|        prev_state ---> +-------------+ ---> new_state + Event[]       |
|                        | StateEngine |      (pure)                    |
|                        +------+------+                                |
|              +----------------+----------------+                      |
|              v (cases in a terminal state)     v                      |
|      +---------------+                  +----------+                  |
|      |  QA Registry  |                  |  Store   |                  |
|      +-------+-------+                  +----------+                  |
|              v  Issue[] (id, severity, evidence)                      |
|      +---------------+<---- config/policy.yaml (issue_id -> action)   |
|      |  PolicyEngine |<---- budgets / cooldown / kill switch          |
|      +---+-------+---+                                                |
|   auto v         v escalate                                           |
|   +----------+  +--------------+                                      |
|   |Remediator|  | TriageQueue  |--> UI: "needs your decision: N"      |
|   +----+-----+  +--------------+                                      |
|        v  the rerun state machine (section 6)                         |
|                                                                       |
|   -----> written together: state.json (atomic), events.jsonl,         |
|          audit.jsonl                                                  |
+-----------------------------------------------------------------------+
                                     |
                                     v  (every 60s)
                    Status Exporter -> shared disk status.json + .html
```

### 3.3 How a user action flows

```
  UI: "rerun wave_002"
        |
        v  write commands/<uuid>.json  {type: rerun, target: wave_002, by, ts}
        v
  The daemon reads it next tick -> validates -> audits -> executes -> deletes
        |
        v  the result lands in state.json, and the UI shows it on refresh
```

The UI never touches a run folder (decision 1).

---

## 4. Module dependencies

| Module | Layer | Depends on | Pure-function testable |
|---|---|---|---|
| `domain/` | L0 | nothing | -- |
| `FsAdapter` | L1 | domain | needs a tmpdir |
| `LsfAdapter` | L1 | domain | needs a mock |
| `ArcxAdapter` | L1 | domain | needs a tmpdir |
| `Store` | L1 | domain | needs a tmpdir |
| `LockManager` | L1 | -- | needs a tmpdir |
| `WavePlanner` | L2 | domain | **fully pure** |
| `SubmissionController` | L2 | domain, LsfAdapter | gate logic is pure |
| `Preflight` | L2 | all of L1 | needs a mock |
| `WorkspaceBuilder` | L2 | FsAdapter, ArcxAdapter | needs a tmpdir |
| `Launcher` | L2 | LsfAdapter, ArcxAdapter | needs a mock |
| `Collector` | L2 | FsAdapter, LsfAdapter | needs a mock |
| `StateEngine` | L2 | domain | **fully pure** |
| `QaRegistry` | L2 | domain, FsAdapter | almost pure |
| `StateResolver` | L2 | domain | **fully pure** |
| `PolicyEngine` | L2 | domain, Store | **fully pure** |
| `Remediator` | L2 | LsfAdapter, FsAdapter, Launcher | needs a mock |
| `Daemon` | L3 | all of L2 | integration |
| `WebUI / CLI` | L4 | Store (read), domain | -- |

The modules marked fully pure are the system's brain and the easiest place to
be wrong, which is exactly why they hold no I/O.

**There are no cycles.** WavePlanner does not know Launcher exists; QA does not
know Policy exists; Policy does not know Remediator exists. The daemon does all
the wiring at L3, so replacing any module means changing only that wiring.

---

## 5. Wave scheduling

### 5.1 Why waves

Releasing every index at once floods the LSF queue. Waves spread submission over
time, and **each wave needs its own isolated directory**: several Arcx instances
sharing one cwd interfere with each other.

### 5.2 How a plan is computed

```
Step 1  for each index:
          read <index_path>/special.cfg  -> O_QCAP_LSF_NUM = cpu_per_case
          count <index_path>/*.gds*      -> gds_count
          index_slots = cpu_per_case * gds_count
          match path keywords (sram, ro, ...) -> priority

Step 2  stable sort by priority; selection order is preserved within a priority

Step 3  fill waves in order up to max_slots_per_wave
          an index over the cap on its own gets a wave to itself, marked
          OVERSIZED

Step 4  emit a WavePlan: pure data, previewable, editable, replayable
```

Step 3 deliberately does no bin-packing optimisation: the selection order and
keyword priority are explicit intent, and reordering makes the outcome
unpredictable.

### 5.3 Three modes

| Mode | Behaviour | When |
|---|---|---|
| `AUTO` | Split by the slot cap, released by the gate | the normal case |
| `MANUAL` | The user assigns indices to waves | to control order or priority |
| `OFF` | One command, one directory, everything at once | few indices, or to match the old behaviour |

All three produce the same `WavePlan` shape -- OFF is just "one wave" and MANUAL
is just "the grouping came from a human" -- so nothing downstream branches on the
mode.

### 5.4 Wave states and the gate

```
PLANNED --> WAITING_GATE --> SUBMITTING --> SUBMITTED --> MONITORING --> DONE
                 ^                                            |
                 +--------------------------------------------+
```

```yaml
gate:
  min_interval_sec: 600      # hard minimum between submissions (debounce)
  quota_threshold: 100       # release only when busers NJOBS is below this
  max_wait_sec: 7200         # force a release, so a stuck quota cannot block
```

    release = min_interval elapsed AND ( NJOBS < quota_threshold OR max_wait elapsed )

A plain OR has a hole: once the timer expires, submitting while the quota is
still full floods the queue anyway. This combination covers "not too dense",
"not flooding" and "never stuck forever" at the same time.

An unreadable `NJOBS` must **not** be treated as a low quota; that would submit
hardest exactly when LSF is in trouble. Only `min_interval` and `max_wait` decide
in that case, and the reason says the data was unavailable.

A forced release must be recorded plainly in the UI and the audit log, so that a
long PEND afterwards is understood rather than mistaken for a fault.

The gate state (`gate_entered_at`, `last_njobs`, `checked_at`) lives in
`state.json`, so a daemon restart resumes rather than restarting the clock.

### 5.4.1 The submission flow

```
       plan_waves                pure; produces a WavePlan
            |
            v
   +------------------+
   | QA PRE checks    |  arcx.cfg validation + preflight (disk, LSF, quota,
   +--------+---------+  target directories, clashes)
            |  any FATAL -> **nothing is touched**, no half-built state
            v
   +------------------+
   | WorkspaceBuilder |  create wave dirs; snapshot arcx.cfg, dir_map, special.cfg
   +--------+---------+
            v
   +------------------+
   | SubmissionCtrl   |  release wave by wave through the gate (pure decision)
   +--------+---------+
            v
   +------------------+
   | Launcher         |  bsub Arcx from inside the wave dir; job id -> launch.json
   +------------------+
```

**Runs from a snapshot, not the originals.** The cfg is copied into the wave
directory and Arcx runs against that copy. QA three days later has to read the
settings the run actually used -- the original being edited in the meantime is
normal, and "what was configured at the time" is the most useful thing there is
when debugging. The price is that paths inside the cfg must be absolute, which
preflight enforces.

**bsub must run from inside the wave directory.** LSF records the submission
directory and Arcx creates its per-index run folders relative to it, so wave
isolation depends entirely on the cwd.

**Dry run is the default.** Without `--yes`, `submit` only checks and prints the
commands and touches no disk. A dry run also **never waits at the gate**: the
point of a preview is seeing every wave in one go.

### 5.5 A failed wave does not block later waves

A case failing in wave_001 does not stop wave_002. Only tripping
`same_issue_burst_limit` -- meaning something systemic, such as a broken cfg --
pauses further submission and escalates. Otherwise one small failure holds up the
whole batch, defeating the purpose.

---

## 6. The rerun / drain state machine

Arcx's own completion logic is complex, but there is one guaranteed contract:
**delete a case's run dir and `-keep_dir --run` will redo it.** So the internals
do not need to be understood; only "which directories to delete" does.

```
RERUN_REQUESTED
    |
    v
STOPPING_PARENT       bkill <arcx_job_id>     kill the parent first, or it
    |                                          simply submits replacements
    v
DRAINING_CHILDREN     bjobs_manage.py -djp <wave_dir>/, wait, recount;
    |                 repeated up to drain_attempts (default 3 x 10s) because
    |                 the deletion can lag several seconds behind the request
    |  count still > 0 after the last attempt, or unreadable
    |       +--------> ABORT and escalate.
    v
VERIFY_QUIESCENT <-- [SAFETY GATE] K consecutive confirmations (default 3 x 30s):
    |                 (a) bjobs_manage.py -jp <wave_dir>/ reports 0 jobs
    |                 (b) the marker set has not changed meanwhile
    |                 A marker that moves means something is still writing:
    |                 the confirmation count restarts from zero.
    |  on timeout (default 15 min) or any failed confirmation
    |       +--------> ABORT and escalate. Never force past it.
    v
DECIDE_CLEAN_SET      QA decides which cases are unfinished; the delete list
    |                 goes into the audit log, and in manual mode the UI shows
    |                 it for confirmation
    v
BACKUP                unfinished run dirs -> <wave>/.arcx_auto/attempts/N/
    |                 (mandatory)
    v
CLEAN                 delete those run dirs. Intermediate files, databases and
    |                 QC_* are left alone: -keep_dir --run rebuilds them
    v
RESUBMIT              bsub "Arcx -p cfg -d <same indices> -keep_dir --run"
    |                 same wave dir as cwd; attempt + 1
    v
MONITORING
```

`bjobs_manage.py -jp` reports a **count**, not a job list:

```
grep all jobs...
finished, total 304 jobs      <- every job
total 299 jobs in path        <- scoped to the path; this is the one
```

**A count that cannot be parsed is unknown, never zero.** Treating "could not
tell" as "no jobs left" would delete files while jobs are still running, which is
the single most destructive mistake this system could make.

### 6.1 One deliberate exception: when unsure, delete

The two ways `DECIDE_CLEAN_SET` can be wrong have **asymmetric** costs:

| Mistake | Consequence | Severity |
|---|---|---|
| Complete case judged unfinished, deleted and rerun | one wasted run; **the result is still correct** | low |
| Unfinished case judged complete, kept | Arcx skips it; **a truncated result ships as a success** | **high** |

So on this one decision the default is "**when unsure, delete and rerun**", the
opposite of the rest of the system. The recoverable mistake is preferred to the
unrecoverable one.

In practice QA returns `COMPLETE`, `INCOMPLETE` or `UNKNOWN`. `UNKNOWN` enters
the delete list by default, but is coloured differently in the UI and can be
unticked, and `BACKUP` preserves the evidence either way.

**Two sources, and only the worse one wins.** Completeness has two independent
witnesses, and they can disagree:

| Source | Sees | Blind to |
|---|---|---|
| State machine (markers) | `.complete` present, still `.run`, nothing at all | whether the artifacts behind `.complete` are real |
| QA `POST` checks | missing netlists, empty reports, wrong case counts | anything on a case that never reached `POST` |

A case that is still `RUNNING` never runs `POST` checks, so QA reports no issues
at all -- which naively reads as "clean". So the two are combined by **rank, not
by preference**:

```
COMPLETE (0)  <  UNKNOWN (1)  <  INCOMPLETE (2)          worse wins
```

QA can only ever **downgrade** the state machine's verdict, never upgrade it.
That is what stops "no issues found" on an unfinished case from being mistaken
for success.

### 6.2 Blockers: refuse before touching anything

The plan is built before a single job is killed, and it carries `blockers` --
conditions under which the rerun is simply not executable:

  * no `arcx_job_id` recorded, so the parent cannot be stopped (it would
    resubmit the very cases we deleted)
  * no index keys, so there is nothing to resubmit
  * no `arcx.cfg` snapshot in the wave dir, so the rerun would use a different
    config than the original run

A plan with blockers is refused by the executor, not attempted and aborted
halfway. `plan.executable` is checked before `STOPPING_PARENT`.

### 6.3 One wave directory, one writer

The rerun takes an exclusive `flock` on `<wave>/.arcx_auto/lock` for the whole
destructive sequence, and refuses rather than queues if it is held.

Without it two reruns started a minute apart both pass the quiescent gate --
each genuinely sees zero jobs, because the other has not resubmitted yet --
both move the same run dirs aside, and both resubmit, leaving two Arcx parents
writing into one wave. The lock file lives in the wave directory itself, so it
covers every machine that mounts the share rather than only one host.

A dry run takes no lock: it reads and prints, and blocking a preview because
someone else is mid-rerun would be worse than useless.

### 6.4 What the safety gate is allowed to call "movement"

The gate restarts its confirmation count whenever the marker set changes, so
the definition of "marker" decides whether the gate ever settles. It reads one
level down, in the index run folders, and matches only names shaped like a
marker.

Neither restriction is cosmetic:

  * NFS renames a file deleted while still open to `.nfs0000...`, and those
    appear **exactly** when jobs are being killed -- the moment this gate runs.
    A fingerprint counting every dot-file would churn on every reading, never
    reach K confirmations, and abort every rerun for a reason that has nothing
    to do with safety.
  * Recursing would stat every netlist and every `QC_*` report on NFS, three
    times, inside a 15 minute deadline.

An index directory that cannot be listed is not evidence of quiet: it feeds a
value that differs from any real reading, so the gate keeps waiting instead of
concluding that nothing moved.

### 6.5 First version is manual trigger only

Phase 3 ships the mechanism, not the autonomy: a rerun happens only when a
person runs `arcx-auto rerun <run> --yes`. Without `--yes` the command prints
the plan (delete list, keep list, uncertain list, blockers) and exits, changing
nothing. Automatic triggering is Phase 4, and it will run in shadow mode first.

`VERIFY_QUIESCENT` can be passed but never skipped; there is no force flag.
Every step -- `rerun_started`, `rerun_parent_stopped`, `rerun_drained`,
`rerun_quiescent_confirmed`, `rerun_cleaned`, `rerun_resubmitted` -- writes to
`audit.jsonl` before it happens, so an interrupted rerun leaves a readable
trail of exactly how far it got.

---

## 7. QA registry and policy

### 7.1 Three stages, one output type

Judging only "did it succeed" is not enough: half the lost turnaround comes from
"stuck and nobody noticed". Waiting for `.complete` before running QA throws away
every observation made while the job was running.

| Stage | Question | When | Cost |
|---|---|---|---|
| `PRE` | can this run at all? | before submission, once | low |
| `LIVE` | is it healthy right now? | every tick | low (uses existing observations) |
| `POST` | did it really succeed? | once, after `.complete` | high (reads the run dir) |

All three emit the same `Issue` (id, severity, evidence) into the same policy
engine. That is why they are one registry rather than two systems.

`POST` is expensive and its answer cannot change, so it is cached per
`(run_folder, case, attempt)`. The attempt number is the whole point of that
key: a rerun rebuilds the run dir from nothing, and the previous verdict then
describes files that have been moved into `.arcx_auto/attempts/`. The monitor
reads the attempt from the wave's `launch.json`, which counts submissions, so
every index run folder in a wave advances together and a rerun invalidates the
cache by construction.

Getting that wrong is not a stale-display problem, it is the exact failure this
system exists to prevent: a long-lived daemon would keep serving the pre-rerun
verdict, so a case that failed and was successfully rerun reads as failed
forever -- and a case that passed, was rerun and then broke reads as a success.

### 7.2 State and Issue are different things

| | State | Issue |
|---|---|---|
| Count | **one, exclusive** | **many can coexist** |
| Meaning | "where it is now" | "what is wrong with it" |
| Change rate | barely ever | constantly growing |
| Producer | StateEngine (pure) | QA functions (pluggable) |

Three layers, always flowing one way (issue -> state):

```
StateEngine    markers + LSF only  ->  base_state
               in {PENDING, QUEUED, RUNNING, SUSPENDED, COMPLETED_MARKER, LOST}
                        |
QA Registry    emits Issue[]
               LIVE ->  CASE_QUIET / LSF_SUSPENDED / CASE_NEVER_STARTED / ...
               POST ->  NETLIST_MISSING / NETLIST_EMPTY / FLOW_DIR_MISSING / ...
                        |
StateResolver  base_state + issues  ->  final_state
               COMPLETED_MARKER + anything blocking  ->  FAILED
               COMPLETED_MARKER + all clear          ->  DONE
               RUNNING          + FATAL CASE_QUIET   ->  STALLED
               everything else is left alone (LOST and SUSPENDED are facts LSF
               confirmed, and QA has no business rewriting them)
```

**Why "stuck" is decided in QA rather than StateEngine**: its conditions keep
being tuned (cell size, quiet phases, whether artifacts are already present).
Inside StateEngine that pure function would grow into a catch-all, and every
tweak would touch core code. In QA, StateEngine stays small and the state stays
single.

`CaseSnapshot` stores `base_state` alongside `state`, because transitions must
compare base against base: comparing against the resolved state would make
`COMPLETED_MARKER -> DONE` look like a change on every tick.

### 7.3 Expected artifacts are derived from arcx.cfg

```
arcx.cfg                                case run dir
------------------------------------------------------------------
g:QCA = Yes                             (shared variables, not checked)

1 BEGIN_SETTING : blocking_nameing_1    NTN_1/
1   QC_FLOW = calQCAP                     blocking_nameing_1_calQCAP/
1   RCX_TECH_QTF = /path/to/file            work_calQCAP/
END_SETTINGS                                  CCI_DB.spice

1 BEGIN_SETTING : blocking_nameing_2      blocking_nameing_2_calQRCFS/
1   QC_FLOW = calQRCFS                        work_calQRCFS/
END_SETTINGS                                    NTN_1.spf
```

The rule is `<block>_<QC_FLOW>/work_<QC_FLOW>/<netlist for that flow>`, and what
each flow produces is declared in settings:

```yaml
qa:
  flows:
    calQCAP:   {work_dir: "work_{flow}", netlists: ["CCI_DB.spice"]}
    calQRCFS:  {work_dir: "work_{flow}", netlists: ["{case}.spf"]}
```

**Adding an EDA tool is a settings change, not a code change.**

This is also why the cfg must be snapshotted into the wave directory at
submission time (section 8): QA three days later has to read the cfg the run
actually used.

### 7.4 Two API levels

**Declarative (YAML)** covers the common "these files must exist and be big
enough", with no Python involved.

**Programmatic (a decorator)** covers checks that need logic. A check is usually
three to ten lines:

```python
@qa_check(id="NETLIST_MISSING", title="netlist missing",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def netlist_missing(case: CaseContext) -> Optional[Issue]:
    """This docstring is what the UI shows.

    It is how false success is caught: Arcx wrote a .complete marker, but the
    file was never produced.
    """
    missing = [a for a in case.expected_artifacts if not case.exists(a.relpath)]
    if not missing:
        return None
    return case.fail("%d netlist(s) missing" % len(missing),
                     evidence={"missing": [a.relpath for a in missing]})
```

The brevity comes from `CaseContext`: paths are relative to the case run dir, it
**caches** (ten checks over one case still scandir each directory once) and it
**never raises** (unreadable means None). The docstring is displayed in the UI,
so documentation and code cannot drift apart.

### 7.5 Three properties that are not negotiable

**1. "I do not know" is a first-class value.** `Severity.UNKNOWN` exists for
exactly that: no cfg snapshot, an unrecognised format, a QA function that threw.
It **can never count as a pass**, and in rerun decisions it leans towards
deleting and rerunning (section 6.1).

**2. A QA function that crashes cannot take the system down.** Engineers are
meant to add their own rules, so some will have bugs. Each check runs inside a
try; one that throws becomes `QA_INTERNAL_ERROR` (UNKNOWN, with a traceback) and
the rest still run.

**3. The id is the interface.** policy.yaml refers to ids, not implementations,
so rewriting a check does not touch policy. Registering a duplicate id raises
immediately -- silent shadowing is the hardest kind of bug to find. Disabling a
check is `qa.disabled_checks`, not deleting code.

### 7.6 "Stuck" is graded, not binary

A single case can take ten minutes or three days, and a log can legitimately go
quiet because the artifacts are already written. A hard rule produces false
alarms, so the system states how long it has been quiet and escalates the visual
weight; **the judgement stays human**:

```yaml
quiet:
  warn_after_sec: 14400          # 4h   uncommon, worth a look        -> WARN
  stalled_after_sec: 28800       # 8h   effectively stuck             -> FATAL
  downgrade_when_artifacts_ready: true   # artifacts present -> one level down
```

### 7.7 PRE checks on arcx.cfg

The highest return of any layer: most configuration mistakes are visible before
anything is submitted, and finding the same mistake afterwards costs hours.

| Check | Severity | What it catches |
|---|---|---|
| `CFG_UNREADABLE` | FATAL | missing or unreadable file |
| `CFG_NO_BLOCKS` | FATAL | nothing would run |
| `CFG_ALL_BLOCKS_DISABLED` | FATAL | runs quietly and produces nothing |
| `CFG_DUPLICATE_BLOCK` | FATAL | output directories overwrite each other silently |
| `CFG_BLOCK_NO_FLOW` | FATAL | no EDA tool flow named |
| `CFG_UNKNOWN_FLOW` | FATAL | artifacts cannot be verified |
| `CFG_PATH_NOT_FOUND` | FATAL | a referenced file is missing |
| `CFG_PARSE_WARNING` | WARN | misspelt keywords, missing END_SETTINGS |

Path checking covers only the keys listed in `qa.cfg_path_keys`, and only on
**enabled** lines. Checking anything path-shaped would produce false alarms on
output paths and on values like `TOOL_VERSION_LVS` that are a command plus
arguments -- and once there are false alarms nobody reads the warnings.

### 7.8 Policy (Phase 4)

```yaml
policies:
  NETLIST_MISSING:     {action: rerun_wave, max_auto: 1}
  NETLIST_EMPTY:       {action: rerun_wave, max_auto: 1}
  FLOW_DIR_MISSING:    {action: escalate}
  CASE_QUIET:          {action: escalate}
  CASE_NEVER_STARTED:  {action: escalate}
  QA_INTERNAL_ERROR:   {action: escalate}

default: {action: escalate}          # when unsure, stop

budgets:
  max_auto_actions_per_run: 20
  cooldown_sec: 900
  same_issue_burst_limit: 5          # a burst of one id -> stop automation
  global_kill_switch: false
```

The automatic-versus-human matrix:

| Class | Example | Handling | Why |
|---|---|---|---|
| `TRANSIENT` | licence briefly unavailable, host down, stale NFS handle | **retry automatically** with backoff | a rerun will very likely pass |
| `INFRA` | memlimit, runlimit, disk full | **retry with adjusted resources** (first time) | programmatically fixable |
| `SETUP` | wrong path, cfg syntax, missing layer map | **never retry; escalate at once** | it would fail identically a hundred times, burning turnaround and machines |
| `TOOL` | segfault, internal error | **retry once**, then escalate | sometimes random, but do not flail |
| `VERIFY_FAIL` | false success | **always escalate, high priority** | the most dangerous; needs judgement |
| `UNKNOWN` | no rule matched | **escalate and invite a new rule** | the system has to admit what it does not know |

---

## 8. Storage and process model

```
~/.arcx-auto/                        per user, never on shared storage
  daemon.lock                        flock; guarantees a single daemon
  config/
    default.yaml  policy.yaml
  runs/<run_id>/
    manifest.json    immutable: creation time, cfg hash, WavePlan, selection
    state.json       mutable snapshot, atomic (tmp -> fsync -> os.replace)
    events.jsonl     append-only state transitions
    audit.jsonl      append-only writes (who / when / on what / why)
    qa/<case>.json   QA results and evidence
  commands/          action intents from the UI or CLI; deleted once consumed
```

```
<run_root>/<run_id>/                 run_id = <timestamp>_<label>
  wave_001/                          one isolated directory per wave (Arcx's cwd)
    arcx.cfg                         snapshot, with a hash
    dir_map                          snapshot
    Arcx.log                         bsub -oo output
    .arcx_auto/
      launch.json                    bsub job id, full command, cwd, time
      special_cfg/<index>.cfg        snapshot of each index's special.cfg
      manifest.json                  this wave's full intent
      lock                           stops the same wave running twice
      attempts/1/                    where a rerun backs up the failed state
    <index run folder>/              created by Arcx
      .complete.NTN_1
      .queue.NDIO_1
      NDIO_1/  PDIO_1/  NTN_1/       per-case run dirs
      QC_Cc/  QC_Ct/  QC_Spice/      reports Arcx assembles
      submit_bjob_cmd_file_1.log     per-case log
      cmd_folder/cmd_file_1          the script, containing `cd <case run dir>`
  wave_002/
```

**The wave directory is where three boundaries coincide**: Arcx's isolation
boundary, `bjobs_manage.py -jp/-djp`'s operating scope, and the scope of a rerun.
One path prefix defines all three, so no mapping table is needed.

**Why JSON and JSONL are enough**: one user, one daemon, one writer, a few
thousand records. Append-only JSONL survives a power cut with at most one damaged
line, and snapshots are atomic through `os.replace`.
> The threshold for moving to SQLite: `state.json` over about 10 MB, or over a
> second to serialise per tick. Store is an L1 adapter, so swapping it does not
> affect anything above.

**Process model**: two independent processes.

```
process A: arcx-auto daemon    long lived under setsid/nohup; the single writer
process B: arcx-auto web       started and stopped freely; reads state.json only
```

No asyncio: `scandir`, `bjobs` and `stat` are all blocking I/O, synchronous code
is easier to follow and to debug, and a single-user scale needs no concurrency.

Three daemon properties:

- **Killable and restartable at any time.** It reloads the previous verdicts from
  `state.json` so stall timers continue, but rebuilds from the run folders even
  without that file.
- **One failed tick cannot kill it.** NFS hiccups and LSF timeouts are routine;
  the error goes into `daemon.last_error` where the UI shows it, and the next
  tick runs. A failed scan **still updates the timestamp in `state.json`**, or
  the UI would show stale data while looking perfectly healthy.
- **The daemon is monitored too.** The UI flags "no update for over 15 min". A
  daemon that dies quietly freezes the display at the last moment and looks
  perfectly healthy, which is the most dangerous state of all.

## 8.1 The web UI

**Standard library `http.server` only; no FastAPI or Flask.**

This is a single-user, loopback-bound, **read-only** dashboard rather than a
service. The standard library is enough, and installing packages on an air-gapped
network is real friction. Zero dependencies also makes "copy the source across
and it runs" true. (A test enforces zero third-party imports in the core; every
new import has to be added to an explicit allowlist.)

Security properties:

- bound to `127.0.0.1` by default, so it serves nobody else and needs no auth
- every route is a GET; there are **no write endpoints**. Phase 3 actions are
  posted through `commands/` files
- run ids are looked up among the existing runs rather than concatenated into a
  path, so there is no room for path traversal

Four levels of drill-down, each rendering only what the daemon already computed:

```
/                                    all runs; "needs your decision: N" is loudest
/run/<id>                            index list plus an issue summary (grouped)
/run/<id>/index/<key>                case table plus scan anomalies
/run/<id>/index/<key>/case/<case>    state, paths, and the evidence per issue
/api/state/<id>                      raw JSON
```

**Issues are grouped by id**: when 200 cases hit the same problem, an engineer
needs "NETLIST_MISSING x 200", not 200 identical lines.

**Quiet time escalates visually** (section 7.6): under 4h plain, 4-8h amber, over
8h red.

---

## 9. External interface contracts

Every assumption about the outside world is collected here. When Arcx or the
environment changes, only the corresponding implementation needs to change.

### 9.1 dir_map (a Perl hash)

```perl
%dir_map =(
"1000" => "/path/to/index1000/"  ,
"1001" => "/path/to/index1001/" ,
"min" => "1000"
"max" => "1014"
);
return 1 ;
```

- entries are extracted with a lenient regex over `"KEY" => "VALUE"`, tolerating
  missing commas, either quote style, and inline comments
- `min` and `max` are reserved meta keys, **not real indices**
- a gap between the declared range and the actual entries is warned about: it is
  invisible to the eye but makes some indices silently never run

### 9.2 arcx.cfg (multiple blocks)

```
g:QCA = Yes
g:O_CAL_SET_ENV = setenv LICENSE 123@lic9

1 BEGIN_SETTING : blocking_nameing_1
1 QC_FLOW = calQCAP
1 PROCESS = xx
1 TOOL_VERSION_LVS = /source.csh tool_build
1 RCX_TECH_QTF = /path/to/file
1 LVS_DFM_DIR = /path/to/dir
1 LVS_DECL = /path/to/file
END_SETTINGS
```

| Element | Meaning |
|---|---|
| leading `1` / `0` | enable / disable. **A `0` line, or one commented with `#`, is not path checked** |
| `g:` prefix | variables shared by every block, outside any block, not checked |
| `BEGIN_SETTING` / `END_SETTINGS` | singular and plural are **inconsistent in real files** and treated as equivalent |
| `QC_FLOW` | picks the EDA tool flow, and names the output directory |

Only an N written as M (`BEGIN_SETTIMG`) is warned about: that misspelling can
make Arcx skip the whole block silently.

Path-checked keys (configurable): `RCX_TECH_QTF`, `RCX_LAYER_NAME_MAP`,
`LVS_DFM_DIR`, `LVS_DECK`, `LVS_DECL`, `LVS_QUERY_CMD`, `RCX_STAR_CMD`.

### 9.3 special.cfg (inside every index path)

```
O_QCAP_LSF_NUM = 4
```

- `cpu_per_case = O_QCAP_LSF_NUM`
- `index_slots = cpu_per_case * gds_count`

### 9.4 Conventions inside an index run folder

```
.queue.NDIO_1                  markers; case ids are cell names
.run.PDIO_1
.complete.NTN_1
NDIO_1/  PDIO_1/  NTN_1/       per-case run dirs (a rerun deletes these)
QC_Cc/  QC_Ct/  QC_Spice/      reports Arcx assembles, not cases
submit_bjob_cmd_file_1.log     logs, named only by sequence number
cmd_folder/cmd_file_1          the submitted script, containing `cd <path>`
```

**Two conventions drive the implementation.**

**1. The case roster comes from markers and cmd_files, never from the directory
listing.** There are three things that could in principle say how many cases a
run has, and only two of them can be trusted:

| Source | Names cases? | Counts cases? |
|---|---|---|
| `cmd_folder/cmd_file_N` | **yes** -- one file, one submitted case | yes |
| `.queue` / `.run` / `.complete` markers | **yes** -- the id is in the filename | yes |
| GDS files in the index path | **no** -- the run uses top cell names, which need not match GDS filenames | roughly |

So the first two build the roster and the third is only ever a cross-check
(`INDEX_CASE_COUNT_MISMATCH`, WARN, and only when a dir_map was supplied).

Directories are then **matched against** that roster, never used to extend it.
`NDIO_1`, `PDIO_1` and `NTN_1` have nothing in common, so a directory cannot be
recognised as a case by its name at all.

This used to work by exclusion -- "not `QC_*`, not `cmd_folder`, not hidden, so
it must be a case" -- and that is a list of the non-case directories somebody
thought of. On a real run folder the ones nobody thought of (`svdb`,
`work_calQCAP`) became two phantom UNKNOWN cases, turning a five case run into
a seven case one. Directories matching no roster entry are now reported as
`unexpected_dirs` and counted as nothing.

Reading the cmd_files **directly**, rather than reaching them through the logs,
is what lets a case submitted seconds ago -- no marker, no log yet -- still
appear. That is exactly when it is most worth seeing.

**2. Log filenames say nothing about their case.**

```
submit_bjob_cmd_file_1.log
    +--(number)--> cmd_folder/cmd_file_1
                       +-- `cd /path/to/index/NDIO_1`
                              +-- basename --> case = NDIO_1
```

The numbering matches **no** ordering, so "the Nth log belongs to the Nth case"
is never valid. A test with `cmd_file_1 -> ZZZ_LAST` and
`cmd_file_2 -> AAA_FIRST` blocks any such shortcut.

This chain also solves LSF job mapping: the execution path a cmd_file names is
deterministic, so log formats never have to be guessed at.

**When it cannot be resolved, it must be visible.** A log with a missing or
unparseable cmd_file means a case we **cannot monitor**; it is recorded in
`unresolved_logs` and shown in the UI. Ignoring it would let the system report
all-clear while partly blind.

**Markers outside the known three** (`.queue`, `.run`, `.complete`) are abnormal.
They are reported separately as `unknown_markers`, and their case id still joins
the case list -- the case exists regardless of a marker kind we do not recognise.

**Arcx keeps its own cfg snapshot in the run folder**, under a name derived
from the user (`zmwu.cfg`). The name cannot be hard coded, so every `*.cfg` in
the folder is parsed and the one with at least one `BEGIN_SETTINGS` block wins;
`special.cfg` is plain `key = value` and fails that test without needing to be
named as an exception.

This is what makes `status --run-folder` work with no arguments. Without it
every case raised `CFG_EXPECTATION_UNAVAILABLE` -- UNKNOWN, which blocks
success -- so five genuinely complete cases reported as FAILED, with the cfg
sitting in the folder the whole time. An explicitly supplied cfg still wins:
that is a deliberate choice and outranks whatever happens to be lying around.

Report directories: `QC_Cc` and `QC_Ct` are always present; others may not be.
Each holds `Report_QC_<X>` and exactly one `Report_QC_<X>_Summary_*`, so more
than one summary is also wrong -- usually a leftover that would feed the wrong
report downstream.

### 9.5 LSF commands

| Purpose | Command |
|---|---|
| Submit Arcx | `bsub -q LVSRCE-0E.q -oo Arcx.log "Arcx -p <cfg> -d <idx...> -lsf0 -nt 50 --run"` |
| Account quota | the `NJOBS` column of `busers` |
| Count jobs under a path | `bjobs_manage.py -jp /abs/path/` |
| Delete jobs under a path | `bjobs_manage.py -djp /abs/path/` |
| Kill the parent | `bkill <arcx_job_id>` |

The Arcx invocation is **one shell string**, matching how the command is written
by hand and how bsub reads its trailing argument. `-oo` overwrites rather than
appends, so a rerun does not stack onto the previous attempt's output.

Drain order is fixed: **bkill the parent first, then `-djp` the children.** The
other way round, the parent simply submits replacements.

### 9.6 The shared-disk export

```
<shared_root>/                  default /tmp1/.auto_golden (configurable)
  index.html                    an overview of every user
  <user>/
    status.json                 full fields
    status.html                 self-contained; opens directly in a browser
    updated_at                  a plain-text timestamp for quick shell checks
```

- writes are always `tmp -> os.replace`, since somebody may be reading
- permissions: directories `0755`, files `0644`
- the update interval is independent of the daemon tick (default 60s)
- **derived data only.** The truth lives in `~/.arcx-auto/` and the run folders,
  so the shared disk can be deleted and rebuilt at any time

---

## 10. Delivery order

| Phase | Contents | State |
|---|---|---|
| **0** | Domain + FsAdapter + Collector + StateEngine + `status` | done |
| **2a** | ArcxAdapter (dir_map / special.cfg) + WavePlanner + `plan` | done |
| **1a** | arcx.cfg parser + QA registry + StateResolver + PRE checks | done |
| **1b** | Store + LockManager + Daemon + read-only web UI | done |
| **2b** | Preflight + WorkspaceBuilder + Launcher + gate + `submit` | done |
| 3 | Rerun drain state machine + triage queue | to do |
| 4 | Policy engine, automatic remediation (shadow mode first) | to do |
| 5 | Shared-disk export, overview page, history | to do |

Phases 0 and 2a were deliberately built first and kept **read only**: neither
writes anything to a run folder, and their purpose was to verify that the system
understands markers, logs, dir_map and special.cfg correctly. Being wrong about
those is cheapest to fix early -- and two of the assumptions did turn out to be
wrong.
