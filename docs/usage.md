# How to use it

For the engineer running the extraction. Nothing here needs the command line
after the first setup.

---

## Once, per person

Put the launcher on your PATH:

```bash
ln -s /path/to/arcx-auto-golden/bin/arcx-auto ~/bin/arcx-auto
arcx-auto --version
```

Then tell it where results should go:

```bash
mkdir -p ~/.arcx-auto/config
cp /path/to/arcx-auto-golden/config/default.yaml ~/.arcx-auto/config/
```

The one line worth looking at is where runs are kept:

```yaml
run_root: "./arcx_runs"
```

`./arcx_runs` means *beside the work you are doing* -- one workspace per
project directory, which is the normal way to use this. An absolute path means
one shared place wherever you run from. Everything else has a working default.

`state_root` is different and must stay one fixed place: the command queue and
the daemon locks live there, and a request queued in one directory has to be
visible to a daemon started in another.

---

## Every day: one command

```bash
arcx-auto start
```

That starts the monitor and the UI, and opens Chrome at
**http://127.0.0.1:8765/**. Leave the terminal open; **Ctrl-C stops both**.

If it is busy at the submission gate the first Ctrl-C says so and stops after
the current step; press it again to stop immediately. From another terminal:

```bash
arcx-auto stop
```

Neither process is needed for Arcx itself to keep going -- jobs already sent to
LSF carry on regardless. They are how you see it and steer it, and a run with
nothing watching it says so on its page.

Running them apart still works, if you want the monitor on one machine and the
UI on another:

```bash
arcx-auto daemon
arcx-auto web
```

---

## Working in more than one directory

A **workspace** is a `run_root` -- with the default `./arcx_runs`, it is the
directory you started the daemon in. Start one daemon per directory you are
working in:

```bash
cd /proj/chipA   &&  arcx-auto start     # daemon + UI + browser
cd /proj/chipB   &&  arcx-auto daemon    # just a daemon
cd /proj/chipC   &&  arcx-auto daemon
```

**One browser is enough.** The UI is not tied to the directory it was started
in: every daemon registers the run_root it owns, the front page lists them, and
a submission picks one.

```
workspaces
run_root                     daemon      run          started in
/proj/chipA/arcx_runs        watching    chipA-4f21   /proj/chipA
/proj/chipB/arcx_runs        watching    chipB-9ac0   /proj/chipB
/proj/chipC/arcx_runs        stopped     chipC-1de8   /proj/chipC
```

On **new submission** the page says which workspace the waves go into, with a
**use this** next to each of the others. With one daemon running there is
nothing to choose and it is a single line. The file picker opens in that
workspace's directory, so your `dir_map` and `arcx.cfg` are usually one click
away.

A daemon started in the same directory again is the **same** workspace: it
picks the run page back up rather than starting a new one. Restarting is free.

If you submit to a workspace whose daemon has stopped, the checks page says so
before you press the button -- the request is queued and waits, it is not lost.

---

## 1. Prepare your files

Nothing new: your `dir_map` and your `arcx.cfg`, wherever you keep them. The
tool reads them, it never edits them.

## 2. Pick what to run

### The quick way: auto select group

Press **new submission**, then **auto select group...**, and click to the
directory that holds your `dir_map`. Everything else is read from that
directory:

```
directory: /proj/chipA/run3      naming: chipA
cfg files: chipA_typical.cfg, chipA_Cbest_T.cfg, chipA_Cworst_T.cfg

    index   corner     cfg                  why
[x] 1000    Cbest_T    chipA_Cbest_T.cfg    corner Cbest_T -> chipA_Cbest_T.cfg
[x] 1001    cbt        chipA_Cbest_T.cfg    corner cbt -> chipA_Cbest_T.cfg
[x] 1002    -          chipA_typical.cfg    no corner -> chipA_typical.cfg
[ ] 1003    Whot_T     -- skip --           no cfg for this corner (Whot_T)
[ ] 1004    -          -- skip --           path matches an excluded pattern (*bak)
[ ] 1005    -          -- skip --           disable flag in the index directory
```

The rules:

- **The corner is the path component after `corner_v2g`** -- not necessarily
  the last one, so an index further down still finds its corner.
- **It goes to the cfg for that corner**, matched by name rather than
  constructed: `Cbest_T` in a path and `cbt` in a file name are the same corner
  if you say so in `auto_group.corner_aliases`.
- **No corner goes to `<naming>_typical.cfg`**, which is also where the naming
  prefix is read from.
- **A corner with no cfg is left out**, never swept into typical.
- **An index directory containing `disable_qcap_golden` is left out**, and so
  is everything under a directory containing it -- drop one file at the top of
  a tree and the whole tree is out. The row says which directory did it. That
  file is read, never written: opting out belongs to whoever owns the data.
- **Backup-looking paths are left out**: anything under a directory matching
  `*_old`, `*bak`, `*backup`, `*back`. The list is
  `auto_group.exclude_path_globs`.

**Every index has a row, including the ones left out, with the reason.**
Nothing is hidden, and every row can be overridden -- tick one it skipped, or
move one to a different cfg with the dropdown. **add these groups** then makes
one group per cfg, and you carry on from step 4 exactly as usual.

Set the corner aliases once, in `~/.arcx-auto/config/default.yaml`:

```yaml
auto_group:
  corner_aliases:
    Cbest_T: [cbest_t, cbt, c_best_t]
    Cworst_T: [cworst_t, cwt]
```

### The manual way

Click **new submission**, then **choose a dir_map and an arcx.cfg...** and
click your way to them. The files that look like a `dir_map` or a cfg in the
directory you are in are offered as buttons at the top, so it is usually one
click. Picking one asks for the other; the picker reopens where you left it.

You get a row per index, with what it will cost:

```
    index   GDS   cpu/case   slots   path
[ ] 1000     3        4        12    /proj/.../index1000
[ ] 1001     2        4         8    /proj/.../index1001
```

`slots = GDS count x O_QCAP_LSF_NUM`, which is what the batching uses. An index
whose `special.cfg` cannot be read is still selectable -- it is sized with the
default of 4 CPU per case, and the checks in step 4 say so. Only an index with
no GDS at all cannot run.

## 3. Tick them, and add more groups if you need to

Tick the indices, name the group, press **add this group**.

A **group** is one `dir_map` + one `arcx.cfg` + the indices you ticked. If some
indices need a *different* cfg, add a second group with that cfg. Repeat as
many times as you like.

You do not batch by hand. Ticking fifty indices does not send fifty at once --
the slot cap splits each group into waves automatically, and the next page shows
you exactly how.

**Waves are cut between directories, not through them.** Indices sharing a
parent directory go out in the same wave, because that structure is already how
the work is classified -- a batch that scatters it is a batch somebody has to
reassemble to debug. The keyword priority (`sram`, `ro`) moves whole folders for
the same reason.

A folder bigger than the slot cap is **not** split: the wave goes over the cap
rather than lose the grouping, and the checks page says so
(`PREFLIGHT_WAVE_OVERSIZED`) before you submit. If that is not what you want for
a particular selection, the group table has a **folders** column -- click it to
switch that group to **split anywhere**.

Your selection is saved as you go. Closing the tab loses nothing.

## 4. Check, then submit

Press **run the pre-submission checks**. This creates nothing and submits
nothing. You get:

- the waves it would produce, with cases and slots for each
- every check, with its severity

```
waves 2   cases 7   slots 28   fatal 0
```

**If anything is FATAL there is no submit button.** Fix it and check again.
Warnings do not block, but read them.

If it is clear, press **submit N wave(s)**. The page comes straight back --
your request is queued, and the daemon picks it up **within about two seconds**.
It returns rather than waiting because the submission gate can hold a wave for
hours until the LSF quota drops, which is not something a browser should sit
through; the queueing is not a delay, it is what lets you carry on.

Changed your mind before the daemon picks it up? **queue** (top right) has a
**cancel** next to anything still waiting.

### Where it lands on disk

One directory per source folder, inside the wave:

```
<run_root>/<run_id>/wave_001/
    chipA_typical.cfg   dir_map          the wave's snapshot
    Cbest_T_blockA/                      one per source folder
        chipA_typical.cfg   dir_map      its own copies
        1000_run/  1001_run/             Arcx creates these
    Cworst_T_blockA/
        chipA_typical.cfg   dir_map
        1002_run/
```

The name carries **two** levels of the source path, because `blockA` exists
under every corner and one directory holding two different `blockA`s would be
exactly the mixing this avoids.

Arcx is started once per directory -- it creates its run folders relative to
where it was started, so this is what keeps folders apart on disk. They all go
out together when the gate releases the wave; the checks page tells you how
many Arcx runs a submission actually is.

## 5. Watch it

Go to **/** (the arcx-auto link, top left). The run appears there on its own --
you do not have to tell the daemon about it.

The front page names **every case needing a person, across every run**, before
any of the run tables. If it says "nothing needs a person right now", that is
the whole answer.

The page refreshes itself every 30 seconds.

A run page groups its indices the way the submission was built:

```
v chipA_typical.cfg        12 index, 240 case(s)   [3 need a person]
    v corner_v2g/Cbest_T/blockA    4 index, 80 case(s)  [3 need a person]
          <the index table>
    > corner_v2g/Cbest_T/blockB    4 index, 80 case(s)
> chipA_cworst.cfg          8 index, 160 case(s)
```

Click to open and close. Anything with a case needing a person is **already
open**; the rest stays shut, because the point is to stop having to read
everything.

## 6. Read the states

| State | Meaning |
|---|---|
| `QUEUED` / `RUNNING` | normal |
| `DONE` | finished **and** every QA check passed |
| `FAILED` | finished, and QA found something wrong -- or could not confirm it was right |
| `STALLED` | the log has not grown for a long time |
| `LOST` | a job we **had seen** for this case has been gone from `bjobs` for half an hour, and the marker still says running |
| `SUSPENDED` | LSF suspended it |

Click an index for the case table, and a case for the evidence behind its
verdict.

The **LSF** column says one of three things, and they mean different things:

| Shown | Meaning |
|---|---|
| `RUN`, `PEND`, ... | a job is matched to this case right now |
| `gone` | a job we had seen is no longer listed by `bjobs`. After `lost_grace_sec` this becomes `LOST` |
| `not matched` | no job has ever been found for this case. Usually it is waiting its turn inside Arcx, which runs only so many cases at a time in one index |

**`not matched` is never `LOST`.** Never having found a job is not evidence
that one is gone.

The one thing that *is* worth reporting about waiting cases is a queue that
has stopped moving: `INDEX_QUEUE_NOT_MOVING` fires when cases are queued and
nothing in that index has been running for an hour
(`qa.queue.idle_after_sec`). Being queued for six hours is a fact about the
size of the index; six hours with nothing running is a fact about the run.

On a big index, filter the table with the buttons above it:

```
[ all 312 ]  [ needs a person 4 ]  [ FAILED 3 ]  [ STALLED 1 ]  [ DONE 308 ]
```

**needs a person** is the one to press. They are plain links, so a filtered
table is a URL you can paste to a colleague, and the page keeps refreshing
into the same filter.

A case page shows **the end of its log on the page** -- no terminal, no `cd`,
no `tail`. Above it are **last 500** and **first 200** for the full viewer,
where you can also read the netlists and every other file the case produced.
The files themselves are listed at the bottom of the case page; if that list
says *no file at all*, that is your answer already.

Every path a QA issue names is a link. `NETLIST_NO_SIGNATURE` says the first
line is wrong -- click the path and read the first line.

The viewer only opens files inside your run directories. It is a window, not
a download: it reads the end (or the start) of a file, never all of it, so
opening a 400MB netlist costs the same as opening a log.

`DONE` means more than "Arcx wrote `.complete`". It means the netlists exist,
are the right size, carry the extraction engine's own banner, and the `QC_*`
summary values are real numbers. A run that finished but produced a truncated
netlist reads `FAILED`, not `DONE`.

## 7. Decide about the failures

On an index with anything needing attention there is a **rerun this folder...**
link. Its scope is the directory Arcx ran in -- one source folder, not the
whole wave -- so rerunning one leaves the rest alone. It shows you, before any
button:

- which case run dirs would be moved aside, and why
- which would be kept
- exactly what will happen, in order

Nothing is deleted -- the directories are **moved** into
`.arcx_auto/attempts/N/`, so the failed state survives for you to look at.

Press it and the request is queued like a submission. The daemon stops the
parent job, drains the LSF jobs, waits until the wave is genuinely quiet, moves
the failed cases aside, and resubmits with `-keep_dir` so the finished cases are
not redone.

**Nothing reruns by itself.** The tool decides what it *would* do and records
it, but every rerun is a button somebody pressed.

## 8. Know when it is done

The run page says so itself once nothing is in flight:

> **finished -- all 12 case(s) passed.** Nothing here needs a person. The
> results are ready to use.

or, if it finished badly:

> **finished, with 2 of 12 case(s) unresolved.** Nothing is running any more,
> so these will not improve on their own.

There is no separate sign-off. The first banner is the green light.

---

## Other people can watch without running anything

Your daemon publishes a page to the shared disk every minute:

```
/tmp1/.auto_golden/index.html        everyone
/tmp1/.auto_golden/<user>/status.html    you
```

They open the file. No daemon, no account, nothing installed. It shows problems
first: every case needing a person, across every run.

---

## When something looks wrong

| Symptom | Look at |
|---|---|
| a button did nothing | **queue** (top right). If requests sit in "waiting", no daemon is running |
| the page looks frozen | the run page says "the daemon has stopped" when nothing is watching |
| Ctrl-C seems ignored | it is at the submission gate. Press it again, or `arcx-auto stop` |
| the page is stale | the daemon terminal -- it prints errors and carries on |
| an index cannot be selected | it has no GDS files; the reason is on the row |
| a file will not open | it is outside `run_root` and the run directories; the viewer only reads inside them |
| everything says `LOST` | LSF is unreachable. Nothing is actually wrong with the jobs |
| a case says **not matched** under LSF | no job has been found for it yet. Normal while Arcx is holding it back -- it runs only so many cases at a time within one index |
| `INDEX_QUEUE_NOT_MOVING` | cases are waiting and **nothing in that index is running**. Nothing is going to start them -- check whether the Arcx job for that directory is still alive |
| a submission went to the wrong directory | the **workspace** line on the submission page; it names the run_root before you submit |
| a wave is bigger than the slot cap | a folder is kept whole. Raise `plan.max_slots_per_wave`, select fewer indices, or set that group to **split anywhere** |
| an index says `special.cfg` cannot be read | it is still selectable and sized at `plan.default_cpu_per_case`. If your config file predates that, check it says 4, not 0 |

To check a cfg without submitting anything:

```bash
arcx-auto check-cfg /path/to/arcx.cfg
```

To see what automatic handling *would* have done, once it has been watching for
a while:

```bash
arcx-auto policy --review <run_id>
```

---

For how the pieces fit together -- what writes what, and why the UI never acts
-- see **[flow.md](flow.md)**.
