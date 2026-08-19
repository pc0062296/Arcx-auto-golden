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

## Every day: start the two processes

```bash
arcx-auto daemon      # terminal 1 -- leave it running
arcx-auto web         # terminal 2 -- leave it running
```

Then open **http://127.0.0.1:8765/** in Chrome.

| | What it does | If you close it |
|---|---|---|
| `daemon` | watches the runs, and does whatever you ask for in the browser | jobs keep running; nothing is monitored, and buttons queue up unanswered |
| `web` | serves the pages | the daemon carries on; you just cannot see it |

Neither one is needed for Arcx itself to keep going. They are how you see it and
steer it.

---

## 1. Prepare your files

Nothing new: your `dir_map` and your `arcx.cfg`, wherever you keep them. The
tool reads them, it never edits them.

## 2. Pick what to run

Click **new submission**, then type the path to a `dir_map` and an `arcx.cfg`
and press **read the dir_map**.

You get a row per index, with what it will cost:

```
    index   GDS   cpu/case   slots   path
[ ] 1000     3        4        12    /proj/.../index1000
[ ] 1001     2        4         8    /proj/.../index1001
```

`slots = GDS count x O_QCAP_LSF_NUM`, which is what the batching uses. An index
whose `special.cfg` cannot be read is greyed out with the reason.

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

## 5. Watch it

Go to **/** (the arcx-auto link, top left). The run appears there on its own --
you do not have to tell the daemon about it.

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

A run is finished when every case reads `DONE`. There is no separate sign-off
-- that is your judgement, and the case table is what you judge from.

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
| the page is stale | the daemon terminal -- it prints errors and carries on |
| an index is greyed out when selecting | its `special.cfg` could not be read; the reason is on the row |
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
