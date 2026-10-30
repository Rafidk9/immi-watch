#!/usr/bin/env python3
"""Build the immi-watch status page (_site/index.html) for GitHub Pages.

Reads sources.yaml, both runners' state files and the change records, and writes one
self-contained HTML page: runner health, recent changes, and every source's last check.

Usage:
  python status_page.py             # writes _site/index.html
  python status_page.py --out DIR
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Australia/Melbourne")
REPO = os.environ.get("GITHUB_REPOSITORY", "Rafidk9/immi-watch")
RECENT_DAYS = 30
MAX_LISTED = 25              # recent changes shown on the page
FAIL_ALERT_AT = 3            # same threshold as monitor.py
LOCAL_STALE_HOURS = 24
LATE_GRACE_HOURS = 3         # a source is "late" when unchecked for every + this many hours
SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def load_json(name: str) -> dict:
    path = ROOT / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def parse_time(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def load_changes(names: dict[str, str], since: dt.datetime) -> list[dict]:
    """Change records are changes/YYYY-MM/YYYYMMDD-HHMM_<id>.md; severity is in the header line."""
    out = []
    for path in (ROOT / "changes").glob("*/*.md"):
        m = re.match(r"(\d{8}-\d{4})_(.+)\.md$", path.name)
        if not m:
            continue
        when = dt.datetime.strptime(m[1], "%Y%m%d-%H%M").replace(tzinfo=TZ)
        if when < since:
            continue
        head = path.read_text(encoding="utf-8")[:600]
        sev = re.search(r"severity: \*\*(\w+)\*\*", head)
        added = re.search(r"\*\*Added:\*\*\n- (.+)", head)
        out.append({
            "when": when, "id": m[2], "name": names.get(m[2], m[2]),
            "severity": sev[1] if sev else "medium",
            "preview": (added[1] if added else "").strip()[:160],
            "url": f"https://github.com/{REPO}/blob/main/{path.relative_to(ROOT).as_posix()}",
        })
    return sorted(out, key=lambda c: c["when"], reverse=True)


def ago(t: dt.datetime | None, now: dt.datetime) -> str:
    if not t:
        return "never"
    mins = int((now - t).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 0)} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def stamp(t: dt.datetime | None) -> str:
    return t.astimezone(TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ") if t else "—"


def build(now: dt.datetime) -> str:
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    sources = [{**cfg.get("defaults", {}), **s} for s in cfg["sources"]]
    cloud, local = load_json("state.json"), load_json("state-local.json")
    names = {s["id"]: s["name"] for s in sources}
    e = html.escape

    # ---- runners
    runners = []
    for label, st, stale_h, note in (
        ("GitHub Actions", cloud, 3, "hourly, cloud"),
        ("Office PC", local, LOCAL_STALE_HOURS, "hourly, sites that block cloud servers"),
    ):
        lr = st.get("last_run", {})
        at = parse_time(lr.get("at"))
        ok = at is not None and (now - at).total_seconds() / 3600 <= stale_h
        runners.append(
            f'<div class="card {"ok" if ok else "bad"}"><div class="k">{e(label)}</div>'
            f'<div class="v">{"Running" if ok else "Not reporting"}</div>'
            f'<div class="s">Last run {e(ago(at, now))} · {e(stamp(at))}<br>'
            f'{lr.get("checked", 0)} checked · {lr.get("changed", 0)} changed · {lr.get("failed", 0)} failed'
            f'<br><span class="mute">{e(note)}</span></div></div>')

    # ---- sources
    rows_by_group: dict[str, list[tuple[int, str]]] = {}
    counts = {"ok": 0, "warn": 0, "bad": 0, "off": 0}
    for s in sources:
        st = (local if s.get("runner") == "local" else cloud).get("sources", {}).get(s["id"], {})
        checked, changed = parse_time(st.get("last_checked")), parse_time(st.get("last_changed"))
        fails = st.get("fail_count", 0)
        every = int(s.get("every", 1))
        if fails >= FAIL_ALERT_AT:
            status, cls, rank = f"Failing ({fails}×)", "bad", 0
        elif fails:
            status, cls, rank = f"Failed last {fails}×", "warn", 1
        elif not checked:
            status, cls, rank = "Not checked yet", "off", 2
        elif (now - checked).total_seconds() / 3600 > every + LATE_GRACE_HOURS:
            status, cls, rank = "Late", "warn", 1
        else:
            status, cls, rank = "OK", "ok", 3
        counts[cls] += 1
        err = f'<div class="err">{e(st["last_error"])}</div>' if fails and st.get("last_error") else ""
        tags = []
        if s.get("priority") == "high":
            tags.append('<span class="tag hi">high</span>')
        if s.get("runner") == "local":
            tags.append('<span class="tag">office PC</span>')
        if every > 1:
            tags.append(f'<span class="tag">every {every} h</span>')
        if s.get("allow_missing"):
            tags.append('<span class="tag">may be offline</span>')
        rows_by_group.setdefault(s.get("group", "Other"), []).append((rank, (
            f'<tr><td><a href="{e(s["url"])}" rel="noopener">{e(s["name"])}</a> {"".join(tags)}{err}</td>'
            f'<td><span class="pill {cls}">{e(status)}</span></td>'
            f'<td title="{e(stamp(checked))}">{e(ago(checked, now))}</td>'
            f'<td title="{e(stamp(changed))}">{e(ago(changed, now) if changed else "—")}</td></tr>')))

    groups = []
    for g, rows in rows_by_group.items():
        rows.sort(key=lambda r: r[0])
        groups.append(f'<tbody><tr class="grp"><th colspan="4">{e(g)} <span class="mute">({len(rows)})</span>'
                      f'</th></tr>{"".join(r for _, r in rows)}</tbody>')

    # ---- recent changes
    changes = load_changes(names, now - dt.timedelta(days=RECENT_DAYS))
    if changes:
        items = "".join(
            f'<li class="sev-{e(c["severity"])}"><a href="{e(c["url"])}">{e(c["name"])}</a>'
            f'<span class="when">{e(stamp(c["when"]))} · {e(c["severity"])}</span>'
            + (f'<div class="prev">{e(c["preview"])}</div>' if c["preview"] else "") + "</li>"
            for c in changes[:MAX_LISTED])
        more = (f'<p class="mute">{len(changes) - MAX_LISTED} older changes are in '
                f'<a href="https://github.com/{REPO}/tree/main/changes">changes/</a>.</p>'
                if len(changes) > MAX_LISTED else "")
        recent = f'<ul class="changes">{items}</ul>{more}'
    else:
        recent = '<p class="mute">No changes in this period.</p>'
    by_sev = {k: sum(c["severity"] == k for c in changes) for k in SEV_RANK}

    return PAGE.format(
        generated=e(stamp(now)), repo=e(REPO), runners="".join(runners),
        n_sources=len(sources), n_ok=counts["ok"], n_attn=counts["warn"] + counts["bad"],
        n_changes=len(changes), days=RECENT_DAYS, n_high=by_sev["high"], n_med=by_sev["medium"],
        n_low=by_sev["low"], recent=recent, groups="".join(groups))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>immi-watch status</title>
<style>
:root {{ --bg:#f6f7f9; --panel:#fff; --ink:#1b1f24; --mute:#667085; --line:#e4e7ec;
  --ok:#12805c; --ok-bg:#e7f6ef; --warn:#a15c07; --warn-bg:#fdf3e1; --bad:#c0262d; --bad-bg:#fdecec;
  --off:#667085; --off-bg:#f0f2f5; --link:#1d5bd8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1216; --panel:#171b21; --ink:#e6e9ee; --mute:#98a2b3;
  --line:#2a3038; --ok:#4cc38a; --ok-bg:#12291f; --warn:#f0a646; --warn-bg:#2d2312; --bad:#f2716f;
  --bad-bg:#331819; --off:#98a2b3; --off-bg:#20252c; --link:#79a6ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1080px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:24px; margin:0; }} h2 {{ font-size:17px; margin:32px 0 12px; }}
a {{ color:var(--link); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.mute {{ color:var(--mute); font-weight:400; }}
header p {{ margin:4px 0 0; color:var(--mute); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-top:20px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; border-left:4px solid var(--off); }}
.card.ok {{ border-left-color:var(--ok); }} .card.bad {{ border-left-color:var(--bad); }} .card.warn {{ border-left-color:var(--warn); }}
.k {{ font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--mute); }}
.v {{ font-size:22px; font-weight:650; margin:2px 0; }} .s {{ font-size:13px; color:var(--mute); }}
.changes {{ list-style:none; margin:0; padding:0; background:var(--panel); border:1px solid var(--line); border-radius:10px; }}
.changes li {{ padding:10px 14px 10px 30px; border-top:1px solid var(--line); position:relative; }}
.changes li:first-child {{ border-top:0; }}
.changes li::before {{ content:""; position:absolute; left:12px; top:17px; width:9px; height:9px; border-radius:50%; background:var(--off); }}
.changes li.sev-high::before {{ background:var(--bad); }} .changes li.sev-medium::before {{ background:var(--warn); }}
.when {{ display:block; font-size:13px; color:var(--mute); }}
.prev {{ font-size:13px; margin-top:2px; overflow-wrap:anywhere; }}
.tablewrap {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px; }}
table {{ width:100%; border-collapse:collapse; min-width:600px; }}
th, td {{ text-align:left; padding:8px 12px; border-top:1px solid var(--line); vertical-align:top; }}
thead th {{ border-top:0; font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--mute); }}
tr.grp th {{ background:var(--off-bg); font-size:13px; }}
td:nth-child(n+2) {{ white-space:nowrap; font-size:13px; }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:99px; font-size:12px; font-weight:600; }}
.pill.ok {{ color:var(--ok); background:var(--ok-bg); }} .pill.warn {{ color:var(--warn); background:var(--warn-bg); }}
.pill.bad {{ color:var(--bad); background:var(--bad-bg); }} .pill.off {{ color:var(--off); background:var(--off-bg); }}
.tag {{ display:inline-block; font-size:11px; padding:0 6px; border:1px solid var(--line); border-radius:4px; color:var(--mute); margin-left:4px; }}
.tag.hi {{ color:var(--bad); border-color:var(--bad); }}
.err {{ font-size:12px; color:var(--bad); margin-top:2px; overflow-wrap:anywhere; }}
footer {{ margin-top:32px; font-size:13px; color:var(--mute); }}
</style></head>
<body><main>
<header><h1>immi-watch status</h1>
<p>Hourly change monitor for Australian immigration and education websites · updated {generated} (Melbourne time)</p></header>
<div class="grid">{runners}
<div class="card"><div class="k">Sources</div><div class="v">{n_ok} / {n_sources} OK</div>
<div class="s">{n_attn} need attention</div></div>
<div class="card"><div class="k">Changes, last {days} days</div><div class="v">{n_changes}</div>
<div class="s">{n_high} high · {n_med} medium · {n_low} low</div></div>
</div>
<h2>Recent changes</h2>
{recent}
<h2>Sources</h2>
<div class="tablewrap"><table>
<thead><tr><th>Page</th><th>Status</th><th>Last checked</th><th>Last changed</th></tr></thead>
{groups}
</table></div>
<footer>Alerts are raised as <a href="https://github.com/{repo}/issues">GitHub Issues</a>.
Always verify against the official page before advising clients.</footer>
</main></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="_site")
    args = ap.parse_args()
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(build(dt.datetime.now(TZ)), encoding="utf-8", newline="\n")
    (out / ".nojekyll").write_text("")
    print(f"wrote {out / 'index.html'}")


if __name__ == "__main__":
    main()
