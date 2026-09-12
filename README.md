# Claude Token Tracker

One script, no dependencies. It reads the transcripts Claude Code already
writes on this machine, boils them down to counts, and renders a dashboard:
what ran, on which model, in which project, what each subagent was for, and
what it all would cost at list price.

## Run it

```
python3 token_report.py            # collect this machine, then render
python3 token_report.py collect    # only refresh data/<hostname>.jsonl
python3 token_report.py render     # only rebuild html/index.html
```

Open `html/index.html` in a browser. Nothing else to install.

## More than one machine

Each machine writes its own snapshot, `data/<hostname>.jsonl`. The dashboard
merges every snapshot it finds in `data/`, so the only job is getting the
snapshots into one folder.

Snapshots hold counts, timestamps, model ids, session titles, and the
one-line description each subagent was spawned with. They do not hold your
conversations. A snapshot for a year of heavy use is a few megabytes.

Two ways to sync, pick one:

**Git (recommended).** Push this repo to a private remote. On each machine:

```
git pull
python3 token_report.py collect
git add data/
git commit -m "Snapshot from $(hostname)"
git push
python3 token_report.py render
```

Each machine only ever rewrites its own file, so there are no merge conflicts.

**A synced folder.** Point `CONFIG["DataDir"]` in the script at a folder
OneDrive or Dropbox already syncs, and run `collect` on each machine. Render
anywhere.

Work machine note: if the work machine must not push to a personal remote,
run `collect` there and carry the one file across however policy allows. The
file has no code and no conversation text in it.

## What the numbers mean

Every Claude API call reports four token counts. The dashboard keeps them
separate because they cost very different amounts:

| Count | What it is | Rough price vs input |
|---|---|---|
| Input | Fresh tokens the model read for the first time | 1x |
| Output | Tokens the model wrote, including its thinking | 5x |
| Cache write | Tokens stored for reuse in later calls | 1.25x (5 min) or 2x (1 hour) |
| Cache read | Tokens re-read from cache | 0.1x, or 0.025x on Fable 5.1 |

Long sessions are almost all cache reads. That is why a session can show
tens of millions of tokens and a modest dollar figure.

**The dollar figures are estimates at public list price.** A subscription
with usage limits does not bill per token. The estimate is for comparing
runs and models with each other. Rates live in `CONFIG["Rates"]` with the
date they were read; update them when the pricing page changes.

## Where the data comes from

- Main sessions: `~/.claude/projects/<project>/<session-id>.jsonl`
- Subagents: `~/.claude/projects/<project>/<session-id>/subagents/agent-*.jsonl`,
  plus a `.meta.json` next to each with the agent type, model, and description.

If Claude Code moves these, change `CONFIG["ProjectsDir"]` or pass
`--projects <dir>`.

## Colorblind check

Model identity is carried by a fixed color per family, a legend with text
labels, a fixed stacking order (Fable always at the bottom), hover titles on
every bar segment, and a table under every chart. Nothing depends on color
alone. The palette passes the dataviz validator on the light surface; the
two lighter hues (aqua, yellow) are always paired with direct labels.
