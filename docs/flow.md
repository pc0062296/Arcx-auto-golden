# How the whole thing works

Three diagrams: the processes and who writes what, one submission end to end,
and the monitoring loop. Everything is plain text so it reads the same in a
terminal, a browser, and a printout.

---

## 1. The processes

```
  YOU                        arcx-auto start          starts both of these
   |                                 |
   |                        +--------+---------+
   |                        |                  |
   v                        v                  v
 Chrome  <--- HTTP --->  web server         daemon
                            |                  |
                     reads state.json    THE ONLY WRITER
                     writes intents      scans, submits, reruns, publishes
                            |                  |
                            +---> commands/ ---+
                                  (a directory
                                   of JSON files)
```

**The web server never acts.** Every button writes a small JSON file saying
what somebody asked for, and returns. The daemon picks it up and does the work.

Two reasons, and the second one is the plain one:

- Two writers on one run folder is the failure this whole system exists to
  avoid, and "the UI only writes when the daemon is not looking" is not an
  invariant anybody can keep.
- The submission gate can wait **hours** for the LSF quota to drop. A browser
  request cannot.

The daemon looks at the queue every **2 seconds**, separately from its scan.
Scanning is expensive -- every run folder over NFS -- so it is paced at 30s
while cases run and 5 minutes when idle. Checking the queue is one `listdir` on
a local directory, so it costs nothing and happens constantly. **A button press
is acted on in about two seconds, not at the next scan.**

### Who owns what

```
~/.arcx-auto/                 state; only the daemon writes here
  runs/<run_id>/
    state.json                what the UI reads
    events.jsonl              state changes
    audit.jsonl               every write action, and why
    policy.jsonl              what automatic handling decided
    daemon.lock               the pid, so `arcx-auto stop` can find it
  drafts/<id>.json            a selection being built (the UI writes these)
  commands/{pending,running,done}/

<run_root>/<run_id>/wave_001/     what Arcx actually runs in
  arcx.cfg  dir_map               snapshots, taken at submit time
  .arcx_auto/
    manifest.json  launch.json  lock  special_cfg/  attempts/
  <index>_run/                    Arcx creates these

/tmp1/.auto_golden/<user>/    derived; delete it and it rebuilds
```

The run folders are the truth. `state.json` is a cache -- delete it and the
next scan rebuilds it from the filesystem.

---

## 2. One submission, end to end

```
  YOU                        WEB                     DAEMON              LSF
   |                          |                        |                  |
   |-- new submission ------->|                        |                  |
   |                     create draft                  |                  |
   |<-- draft page -----------|                        |                  |
   |                          |                        |                  |
   |-- choose files --------->|                        |                  |
   |                     list a directory              |                  |
   |<-- click a dir_map ------|                        |                  |
   |<-- click an arcx.cfg ----|                        |                  |
   |                          |                        |                  |
   |<-- index list -----------|  read dir_map,         |                  |
   |    GDS, cpu, slots       |  size each index       |                  |
   |                          |                        |                  |
   |-- tick, add group ------>|                        |                  |
   |     (repeat for another cfg if you need to)       |                  |
   |                          |                        |                  |
   |-- run the checks ------->|                        |                  |
   |                     plan waves + PRE checks       |                  |
   |                     ** creates nothing **         |                  |
   |<-- result ---------------|                        |                  |
   |                          |                        |                  |
   |   FATAL?  -> no submit button exists.             |                  |
   |   clear?  -> the button is there.                 |                  |
   |                          |                        |                  |
   |-- submit --------------->|                        |                  |
   |                     write commands/pending/x.json |                  |
   |<-- "queued" (instant) ---|                        |                  |
   |                          |                        |                  |
   |                          |   claim (os.replace)   |                  |
   |                          |<-----------------------|  ~2s later       |
   |                          |                        |                  |
   |                          |             validate every path           |
   |                          |             build wave dirs + snapshots   |
   |                          |                        |                  |
   |                          |             +--- gate: NJOBS low, or      |
   |                          |             |    max_wait elapsed?        |
   |                          |             |      no -> wait             |
   |                          |             |      yes -> release         |
   |                          |             v                             |
   |                          |             bsub wave_001 --------------->|
   |                          |             (one wave at a time)          |
   |                          |                        |                  |
   |                          |   move to commands/done/, with the result |
   |                          |                        |                  |
   |<-- the run appears on / --------------------------|  discovered by   |
   |                                                      scanning        |
```

**Nothing is created until the button.** The checks page plans and validates
and writes nothing, so it can be run as many times as you like.

**A FATAL check removes the button rather than refusing the press.** A button
you are allowed to press and then told off for is worse than no button.

**The checks page runs the same preflight the daemon will**, LSF adapter
included -- otherwise it could say "all clear" and then have the submission
refused for an unreachable `bsub`.

### Groups and waves

```
  what you selected                      what goes out

  group_1: dir_map A + cfg A             wave_001  <- group_1, 12 slots
           indices 1000, 1002            wave_002  <- group_1, 16 slots
                                         wave_003  <- group_2, 20 slots
  group_2: dir_map B + cfg B
           indices 2000                     released one at a time,
                                            by the gate
```

A **group** is what a person said belongs together. A **wave** is what may go
out at once. Ticking fifty indices does not send fifty: the slot cap
(`GDS count x O_QCAP_LSF_NUM`) splits each group, because the person ticking
boxes has not thought about the queue.

Each wave carries **its own** dir_map and cfg, since a wave is one Arcx command
against one cfg, and they are snapshotted into the wave directory so QA three
days later reads what actually ran.

---

## 3. The monitoring loop

Every tick, for every wave under `run_root`:

```
   scan the filesystem            one scandir per index run folder
        |                         markers, logs, cmd_files
        v
   build the case roster          from markers + cmd_folder/cmd_file_N
        |                         NEVER from the directory listing
        v
   attach LSF jobs                one bjobs for the whole account
        |
        v
   StateEngine  (pure)            markers + LSF -> base state
        |                         QUEUED RUNNING STALLED LOST SUSPENDED
        v                         COMPLETED_MARKER
   QA checks
        |  LIVE   every tick, cheap: is it healthy right now?
        |  POST   once per case after .complete: did it really succeed?
        |  INDEX POST  only once EVERY case has finished, because that
        |              is when Arcx writes the reports
        v
   StateResolver (pure)           base state + issues -> final state
        |                         COMPLETED_MARKER + clean   -> DONE
        |                         COMPLETED_MARKER + problem -> FAILED
        |                         RUNNING + quiet too long   -> STALLED
        v
   write state.json  ->  the UI reads it
   write events.jsonl / audit.jsonl
   policy engine (shadow)  ->  policy.jsonl
   publish to the shared disk (every 60s)
```

### Why a case is DONE

`DONE` is not "Arcx wrote `.complete`". That is `COMPLETED_MARKER`, and it is
only the starting point:

```
  .complete marker present
        |
        +-- netlists exist?                     no -> FAILED
        +-- big enough?                         no -> FAILED
        +-- first line has the engine banner?   no -> FAILED   (truncated)
        +-- QC_* summary values are numbers?    no -> FAILED   (no result)
        +-- could not check something?               -> FAILED (UNKNOWN)
        |
        v
      DONE
```

A run that finished and produced a truncated netlist reads `FAILED`. That is
the whole point: it is the failure that otherwise ships as a success.

### Investigating a failure

```
   the case page                      what it answers without leaving it

   state + reason                     what the verdict is
   QA issues, worst first             why -- with the evidence
     every path in the evidence   ->  /view (the file itself)
   the end of the log                 what it was doing when it stopped
   the files it produced              or that it produced none at all
```

`/view` is the only read endpoint that takes a path from the request, so it
takes it under a confinement check: the path is resolved with `realpath` and
must land inside `run_root` or inside a wave directory the daemon is actually
monitoring. `realpath` comes first because anyone who can write into a run
folder can drop a symlink in it, and a check on the literal path would see
something under the root and say yes. No roots configured denies everything.

Nothing is ever read whole. A tail seeks to the end and reads backwards in
blocks, so a 400MB netlist costs the same as a log.

### Deciding about a failure

```
   a case reads FAILED
        |
        v
   policy engine, in shadow mode
        |  decides what it WOULD do, writes it to policy.jsonl
        |  acts on nothing
        v
   `arcx-auto policy --review <run_id>`  <- the evidence for whether
        |                                   automation would have been right
        v
   YOU press "rerun this wave..."
        |
        v
   confirmation page: exactly which run dirs move aside, and why
        |
        v
   queued -> daemon:
        bkill the parent Arcx job
        bjobs_manage.py -djp   (up to 3 times; deletion lags)
        SAFETY GATE: 3 consecutive readings of zero jobs,
                     with the marker set unchanged
                     -- anything else ABORTS, nothing is touched
        move the failed run dirs into .arcx_auto/attempts/N/   (moved, not deleted)
        bsub with -keep_dir    (finished cases are not redone)
```

**Every rule ships as `escalate`.** Nothing reruns by itself. Turning any of it
on is a decision to make against the shadow log, after it has watched real
failures.

---

## The four rules everything else follows from

**The filesystem is the truth.** `state.json`, the shared-disk pages and the
export are all caches. Delete any of them and the next scan rebuilds it.

**One writer.** The daemon. The UI asks; the CLI asks; neither writes into a
run folder. A rerun additionally takes a lock on the wave directory, so two of
them cannot both move the same directories aside.

**"Could not check" is never a pass.** `UNKNOWN` blocks success exactly like
`FATAL` does. The one deliberate exception is the rerun delete list, where
unsure means delete -- wasting a run is recoverable and shipping a truncated
result is not.

**Uncertain stops and asks.** The drain gate aborts rather than guessing. The
policy engine escalates rather than acting. A missing job count is unknown,
never zero.
