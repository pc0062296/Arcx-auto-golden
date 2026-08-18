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
| 3 | Rerun drain state machine + triage queue | to do |
| 4 | Policy engine and automatic remediation | to do |
| 5 | Shared-disk export and history | to do |

---

## Requirements

* Python 3.9.10 or newer
* **Zero third-party dependencies.** PyYAML is needed only to read a `.yaml`
  settings file; without it, use a `.json` settings file with the same
  structure.
* **Pure ASCII.** The whole tree contains no byte outside the ASCII range, and
  a test enforces it.

---

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

## Continuous monitoring and the web UI

```bash
# terminal 1: the daemon (the single writer)
python3 -m arcx_auto daemon --run-id nightly --wave-dir /path/to/wave_001

# terminal 2: the web UI (read only, open and close it freely)
python3 -m arcx_auto web            # then open http://127.0.0.1:8765/
```

The web UI uses only the standard library `http.server`, binds to `127.0.0.1`
by default, and has no write endpoints. Four levels of drill-down: all runs ->
index list and issue summary -> case table -> the evidence for one case.

The daemon can be stopped and restarted at any time: it resumes the previous
verdicts from `state.json`, and rebuilds from the run folders even without it.

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
| `plan --dir-map FILE --index ...` | Produce a wave plan (**never submits**) |
| `submit --dir-map X --arcx-cfg Y` | Check, create wave dirs, submit (**dry run by default**) |
| `daemon --wave-dir PATH` | Keep monitoring, writing state for the web UI |
| `web` | Serve the local read-only dashboard |
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
```

**1. Case ids are cell names with no shared pattern**, so case run dirs are
identified by exclusion; the exclusion list is `layout.non_case_dir_regexes`.

**2. Log filenames say nothing about their case.**
`submit_bjob_cmd_file_1.log` pairs by number with `cmd_folder/cmd_file_1`, and
that script's `cd <path>` names the case. The numbering matches no ordering, and
a test with `cmd_file_1 -> ZZZ_LAST` and `cmd_file_2 -> AAA_FIRST` blocks any
shortcut that guesses from the number.

A log whose case cannot be resolved lands in `unresolved_logs` and is shown
under "scan anomalies": it means a case that cannot be monitored, which must
never be dropped silently.

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
