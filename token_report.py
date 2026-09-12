"""
Token tracker for Claude Code. Two steps, no dependencies beyond the standard
library.

  collect   scan this machine's Claude Code transcripts and write one compact
            snapshot, data/<hostname>.jsonl. No conversation text leaves the
            transcripts; the snapshot holds counts, timestamps, session titles,
            and subagent descriptions.
  render    merge every snapshot in data/ (this machine and any others synced
            in) and write html/index.html, a static dashboard with inline SVG.
  all       collect, then render.

Usage: python3 token_report.py [collect|render|all] [--projects DIR]

@interacts  reads ~/.claude/projects/**.jsonl and data/*.jsonl; writes
            data/<host>.jsonl and html/index.html only
@deps       standard library only (json, datetime, pathlib, html, socket)
@complexity collect O(L), L transcript lines on the machine (about 1.5M for
            436 MB); render O(R log R), R snapshot rows (thousands)
@alloc      collect streams line by line and keeps one dict per
            (session, agent, model, day); render holds all rows in memory,
            a few MB. Nothing retained after write.
"""

from __future__ import annotations

import html
import json
import socket
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG = {
    "ProjectsDir": Path.home() / ".claude" / "projects",
    "DataDir": Path(__file__).resolve().parent / "data",
    "OutDir": Path(__file__).resolve().parent / "html",
    "SnapshotVersion": 1,
    "WeekStartsMonday": True,
    "DaysInChart": 30,
    "TopProjects": 12,
    "TopSessions": 40,
    "TopAgents": 80,
    "TitleMaxChars": 70,
    # USD per million tokens. Source: platform.claude.com/docs/en/about-claude/pricing,
    # read 2026-09-12. Matched by prefix, first match wins, so keep specific ids first.
    "Rates": [
        ("claude-fable-5-1", {"in": 10.0, "out": 50.0, "w5m": 12.5, "w1h": 20.0, "read": 0.25}),
        ("claude-mythos-5-1", {"in": 10.0, "out": 50.0, "w5m": 12.5, "w1h": 20.0, "read": 0.25}),
        ("claude-fable-5", {"in": 10.0, "out": 50.0, "w5m": 12.5, "w1h": 20.0, "read": 1.0}),
        ("claude-opus-4-1", {"in": 15.0, "out": 75.0, "w5m": 18.75, "w1h": 30.0, "read": 1.5}),
        ("claude-opus-4", {"in": 5.0, "out": 25.0, "w5m": 6.25, "w1h": 10.0, "read": 0.5}),
        ("claude-opus-5", {"in": 5.0, "out": 25.0, "w5m": 6.25, "w1h": 10.0, "read": 0.5}),
        ("claude-sonnet-5", {"in": 2.0, "out": 10.0, "w5m": 2.5, "w1h": 4.0, "read": 0.2}),
        ("claude-sonnet-4", {"in": 3.0, "out": 15.0, "w5m": 3.75, "w1h": 6.0, "read": 0.3}),
        ("claude-haiku-4-5", {"in": 1.0, "out": 5.0, "w5m": 1.25, "w1h": 2.0, "read": 0.1}),
        ("claude-haiku-3-5", {"in": 0.8, "out": 4.0, "w5m": 1.0, "w1h": 1.6, "read": 0.08}),
    ],
    # Series identity is fixed per model family and never re-assigned by rank.
    # Palette validated with the dataviz validator (light surface): all checks pass;
    # aqua and yellow need direct labels, which every chart here carries.
    "Families": [
        ("fable", "Fable", "#2a78d6"),
        ("opus", "Opus", "#eb6834"),
        ("sonnet", "Sonnet", "#1baf7a"),
        ("haiku", "Haiku", "#eda100"),
        ("other", "Other", "#8a8a8a"),
    ],
}

MILLION = 1_000_000


# ---------------------------------------------------------------- shared helpers

def family_of(model: str) -> str:
    for key, _, _ in CONFIG["Families"]:
        if key != "other" and key in (model or ""):
            return key
    return "other"


def rates_for(model: str) -> dict[str, float] | None:
    for prefix, rates in CONFIG["Rates"]:
        if (model or "").startswith(prefix):
            return rates
    return None


def cost_of(row: dict) -> float:
    rates = rates_for(row["model"])
    if not rates:
        return 0.0
    return (
        row["input"] * rates["in"]
        + row["output"] * rates["out"]
        + row["cache_w5m"] * rates["w5m"]
        + row["cache_w1h"] * rates["w1h"]
        + row["cache_read"] * rates["read"]
    ) / MILLION


def local_date(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- collect

def _first_user_text(record: dict) -> str | None:
    message = record.get("message") or {}
    if record.get("type") != "user" or record.get("isMeta"):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text")
    return None


def scan_transcript(path: Path, session_id: str, agent_id: str | None, meta: dict) -> list[dict]:
    """One transcript file to rows, one per (model, local day)."""
    buckets: dict[tuple[str, str], dict] = {}
    title = None
    first_user = None
    cwd = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"usage"' not in line and '"aiTitle"' not in line and (first_user is not None or '"user"' not in line):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("aiTitle") and not title:
                title = record["aiTitle"]
            if first_user is None:
                text = _first_user_text(record)
                if text:
                    first_user = text
            cwd = cwd or record.get("cwd")
            message = record.get("message") or {}
            usage = message.get("usage")
            if record.get("type") != "assistant" or not usage or not record.get("timestamp"):
                continue
            key = (message.get("model") or "unknown", local_date(record["timestamp"]))
            bucket = buckets.get(key)
            if bucket is None:
                bucket = buckets[key] = {
                    "calls": 0, "tool_uses": 0, "input": 0, "output": 0,
                    "cache_read": 0, "cache_w5m": 0, "cache_w1h": 0,
                    "first_ts": record["timestamp"], "last_ts": record["timestamp"],
                }
            bucket["calls"] += 1
            bucket["tool_uses"] += sum(
                1 for block in (message.get("content") or []) if isinstance(block, dict) and block.get("type") == "tool_use"
            )
            bucket["input"] += usage.get("input_tokens", 0) or 0
            bucket["output"] += usage.get("output_tokens", 0) or 0
            bucket["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
            creation = usage.get("cache_creation") or {}
            w5m = creation.get("ephemeral_5m_input_tokens")
            w1h = creation.get("ephemeral_1h_input_tokens")
            if w5m is None and w1h is None:
                w5m = usage.get("cache_creation_input_tokens", 0) or 0
                w1h = 0
            bucket["cache_w5m"] += w5m or 0
            bucket["cache_w1h"] += w1h or 0
            if record["timestamp"] < bucket["first_ts"]:
                bucket["first_ts"] = record["timestamp"]
            if record["timestamp"] > bucket["last_ts"]:
                bucket["last_ts"] = record["timestamp"]
    rows = []
    for (model, day), bucket in buckets.items():
        rows.append({
            "session_id": session_id,
            "agent_id": agent_id,
            "agent_type": meta.get("agentType"),
            "agent_desc": meta.get("description"),
            "agent_model_alias": meta.get("model"),
            "session_title": truncate(title or first_user or "(untitled)", CONFIG["TitleMaxChars"]),
            "project": Path(cwd).name if cwd else path.parent.name.split("-")[-1],
            "model": model,
            "date": day,
            **bucket,
        })
    return rows


def collect(projects_dir: Path, host: str) -> Path:
    rows: list[dict] = []
    sessions = 0
    agents = 0
    for project_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        for transcript in sorted(project_dir.glob("*.jsonl")):
            session_id = transcript.stem
            sessions += 1
            rows.extend(scan_transcript(transcript, session_id, None, {}))
            for agent_file in sorted((project_dir / session_id / "subagents").glob("agent-*.jsonl")):
                agents += 1
                meta_path = agent_file.with_suffix(".meta.json")
                meta = {}
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        meta = {}
                rows.extend(scan_transcript(agent_file, session_id, agent_file.stem.removeprefix("agent-"), meta))
    CONFIG["DataDir"].mkdir(parents=True, exist_ok=True)
    out = CONFIG["DataDir"] / f"{host}.jsonl"
    with out.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "_meta": True, "host": host, "version": CONFIG["SnapshotVersion"],
            "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sessions": sessions, "agents": agents, "rows": len(rows),
        }) + "\n")
        for row in rows:
            handle.write(json.dumps({"host": host, **row}) + "\n")
    print(f"collected {sessions} sessions, {agents} subagents, {len(rows)} rows into {out}")
    return out


# ---------------------------------------------------------------- render: data shaping

def load_snapshots() -> tuple[list[dict], list[dict]]:
    rows, metas = [], []
    for path in sorted(CONFIG["DataDir"].glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("_meta"):
                    metas.append(record)
                else:
                    record["cost"] = cost_of(record)
                    record["family"] = family_of(record["model"])
                    rows.append(record)
    return rows, metas


def week_start(today: datetime) -> datetime:
    offset = today.weekday() if CONFIG["WeekStartsMonday"] else (today.weekday() + 1) % 7
    return (today - timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------- render: formatting

def fmt_tokens(n: float) -> str:
    if n >= MILLION:
        return f"{n / MILLION:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}K"
    return f"{int(n)}"


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def fmt_duration(first_ts: str, last_ts: str) -> str:
    seconds = (datetime.fromisoformat(last_ts.replace("Z", "+00:00")) - datetime.fromisoformat(first_ts.replace("Z", "+00:00"))).total_seconds()
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def esc(text) -> str:
    return html.escape(str(text))


def nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


# ---------------------------------------------------------------- render: SVG charts

FAMILY_LABEL = {key: label for key, label, _ in CONFIG["Families"]}
FAMILY_COLOR = {key: color for key, _, color in CONFIG["Families"]}
FAMILY_ORDER = [key for key, _, _ in CONFIG["Families"]]


def legend(families: list[str]) -> str:
    items = "".join(
        f'<span class="key"><span class="swatch" style="background:{FAMILY_COLOR[f]}"></span>{esc(FAMILY_LABEL[f])}</span>'
        for f in families
    )
    return f'<div class="legend">{items}</div>'


def stacked_columns(days: list[str], per_day: dict[str, dict[str, float]], families: list[str], unit: str, fmt) -> str:
    width, height = 900, 260
    left, right, top, bottom = 56, 12, 16, 40
    plot_w, plot_h = width - left - right, height - top - bottom
    totals = {d: sum(per_day[d].values()) for d in days}
    ymax = nice_ceiling(max(totals.values()) if totals else 1)
    slot = plot_w / max(len(days), 1)
    bar_w = min(24, slot * 0.7)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(unit)} per day, stacked by model family">']
    for i in range(5):
        y = top + plot_h - plot_h * i / 4
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">{esc(fmt(ymax * i / 4))}</text>')
    for idx, day in enumerate(days):
        x = left + slot * idx + (slot - bar_w) / 2
        y_cursor = top + plot_h
        for fam in families:
            value = per_day[day].get(fam, 0.0)
            if value <= 0:
                continue
            seg_h = plot_h * value / ymax
            y_cursor -= seg_h
            draw_h = max(seg_h - 2, 0.5)
            parts.append(
                f'<rect x="{x:.1f}" y="{y_cursor + 2:.1f}" width="{bar_w:.1f}" height="{draw_h:.1f}" fill="{FAMILY_COLOR[fam]}">'
                f'<title>{esc(day)} {esc(FAMILY_LABEL[fam])}: {esc(fmt(value))}</title></rect>'
            )
        if totals[day] > 0:
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{y_cursor - 4:.1f}" class="cap" text-anchor="middle">{esc(fmt(totals[day]))}</text>')
        if idx % 3 == 0 or len(days) <= 14:
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 22}" class="tick" text-anchor="middle">{esc(day[5:])}</text>')
    parts.append("</svg>")
    return "".join(parts) + legend(families)


def horizontal_bars(items: list[tuple[str, float]], fmt) -> str:
    row_h, left, width = 28, 220, 900
    height = row_h * len(items) + 8
    xmax = nice_ceiling(max((v for _, v in items), default=1))
    plot_w = width - left - 90
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="ranked horizontal bars">']
    for i, (label, value) in enumerate(items):
        y = 4 + i * row_h
        bar = plot_w * value / xmax
        parts.append(f'<text x="{left - 10}" y="{y + 17}" class="rowlabel" text-anchor="end">{esc(truncate(label, 30))}</text>')
        parts.append(f'<rect x="{left}" y="{y + 4}" width="{bar:.1f}" height="20" rx="4" fill="#2a78d6"><title>{esc(label)}: {esc(fmt(value))}</title></rect>')
        parts.append(f'<text x="{left + bar + 8:.1f}" y="{y + 18}" class="cap">{esc(fmt(value))}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- render: page

STYLE = """
:root { --ink:#0b0b0b; --ink2:#52514e; --paper:#fbfaf7; --card:#fcfcfb; --line:#d9d4c7; --accent:#2a78d6; --code:#f1efe8; }
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px 64px; background:var(--paper); color:var(--ink); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:960px; margin:0 auto; }
h1 { font-size:28px; margin:0 0 4px; }
p.sub { color:var(--ink2); margin:0 0 22px; }
section.block { background:var(--card); border:1px solid var(--line); border-left:5px solid var(--accent); border-radius:6px; padding:18px 22px; margin:0 0 18px; }
section.block h2 { margin:0 0 12px; font-size:20px; }
section.block h3 { margin:14px 0 6px; font-size:16px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
.tile { border:1px solid var(--line); border-radius:6px; padding:12px 14px; background:#fff; }
.tile .label { font-size:12px; color:var(--ink2); text-transform:uppercase; letter-spacing:.04em; }
.tile .value { font-size:26px; font-weight:700; margin-top:2px; }
.tile .note { font-size:12px; color:var(--ink2); }
svg { width:100%; height:auto; display:block; }
.grid { stroke:#e6e2d8; stroke-width:1; }
.tick, .rowlabel, .cap { font:12px system-ui, sans-serif; fill:var(--ink2); }
.cap { fill:var(--ink); }
.legend { display:flex; flex-wrap:wrap; gap:14px; margin-top:6px; font-size:13px; color:var(--ink2); }
.swatch { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:-1px; }
.tablewrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { border:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; white-space:nowrap; }
th { background:var(--code); position:sticky; top:0; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td.wrap { white-space:normal; min-width:220px; }
.caveat { background:var(--code); border-radius:4px; padding:10px 12px; font-size:13px; }
@media (max-width:480px) { body { font-size:14px; } h1 { font-size:22px; } section.block { padding:14px; } }
"""


def tile(label: str, value: str, note: str = "") -> str:
    return f'<div class="tile"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div><div class="note">{esc(note)}</div></div>'


def table(headers: list[tuple[str, str]], rows: list[list[str]]) -> str:
    head = "".join(f'<th class="{cls}">{esc(h)}</th>' for h, cls in headers)
    body = "".join("<tr>" + "".join(f'<td class="{headers[i][1]}">{cell}</td>' for i, cell in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(rows: list[dict], metas: list[dict]) -> Path:
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    wk = week_start(now).strftime("%Y-%m-%d")
    families_present = [f for f in FAMILY_ORDER if any(r["family"] == f for r in rows)]

    # This week tiles
    week_rows = [r for r in rows if r["date"] >= wk]
    by_fam_week = defaultdict(lambda: {"output": 0, "cost": 0.0})
    for r in week_rows:
        by_fam_week[r["family"]]["output"] += r["output"]
        by_fam_week[r["family"]]["cost"] += r["cost"]
    tiles = [tile("This week, est. cost", fmt_usd(sum(r["cost"] for r in week_rows)), f"since {wk}")]
    for fam in families_present:
        stats = by_fam_week.get(fam)
        if stats:
            tiles.append(tile(f"{FAMILY_LABEL[fam]} output this week", fmt_tokens(stats["output"]), f"est. {fmt_usd(stats['cost'])}"))
    tiles.append(tile("Sessions this week", str(len({r['session_id'] for r in week_rows if not r['agent_id']}))))
    tiles.append(tile("Subagents this week", str(len({r['agent_id'] for r in week_rows if r['agent_id']}))))

    # Per day charts
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(CONFIG["DaysInChart"] - 1, -1, -1)]
    cost_day = {d: defaultdict(float) for d in days}
    out_day = {d: defaultdict(float) for d in days}
    for r in rows:
        if r["date"] in cost_day:
            cost_day[r["date"]][r["family"]] += r["cost"]
            out_day[r["date"]][r["family"]] += r["output"]

    # Projects
    proj = defaultdict(float)
    for r in rows:
        proj[r["project"]] += r["cost"]
    ranked = sorted(proj.items(), key=lambda kv: -kv[1])
    top = ranked[: CONFIG["TopProjects"]]
    rest = sum(v for _, v in ranked[CONFIG["TopProjects"]:])
    if rest > 0:
        top.append(("Other", rest))

    # Sessions table (main session rows only, aggregated over days and models)
    sess = {}
    for r in rows:
        key = (r["host"], r["session_id"])
        s = sess.setdefault(key, {"host": r["host"], "project": r["project"], "title": r["session_title"], "first": r["first_ts"], "last": r["last_ts"],
                                  "models": set(), "calls": 0, "output": 0, "cache_read": 0, "cost": 0.0, "agents": set(), "agent_cost": 0.0})
        s["models"].add(FAMILY_LABEL[r["family"]])
        s["calls"] += r["calls"]; s["output"] += r["output"]; s["cache_read"] += r["cache_read"]; s["cost"] += r["cost"]
        s["first"] = min(s["first"], r["first_ts"]); s["last"] = max(s["last"], r["last_ts"])
        if r["agent_id"]:
            s["agents"].add(r["agent_id"]); s["agent_cost"] += r["cost"]
        if not r["agent_id"] and r["session_title"] != "(untitled)":
            s["title"] = r["session_title"]
    session_rows = sorted(sess.values(), key=lambda s: -s["cost"])[: CONFIG["TopSessions"]]
    session_table = table(
        [("Date", ""), ("Host", ""), ("Project", ""), ("Session", "wrap"), ("Models", ""), ("Calls", "num"), ("Output", "num"), ("Cache read", "num"), ("Agents", "num"), ("Agent cost", "num"), ("Est. cost", "num")],
        [[esc(local_date(s["first"])), esc(s["host"]), esc(s["project"]), esc(s["title"]), esc(", ".join(sorted(s["models"]))), f"{s['calls']:,}", fmt_tokens(s["output"]), fmt_tokens(s["cache_read"]), str(len(s["agents"])), fmt_usd(s["agent_cost"]), f"<strong>{fmt_usd(s['cost'])}</strong>"] for s in session_rows],
    )

    # Agents table
    agents = {}
    for r in rows:
        if not r["agent_id"]:
            continue
        key = (r["host"], r["agent_id"])
        a = agents.setdefault(key, {"host": r["host"], "session": r["session_title"], "type": r["agent_type"] or "", "desc": r["agent_desc"] or "", "alias": r["agent_model_alias"] or "",
                                    "model": FAMILY_LABEL[r["family"]], "first": r["first_ts"], "last": r["last_ts"], "calls": 0, "tools": 0, "output": 0, "cache_read": 0, "cache_w": 0, "cost": 0.0})
        a["calls"] += r["calls"]; a["tools"] += r["tool_uses"]; a["output"] += r["output"]; a["cache_read"] += r["cache_read"]; a["cache_w"] += r["cache_w5m"] + r["cache_w1h"]; a["cost"] += r["cost"]
        a["first"] = min(a["first"], r["first_ts"]); a["last"] = max(a["last"], r["last_ts"])
    parent_title = {(s["host"], sid): s["title"] for (h, sid), s in sess.items() for _ in [0]}
    agent_rows = sorted(agents.values(), key=lambda a: -a["cost"])[: CONFIG["TopAgents"]]
    agent_table = table(
        [("Date", ""), ("Host", ""), ("Model", ""), ("Description", "wrap"), ("Type", ""), ("Calls", "num"), ("Tools", "num"), ("Output", "num"), ("Cache write", "num"), ("Cache read", "num"), ("Time", "num"), ("Est. cost", "num")],
        [[esc(local_date(a["first"])), esc(a["host"]), esc(a["model"] + (f" ({a['alias']})" if a["alias"] and a["alias"].lower() != a["model"].lower() else "")), esc(a["desc"] or "(no description)"), esc(a["type"]), f"{a['calls']:,}", f"{a['tools']:,}", fmt_tokens(a["output"]), fmt_tokens(a["cache_w"]), fmt_tokens(a["cache_read"]), fmt_duration(a["first"], a["last"]), f"<strong>{fmt_usd(a['cost'])}</strong>"] for a in agent_rows],
    )

    # By model, all time
    fam_tot = defaultdict(lambda: defaultdict(float))
    for r in rows:
        f = fam_tot[r["family"]]
        for k in ("input", "output", "cache_read", "cache_w5m", "cache_w1h", "cost", "calls"):
            f[k] += r[k]
    model_table = table(
        [("Model", ""), ("Calls", "num"), ("Input", "num"), ("Output", "num"), ("Cache write 5m", "num"), ("Cache write 1h", "num"), ("Cache read", "num"), ("Est. cost", "num"), ("Share", "num")],
        [[esc(FAMILY_LABEL[f]), f"{int(fam_tot[f]['calls']):,}", fmt_tokens(fam_tot[f]["input"]), fmt_tokens(fam_tot[f]["output"]), fmt_tokens(fam_tot[f]["cache_w5m"]), fmt_tokens(fam_tot[f]["cache_w1h"]), fmt_tokens(fam_tot[f]["cache_read"]), f"<strong>{fmt_usd(fam_tot[f]['cost'])}</strong>", f"{100 * fam_tot[f]['cost'] / max(sum(x['cost'] for x in fam_tot.values()), 1e-9):.0f}%"] for f in families_present],
    )

    machines = table(
        [("Host", ""), ("Collected", ""), ("Sessions", "num"), ("Subagents", "num"), ("Rows", "num"), ("First day", ""), ("Last day", "")],
        [[esc(m["host"]), esc(m["collected_at"][:16].replace("T", " ")), str(m["sessions"]), str(m["agents"]), str(m["rows"]), esc(min((r["date"] for r in rows if r["host"] == m["host"]), default="")), esc(max((r["date"] for r in rows if r["host"] == m["host"]), default=""))] for m in metas],
    )
    rate_table = table(
        [("Model prefix", ""), ("Input", "num"), ("Output", "num"), ("Cache write 5m", "num"), ("Cache write 1h", "num"), ("Cache read", "num")],
        [[esc(p), fmt_usd(r["in"]), fmt_usd(r["out"]), fmt_usd(r["w5m"]), fmt_usd(r["w1h"]), fmt_usd(r["read"])] for p, r in CONFIG["Rates"]],
    )

    total_cost = sum(r["cost"] for r in rows)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Token Tracker</title><style>{STYLE}</style></head><body><main>
<h1>Claude Token Tracker</h1>
<p class="sub">Rendered {esc(now.strftime('%Y-%m-%d %H:%M'))} from {len(metas)} machine snapshot(s). All-time est. cost at list price: <strong>{esc(fmt_usd(total_cost))}</strong>.</p>

<section class="block"><h2>This week</h2><div class="tiles">{''.join(tiles)}</div></section>

<section class="block"><h2>Est. cost per day, last {CONFIG['DaysInChart']} days</h2>{stacked_columns(days, cost_day, families_present, 'USD', fmt_usd)}</section>

<section class="block"><h2>Output tokens per day, last {CONFIG['DaysInChart']} days</h2>{stacked_columns(days, out_day, families_present, 'output tokens', fmt_tokens)}</section>

<section class="block"><h2>Est. cost by project, all time</h2>{horizontal_bars(top, fmt_usd)}</section>

<section class="block"><h2>By model, all time</h2>{model_table}</section>

<section class="block"><h2>Sessions, top {CONFIG['TopSessions']} by est. cost</h2><p class="sub">Session cost includes its subagents. The Agent cost column is the part spent by subagents.</p>{session_table}</section>

<section class="block"><h2>Subagents, top {CONFIG['TopAgents']} by est. cost</h2>{agent_table}</section>

<section class="block"><h2>Machines</h2>{machines}</section>

<section class="block"><h2>How the estimate works</h2>
<div class="caveat">These are list-price figures: tokens times the public per-million rates below, including the cheap cache-read rate that dominates long sessions. You are on a subscription with usage limits, not per-token billing, so this is what the work <em>would</em> cost at list price. Use it to compare runs and models, not as a bill. Rates were read from the public pricing page on 2026-09-12 and live in the script's CONFIG.</div>
<h3>Rates, USD per million tokens</h3>{rate_table}
</section>
</main></body></html>"""
    CONFIG["OutDir"].mkdir(parents=True, exist_ok=True)
    out = CONFIG["OutDir"] / "index.html"
    out.write_text(page, encoding="utf-8")
    print(f"rendered {out} from {len(rows)} rows, {len(metas)} snapshot(s)")
    return out


# ---------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "all"
    projects = CONFIG["ProjectsDir"]
    if "--projects" in argv:
        projects = Path(argv[argv.index("--projects") + 1]).expanduser()
    host = socket.gethostname()
    if command in ("collect", "all"):
        collect(projects, host)
    if command in ("render", "all"):
        rows, metas = load_snapshots()
        if not rows:
            print("no snapshots in data/; run collect first", file=sys.stderr)
            return 1
        render(rows, metas)
    if command not in ("collect", "render", "all"):
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
