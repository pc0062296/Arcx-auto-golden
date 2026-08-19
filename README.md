# Arcx Auto Golden

Automated submission, monitoring, QA and rerun for RC extraction runs driven by
Arcx.

The goal is to replace "watch it by hand, then dig out the problems afterwards"
with "the system watches, people only make decisions", and so cut turnaround
time.

Full design: **[docs/architecture.md](docs/architecture.md)**.

---

## Status

| Phase | Contents | State |
|---|---|---|
| **0** | Domain + FsAdapter + Collector + StateEngine + `status` | done |
| **2a** | ArcxAdapter (dir_map / special.cfg) + WavePlanner + `plan` | done |
| **1a** | arcx.cfg parser + QA registry + StateResolver + PRE checks | done |
| **1b** | Store + LockManager + Daemon + read-only web UI | done |
| **2b** | Preflight + WorkspaceBuilder + Launcher + gate + `submit` | done |
| **3** | Rerun planner + drain state machine + `rerun` (manual trigger) | done |
| **3.5** | False-success detection: netlist signature + QC_* summary values | done |
| **4** | Policy engine + shadow mode + `policy` | done (shadow) |
| **5** | Shared-disk export, overview page and history | done |

---

## Requirements

* Python 3.9.10 or newer
* **Zero third-party dependencies.** PyYAML is needed only to read a `.yaml`
  settings file; without it, use a `.json` settings file with the same
  structure.
* **Pure ASCII.** The whole tree contains no byte outside the ASCII range, and
  a test enforces it.

---

## Installing it somewhere you can run it from

The package has **no dependencies**, so being importable is the whole
installation. Three ways, in the order they are worth trying:

**1. The launcher (nothing installed).** Symlink it onto your PATH:

```bash
ln -s /path/to/arcx-auto-golden/bin/arcx-auto ~/bin/arcx-auto
arcx-auto --version          # now works from any directory
```

The script resolves its own symlink to find its source tree, so the link can
live anywhere. Set `ARCX_PYTHON` if `python3` is not the interpreter you want.

**2. PYTHONPATH, if you would rather not add a script:**

```bash
export PYTHONPATH=/path/to/arcx-auto-golden:$PYTHONPATH
python3 -m arcx_auto --version
```

Put that line in your shell profile and `python3 -m arcx_auto` works everywhere.

**3. pip, if your site allows it:**

```bash
pip install --user /path/to/arcx-auto-golden      # or: -e for a live checkout
arcx-auto --version
```

No network is needed -- there is nothing to download.

### Where things get written

Two roots, both under your home directory by default, so they do **not** depend
on where you run the command from:

| Setting | Default | Holds |
|---|---|---|
| `state_root` | `~/.arcx-auto` | state.json, events, audit, the command queue |
| `run_root` | `~/arcx_runs` | the wave directories Arcx runs in |

Point `run_root` at the disk you actually want the results on:

```yaml
run_root: "/proj/rc_golden/runs"
```

**A relative root follows your shell's working directory**, which means waves
created from one directory and a daemon started from another never see each
other. The tool warns if you configure one.

### Settings file

Optional; every field has a default. Search order:

1. `-c/--config /path/to/settings.yaml`
2. `./arcx_auto.yaml` (the directory you are in)
3. `~/.arcx-auto/config/default.yaml`

For a per-user setup, copy the shipped template once:

```bash
mkdir -p ~/.arcx-auto/config
cp /path/to/arcx-auto-golden/config/default.yaml ~/.arcx-auto/config/
```

Without PyYAML, use the same structure as `.json` instead.

## Quick start

```bash
# 1. Build a fake run folder covering finished / running / stalled /
#    inconsistent markers / orphan directories and so on
python3 tests/fixtures/fake_run.py /tmp/arcx-demo

# 2. Check that dir_map parses the way you expect
python3 -m arcx_auto inspect dir-map /tmp/arcx-demo/dir_map --verify

# 3. Look at the state of the run folders
python3 -m arcx_auto status --wave-dir /tmp/arcx-demo/wave_001 --no-lsf --detail

# 4. Produce a wave plan (computes only; creates nothing, submits nothing)
python3 -m arcx_auto plan --dir-map /tmp/arcx-demo/dir_map --all \
        --max-slots 100 --show-command
```

## Against real data

```bash
# Parse a real dir_map and check that every index path exists
python3 -m arcx_auto inspect dir-map /path/to/dir_map --verify

# Inspect one index: read O_QCAP_LSF_NUM from special.cfg and count GDS files
python3 -m arcx_auto inspect index /path/to/index1000/

# Monitor a running index run folder (queries bjobs)
python3 -m arcx_auto status --run-folder /path/to/run/1000 --detail

# Keep watching, persisting state so stall timing accumulates
python3 -m arcx_auto status --wave-dir /path/to/wave_001 \
        --state-file ~/.arcx-auto/scan.json --watch 30
```

Every command supports `--json`.

## Submitting

```bash
# 1. See what would happen (a dry run by default; touches no disk)
python3 -m arcx_auto submit --dir-map /path/dir_map --arcx-cfg /path/arcx.cfg \
        --all --max-slots 200 --run-id nightly

# 2. Once it looks right, do it for real
python3 -m arcx_auto submit ... --run-id nightly --yes
```

The flow is **check -> create wave directories -> pass the gate -> bsub**. Any
FATAL stops everything before anything is created, so no half-built state is
left behind.

Each wave directory holds a **snapshot** of `arcx.cfg`, `dir_map` and every
`special.cfg`, and Arcx runs against the snapshot rather than the originals:
QA three days later has to read the settings the run actually used.

The submitted command matches the hand-written form exactly:

```
cd <wave dir>
bsub -q LVSRCE-0E.q -oo Arcx.log "Arcx -p <snapshot cfg> -d 1000 1002 -lsf0 -nt 50 --run"
```

The gate releases when `min_interval elapsed AND (NJOBS < threshold OR max_wait
elapsed)`. A plain OR has a hole -- once the timer expires, submitting while the
quota is still full floods the queue anyway -- so this combination covers "not
too dense", "not flooding" and "never stuck forever".

## The normal way to use it

> For the engineer running the extraction, there is a step-by-step guide in
> **[docs/usage.md](docs/usage.md)** that needs no command line beyond the
> first setup.

```bash
# terminal 1: the daemon -- monitors, and runs whatever the UI asks for
python3 -m arcx_auto daemon

# terminal 2: the UI
python3 -m arcx_auto web            # then open http://127.0.0.1:8765/
```

Naming no directory is the point: the daemon watches every wave under
`run_root`, so a submission made in the browser a minute ago is picked up
without restarting anything.

Then, in the browser:

1. **new submission** -> type a `dir_map` and an `arcx.cfg`
2. tick the indices you want; each row shows its GDS count and slot demand
3. **add this group.** A group is one (dir_map, arcx.cfg, indices) selection --
   add another with a different cfg if some indices need one
4. **run the pre-submission checks.** This creates nothing. A FATAL means the
   submit button is not rendered at all: a button you are allowed to press and
   then told off for is worse than no button
5. **submit.** The browser queues an intent and returns immediately -- the gate
   can wait two hours for the LSF quota, which is not something a page can sit
   through. The daemon does the work
6. the run appears on the monitoring page as it goes
7. where a case needs attention, **rerun this wave...** shows exactly which
   directories would be moved aside, and only then offers the button

A group is what a person selected; a wave is what may go out at once. Ticking
fifty indices does not mean fifty go at once -- the slot cap still splits a
group into waves, because that cap is what keeps the queue from flooding.

**The UI never acts.** Every POST writes an intent into `commands/` and the
daemon, already the single writer, executes it. Two writers on one run folder
is the failure this whole system exists to avoid, and "the UI only writes when
the daemon is not looking" is not an invariant anybody can keep.

Every POST also checks the `Origin` header. Binding to loopback stops the
network from reaching the server, but it does **not** stop a page open in the
same browser from posting to it -- without that check, an open tab could
trigger a rerun that moves directories. `--read-only` turns the buttons off
entirely.

The UI uses only the standard library `http.server`. Plain forms, a POST and a
redirect; the small amount of inline JavaScript is convenience only, and every
action works with it disabled. Four levels of drill-down: all runs -> index
list and issue summary -> case table -> the evidence for one case.

The daemon can be stopped and restarted at any time: it resumes the previous
verdicts from `state.json`, and rebuilds from the run folders even without it.

## Letting other people see it

Only one person runs the daemon. Everybody else opens a file off a mounted
share -- no server, no accounts, nothing to install:

```
<shared_root>/                  default /tmp1/.auto_golden
  index.html                    every user, one row each
  <user>/
    status.html                 one self-contained page
    status.json                 the same data, for scripts
    updated_at                  a plain timestamp for `cat`
```

The daemon publishes on its own slower timer (`export.interval_sec`, 60s) --
the tick is every 30s while cases run, and rewriting files on an NFS mount that
often is rude to everybody who has it mounted. `python3 -m arcx_auto export`
does it once by hand, and reports in full why a share is refusing writes.

The page puts **problems first**: every case needing a person, across every run
including finished ones, before any healthy detail. A run that ended with
failures is still something somebody has to look at.

Everything there is derived. Delete the whole tree and the next export rebuilds
it; the truth stays in the run folders and `~/.arcx-auto/`. Publishing failures
never stop monitoring -- they surface as `export_error` in daemon health, not as
a dead daemon.

## Automatic handling, and why it is off

The policy engine ships in **shadow mode**: it decides what should happen to
every problem and records it, and it acts on none of them.

```bash
python3 -m arcx_auto policy --wave-dir /path/to/wave_001   # what it would do now
python3 -m arcx_auto policy --review nightly               # what it has recorded
```

```
== policy review ==
  issue                 class        would rerun  escalated  budget-stopped  cases
  NETLIST_EMPTY         tool                   1          0               0      1
  NETLIST_NO_SIGNATURE  verify_fail            0          1               0      1

  5 decision(s) recorded: 5 in shadow, 0 acted on.
```

That table is the point. **Every shipped rule is `escalate`**; switching one to
`rerun_wave` is a decision to make once the review says it would have fired on
the cases you would have chosen yourself.

Budgets are the safety mechanism, not a safety net -- the risk of an automated
action is never doing the wrong thing once, it is doing the right thing two
hundred times:

| Guard | Stops |
|---|---|
| `global_kill_switch` | everything, without editing rules |
| `same_issue_burst_limit` | one broken cfg becoming N reruns |
| `max_auto` per rule | the same problem retried forever on one wave |
| `max_auto_actions_per_run` | a run spending its budget on automation |
| `cooldown_sec` | a tight loop of actions |

The engine is a pure function with no access to LSF or the filesystem, so
"decides but does not act" is the absence of a capability rather than a flag.
Budget history is rebuilt from `policy.jsonl` on every evaluation, so restarting
the daemon does not hand it a fresh budget.

## Validating arcx.cfg before submitting

```bash
python3 -m arcx_auto check-cfg /path/to/arcx.cfg
```

Checks for duplicate block names, unknown `QC_FLOW` values, and missing
referenced files. **Lines with a leading `0`, or commented out with `#`, are
not checked** -- those settings never take effect, and verifying them would only
manufacture false alarms. Exits 1 on a FATAL, so it chains into a submit script.

---

## Commands

| Command | Purpose |
|---|---|
| `status --run-folder PATH...` | Scan the given index run folders |
| `status --wave-dir PATH` | Scan every index run folder under a wave dir |
| `status ... --dir-map FILE` | Also cross-check the case count against the index path's GDS count (warning only) |
| `plan --dir-map FILE --index ...` | Produce a wave plan (**never submits**) |
| `submit --dir-map X --arcx-cfg Y` | Check, create wave dirs, submit (**dry run by default**) |
| `rerun --wave-dir PATH` | Stop, drain, back up, clean and resubmit (**dry run by default**) |
| `daemon` | Monitor, and run what the UI asks for |
| `web` | Serve the local UI (`--read-only` for display only) |
| `export` | Publish this user's status to the shared disk, once |
| `policy` | What automatic handling would do (**decides, never acts**) |
| `policy --review RUN_ID` | What it has recorded so far |
| `check-cfg FILE` | Validate arcx.cfg (exit 1 on FATAL) |
| `inspect dir-map FILE` | Parse dir_map and report gaps |
| `inspect index PATH...` | Parse special.cfg and count GDS, giving the slot demand |

Common options: `--json`, `--detail`, `--watch SEC`, `--state-file`, `--no-lsf`,
`-c CONFIG`.

---

## Configuration

The settings file is optional; every field has a built-in default. Search
order:

1. `-c/--config`
2. `./arcx_auto.yaml`
3. `~/.arcx-auto/config/default.yaml`

See [`config/default.yaml`](config/default.yaml). Every assumption about the
outside world -- marker naming, log naming, the `special.cfg` key, LSF commands,
the shared disk path -- lives in settings rather than in code.

---

## Development

```bash
python3 -m unittest discover -s tests -t .     # everything
python3 -m unittest tests.test_state_engine    # one module
```

The tests are **fully offline**: no LSF, no NFS, no Arcx.
`tests/fixtures/fake_run.py` builds any run folder situation in milliseconds,
including the ones that take a real job three days to reach (stalled, job gone,
inconsistent markers).

### The two conventions that shape the code

```
.queue.NDIO_1  .run.PDIO_1  .complete.NTN_1   markers; case ids are cell names
NDIO_1/  PDIO_1/  NTN_1/                      case run dirs (a rerun deletes these)
QC_Cc/  QC_Ct/  QC_Spice/                     reports Arcx assembles, not cases
submit_bjob_cmd_file_1.log                    logs, named only by number
cmd_folder/cmd_file_1                         the script, with `cd <case run dir>`
zmwu.cfg                                      Arcx's snapshot of the cfg it ran
```

**1. The case roster comes from markers and cmd_files, never from the directory
listing.** One `cmd_file_N` is one submitted case, and a marker filename names
one. Case ids are cell names with nothing in common, so a directory can only be
recognised by matching that roster -- it can never extend it.

Deciding by exclusion instead ("not `QC_*`, not `cmd_folder`, so it must be a
case") only ever lists the non-case directories somebody thought of; the ones
nobody thought of become phantom cases. Directories matching no roster entry are
reported under "scan anomalies" as `dir is not a case`, and counted as nothing.

The GDS files in the index path are a third possible source and are deliberately
**not** used for identity -- the run works on top cell names, which need not
match GDS filenames. Pass `--dir-map` and they become a count cross-check that
warns (`INDEX_CASE_COUNT_MISMATCH`), nothing more.

**2. Log filenames say nothing about their case.**
`submit_bjob_cmd_file_1.log` pairs by number with `cmd_folder/cmd_file_1`, and
that script's **first** `cd <path>` names the case -- the script enters the case
run dir first and cds on to `QC_*` later to assemble reports, so taking the last
one reported a report directory as a case. The numbering matches no ordering, and
a test with `cmd_file_1 -> ZZZ_LAST` and `cmd_file_2 -> AAA_FIRST` blocks any
shortcut that guesses from the number.

A log whose case cannot be resolved lands in `unresolved_logs` and is shown
under "scan anomalies": it means a case that cannot be monitored, which must
never be dropped silently.

**3. Arcx leaves its own cfg snapshot in the run folder**, named after the user
(`zmwu.cfg`). Every `*.cfg` there is parsed and the one with at least one
`BEGIN_SETTINGS` block is used, so `status --run-folder` needs no `--arcx-cfg`.
An explicit `--arcx-cfg` still wins.

### Catching a false success

Existence and size checks share one blind spot: a file that is present, large
and wrong passes all of them. Two checks read content instead.

**`NETLIST_NO_SIGNATURE`** -- a `calQCAP` netlist's first line should mention
`QuickCap`, the extraction engine's own stamp. Whether it is required comes from
`special.cfg`:

| first line | `O_EXTARCTION` | verdict |
|---|---|---|
| has it | anything | pass |
| missing | `R` | pass -- resistance only, that engine never runs |
| missing | anything else | **FATAL** |
| missing | special.cfg unreadable | **UNKNOWN** |

`special.cfg` is read only once the wording is already missing, so a healthy
netlist never touches it.

**`SUMMARY_TABLE_BAD_VALUE`** -- in a `QC_Spice` Summary, every column of the
table after `refReport =` must be a real number. Blank, `fail`, or a sentinel
like `1e+15` all mean the comparison produced no answer. The sentinel matters
most: it parses as a float, so a naive numeric check lets it through.

Not finding a table at all is `SUMMARY_TABLE_UNREADABLE` -- UNKNOWN, not FATAL,
because an unfamiliar report shape says nothing about the run.

Both are tunable in `qa.netlist_signature` and `qa.summary_table`.

### QA: expected artifacts are derived from arcx.cfg

```
arcx.cfg                                case run dir
1 BEGIN_SETTING : blocking_nameing_1      NTN_1/
1   QC_FLOW = calQCAP                       blocking_nameing_1_calQCAP/
END_SETTINGS                                  work_calQCAP/
                                                CCI_DB.spice
```

The rule is `<block>_<QC_FLOW>/work_<QC_FLOW>/<netlist for that flow>`, and what
each flow produces is declared in `qa.flows`. **Adding an EDA tool is a settings
change, not a code change.**

To add a check, copy an existing one in
`arcx_auto/services/qa/checks_case.py`:

```python
@qa_check(id="NETLIST_MISSING", title="netlist missing",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def netlist_missing(case: CaseContext) -> Optional[Issue]:
    """This docstring is what the UI shows."""
    missing = [a for a in case.expected_artifacts if not case.exists(a.relpath)]
    if not missing:
        return None
    return case.fail("%d netlist(s) missing" % len(missing),
                     evidence={"missing": [a.relpath for a in missing]})
```

To disable one, add its id to `qa.disabled_checks`; no code needs deleting.

Three properties the QA layer will not give up: **"could not check" never counts
as a pass** (`Severity.UNKNOWN`), **one bad rule cannot take the monitoring
down** (each check runs in its own try), and **the id is the interface**
(registering a duplicate raises immediately).

### The architectural rule

Dependencies point one way and never back:

```
L4 cli/, web/   interfaces (thin, no business logic)
L3 daemon/      orchestration (the single writer)
L2 services/    business logic (unit testable)
L1 adapters/    the only place with side effects
L0 domain/      pure data and pure functions, zero I/O
```

`StateEngine` and `WavePlanner` are **completely pure functions**. That is the
most valuable investment in the project: real jobs take days, so validating
decision logic by running them would never converge.
