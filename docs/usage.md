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

Edit that file and set **one** line -- the disk you want the runs on:

```yaml
run_root: "/proj/rc_golden/runs"
```

Everything else has a working default.

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

## 1. Prepare your files

Nothing new: your `dir_map` and your `arcx.cfg`, wherever you keep them. The
tool reads them, it never edits them.

## 2. Pick what to run

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
your request is queued, and the daemon does the work. That is deliberate: the
submission gate can wait hours for the LSF quota to drop, which is not
something a browser should sit through.

Changed your mind before the daemon picks it up? **queue** (top right) has a
**cancel** next to anything still waiting.

## 5. Watch it

Go to **/** (the arcx-auto link, top left). The run appears there on its own --
you do not have to tell the daemon about it.

The front page names **every case needing a person, across every run**, before
any of the run tables. If it says "nothing needs a person right now", that is
the whole answer.

The page refreshes itself every 30 seconds.

## 6. Read the states

| State | Meaning |
|---|---|
| `QUEUED` / `RUNNING` | normal |
| `DONE` | finished **and** every QA check passed |
| `FAILED` | finished, and QA found something wrong -- or could not confirm it was right |
| `STALLED` | the log has not grown for a long time |
| `LOST` | the marker says it is running, but LSF has no such job |
| `SUSPENDED` | LSF suspended it |

Click an index for the case table, and a case for the evidence behind its
verdict.

`DONE` means more than "Arcx wrote `.complete`". It means the netlists exist,
are the right size, carry the extraction engine's own banner, and the `QC_*`
summary values are real numbers. A run that finished but produced a truncated
netlist reads `FAILED`, not `DONE`.

## 7. Decide about the failures

On an index with anything needing attention there is a **rerun this wave...**
link. It shows you, before any button:

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
| everything says `LOST` | LSF is unreachable. Nothing is actually wrong with the jobs |

To check a cfg without submitting anything:

```bash
arcx-auto check-cfg /path/to/arcx.cfg
```

To see what automatic handling *would* have done, once it has been watching for
a while:

```bash
arcx-auto policy --review <run_id>
```
