#!/usr/bin/env python3
"""immi-watch: hourly change monitor for Australian immigration websites.

Fetches every page in sources.yaml, reduces it to clean text, compares it with the
last snapshot and raises alerts for anything that changed:

  * GitHub Issue (when running in GitHub Actions)  -> GitHub emails you
  * Email via SMTP                                  (if SMTP_* env vars are set)
  * Teams / Slack webhook                           (if ALERT_WEBHOOK_URL is set)
  * Claude summary + severity                       (if ANTHROPIC_API_KEY is set)

Usage:
  python monitor.py                 # normal run (what the scheduler calls)
  python monitor.py --only ha-500   # check selected source ids, ignoring `every`
  python monitor.py --dry-run       # fetch + diff, but don't save or notify
  python monitor.py --show ha-500   # print the extracted text for one source
  python monitor.py --test-alert    # send a test notification through every channel
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import smtplib
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "snapshots"
CHANGE_DIR = ROOT / "changes"
STATE_FILE = ROOT / "state.json"
SOURCES_FILE = ROOT / "sources.yaml"

TZ = ZoneInfo("Australia/Melbourne")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
REQUEST_DELAY = 1.0          # seconds between requests to the same site (be polite)
HTTP_TIMEOUT = 30
HTTP_WORKERS = 6             # different sites are fetched in parallel, each site sequentially
FAIL_ALERT_AT = 3            # consecutive failures before a health alert
DIGEST_HOUR = 8              # Melbourne hour at which low-severity changes are sent
MIN_TEXT_CHARS = 200         # less than this = treat as a failed/blocked fetch
MAX_DIFF_LINES = 60          # per change, in alert bodies
AI_MODEL = os.environ.get("IMMI_AI_MODEL", "claude-opus-5-5")

# Lines that change on every load or carry no meaning.
NOISE = re.compile(
    r"^((page|content|this page was)\s+)?(last\s+)?(updated|modified|reviewed)\b"
    r"|^share( this page)?$|^print( this page)?$|^back to top$|^skip to\b"
    r"|^©|^copyright\b|^\d+\s*(views?|min(ute)?s? read)$",
    re.I,
)
# Words that make a change worth an immediate alert.
HIGH_WORDS = re.compile(
    r"invitation|round|nominat|allocation|\bcap\b|ceiling|occupation|points?\b|\bfees?\b|\$\s?\d"
    r"|price|effective|commenc|\bclos(e|ed|es|ing)\b|paused?|suspend|\bopen(ed|s|ing)?\b"
    r"|requirement|amend|ministerial direction|legislative instrument|deadline|english"
    r"|financial capacity|genuine student|processing time|planning level|subclass \d{3}",
    re.I,
)
HA_SKIP_FIELDS = ("Translate", "BannerImage", "PageImage", "Thumbnail")


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


MISSING_LINE = "[This page currently returns 404 Not Found – it is not published right now]"


# --------------------------------------------------------------------------- text

def clean_lines(text: str) -> list[str]:
    out: list[str] = []
    text = re.sub(r"[​‌‍⁠﻿]", "", text)
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw.replace("\xa0", " ")).strip()
        line = re.sub(r"\s+([,.;:!?)])", r"\1", line)
        if not line or NOISE.search(line):
            continue
        if out and out[-1] == line:
            continue
        out.append(line)
    return out


def html_to_lines(markup: str | bytes, selector: str | None = None) -> list[str]:
    soup = BeautifulSoup(markup, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template", "button"]):
        tag.decompose()
    root = soup.select_one(selector) if selector else None
    if root is None:
        # First candidate with real text (a "#main-content" can be an empty skip-link anchor).
        candidates = [soup.select_one(s) for s in ("main", "[role=main]", "#main-content")]
        root = next((c for c in candidates if c is not None and len(c.get_text(strip=True)) > 150),
                    soup.body or soup)
    for tag in root.select("nav, header, footer, aside, [role=navigation], [role=banner], "
                           "[role=contentinfo], .breadcrumb, .breadcrumbs, [aria-label*=readcrumb]"):
        tag.decompose()
    return clean_lines(block_text(root))


BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
              "table", "section", "article", "dt", "dd", "dl", "blockquote", "pre", "figcaption",
              "summary", "details", "option", "label", "legend", "fieldset", "hr"]


def block_text(root) -> str:
    """get_text that breaks lines only at block elements, so inline links stay in their sentence."""
    for br in root.find_all("br"):
        br.replace_with("\n")
    for tag in root.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return root.get_text("")


def _walk_json(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _walk_json(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_json(value, out)
    elif isinstance(node, str):
        s = node.strip()
        if not s or s.lower() in ("true", "false", "null") or re.fullmatch(r"(https?:)?/[^\s]*", s):
            return
        if "<" in s and ">" in s:
            out.extend(html_to_lines(f"<body>{s}</body>"))
        else:
            out.extend(clean_lines(s))


# ------------------------------------------------------------------------ fetchers

class Fetcher:
    def __init__(self) -> None:
        self._local = threading.local()
        self._pw = self._browser = self._ctx = None

    @property
    def http(self) -> requests.Session:
        """One requests.Session per thread (HTTP sources for different hosts run in parallel)."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9"})
        return s

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def get(self, url: str) -> requests.Response:
        r = self.http.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}", r.status_code)
        return r

    # --- methods -----------------------------------------------------------
    def fetch(self, src: dict) -> list[str]:
        try:
            return self._fetch(src)
        except FetchError as e:
            # Pages that are taken down between program years: record "missing" as a state,
            # so the page coming back (or disappearing) is itself an alert.
            if e.status in (404, 410) and src.get("allow_missing"):
                return [MISSING_LINE]
            raise

    def _fetch(self, src: dict) -> list[str]:
        method = src.get("method", "http")
        if method == "http":
            return html_to_lines(self.get(src["url"]).content, src.get("selector"))
        if method == "homeaffairs":
            return self.homeaffairs(src)
        if method == "ha_news":
            return self.ha_news(src)
        if method == "browser":
            return html_to_lines(self.browser_html(src["url"]), src.get("selector"))
        raise FetchError(f"unknown method {method!r}")

    def homeaffairs(self, src: dict) -> list[str]:
        """Home Affairs pages are SharePoint shells; the content sits in hidden JSON/HTML fields."""
        markup = self.get(src["url"]).content.decode("utf-8", "replace")
        soup = BeautifulSoup(markup, "lxml")
        lines: list[str] = []
        if soup.title and soup.title.string:
            lines.append(soup.title.string.strip())
        for inp in soup.select('input[type=hidden][name^="ctl00$PlaceHolderMain"]'):
            if any(s in inp.get("name", "") for s in HA_SKIP_FIELDS):
                continue
            value = (inp.get("value") or "").strip()
            if not value or value in ("True", "False"):
                continue
            try:
                _walk_json(json.loads(value), lines)
            except ValueError:
                lines.extend(html_to_lines(f"<body>{value}</body>"))
        # Visible (non-hidden) page fields, e.g. rich-text zones.
        for zone in soup.select('[id^="ctl00_PlaceHolderMain"]'):
            if zone.name in ("input", "script"):
                continue
            for inp in zone.select("input"):
                inp.decompose()
            lines.extend(clean_lines(block_text(zone)))
            zone.decompose()
        return clean_lines("\n".join(lines))

    def ha_news(self, src: dict) -> list[str]:
        """Home Affairs news archive is an Angular app backed by Data.aspx/GetNews."""
        parts = urlsplit(src["url"])
        base = f"{parts.scheme}://{parts.netloc}"
        items: dict[str, dict] = {}
        year = dt.datetime.now(TZ).year
        for y in (year, year - 1):
            body = {"siteUrl": "https://www.homeaffairs.gov.au", "webUrl": "/News-subsite",
                    "filter": {"Month": None, "Year": str(y), "Categories": [], "PageNumber": 1,
                               "RowLimit": 50, "ShowCurrentSiteOnly": False,
                               "CurrentSite": src.get("site", "Immi")}}
            r = self.http.post(f"{base}/_layouts/15/api/Data.aspx/GetNews", json=body, timeout=HTTP_TIMEOUT,
                               headers={"Accept": "application/json"})
            if r.status_code >= 400:
                raise FetchError(f"GetNews HTTP {r.status_code}")
            r.encoding = "utf-8"
            data = r.json().get("d", {}).get("data") or []
            if isinstance(data, dict):
                data = data.get("results", [])
            for it in data:
                items[str(it.get("Id"))] = it
        if not items:
            raise FetchError("GetNews returned no items")

        def item_date(it):
            raw = str(it.get("Date") or "").strip()
            m = re.search(r"/Date\((\d+)", raw)
            if m:
                return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, TZ).date().isoformat()
            for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return dt.datetime.strptime(raw[:19], fmt).date().isoformat()
                except ValueError:
                    pass
            return raw

        rows = sorted(items.values(), key=lambda it: (item_date(it), int(it.get("Id") or 0)), reverse=True)
        lines = []
        for it in rows[:40]:
            url = it.get("NewsRedirectLink") or it.get("Url") or ""
            url = urljoin(base + "/", url) if url else src["url"]
            title = re.sub(r"\s+", " ", str(it.get("Title") or "")).strip()
            lines.append(f"{item_date(it)} | {it.get('Category') or '-'} | {title} | {url}")
        return lines

    def browser_html(self, url: str) -> str:
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"])
            self._ctx = self._browser.new_context(user_agent=UA, locale="en-AU",
                                                  viewport={"width": 1366, "height": 900})
            self._ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            # Text is all we need: skip images, fonts and media to keep runs short.
            self._ctx.route("**/*", lambda route: route.abort()
                            if route.request.resource_type in ("image", "font", "media")
                            else route.continue_())
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            for _ in range(20):                       # wait out a Cloudflare interstitial
                if "just a moment" not in page.title().lower():
                    break
                page.wait_for_timeout(1000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            title = page.title()
            if "just a moment" in title.lower() or "access denied" in title.lower():
                raise FetchError(f"blocked by bot protection ({title!r})")
            if resp is not None and resp.status >= 400 and resp.status not in (403,):
                raise FetchError(f"HTTP {resp.status}", resp.status)
            return page.content()
        finally:
            page.close()


# --------------------------------------------------------------------------- model

@dataclass
class Change:
    src: dict
    added: list[str]
    removed: list[str]
    unified: str
    severity: str = "medium"
    summary: str = ""
    points: list[str] = field(default_factory=list)
    record: str = ""


def diff_lines(old: list[str], new: list[str]) -> tuple[list[str], list[str], str]:
    added, removed = [], []
    for line in difflib.ndiff(old, new):
        if line.startswith("+ "):
            added.append(line[2:])
        elif line.startswith("- "):
            removed.append(line[2:])
    unified = "\n".join(difflib.unified_diff(old, new, "before", "after", lineterm="", n=1))
    return added, removed, unified


def heuristic_severity(ch: Change) -> str:
    text = "\n".join(ch.added + ch.removed)
    if ch.src.get("method") == "ha_news" or MISSING_LINE in ch.added + ch.removed:
        return "high"
    if len(text) < 40 and not re.search(r"\d", text):
        return "low"
    if ch.src.get("priority") == "high" or HIGH_WORDS.search(text):
        return "high"
    return "medium"


AI_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["severity", "summary", "key_points"],
    "additionalProperties": False,
}
AI_SYSTEM = (
    "You review changes detected on Australian immigration and education websites for a "
    "registered migration and education agency. You get the lines removed from and added to "
    "one page since the last check. Say plainly what changed, quoting exact figures, dates, "
    "occupations or visa subclasses where they appear. Severity: high = anything that can "
    "affect a client's eligibility, application, fees, deadlines, nomination/invitation "
    "rounds, occupation lists or policy, or a new news item/announcement; medium = new or "
    "reworded guidance with little practical effect; low = cosmetic, navigation, typo or "
    "formatting changes. Keep the summary to 1-2 sentences and key_points to at most 5 "
    "short bullets. Base everything only on the lines given."
)


def ai_review(ch: Change) -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic
        client = anthropic.Anthropic()
        removed = "\n".join(ch.removed) or "(none)"
        added = "\n".join(ch.added) or "(none)"
        prompt = (f"Page: {ch.src['name']}\nURL: {ch.src['url']}\n\n"
                  f"<removed_lines>\n{removed}\n</removed_lines>\n\n<added_lines>\n{added}\n</added_lines>")
        resp = client.beta.messages.create(
            model=AI_MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=AI_SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": AI_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return False
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        ch.severity = data["severity"]
        ch.summary = data["summary"].strip()
        ch.points = [p.strip() for p in data["key_points"] if p.strip()]
        return True
    except Exception as e:  # never let the AI step break monitoring
        print(f"  ! AI review failed for {ch.src['id']}: {type(e).__name__}: {e}")
        return False


# ------------------------------------------------------------------------ alerts

SEV_ICON = {"high": "🔴", "medium": "🟠", "low": "⚪"}


def change_md(ch: Change, with_diff: bool = True) -> str:
    s = ch.src
    parts = [f"### {SEV_ICON[ch.severity]} {s['name']}",
             f"**{s.get('group', '')}** · [{s['url']}]({s['url']}) · severity: **{ch.severity}**"]
    if ch.summary:
        parts.append(f"\n{ch.summary}")
    for p in ch.points:
        parts.append(f"- {p}")
    if not ch.summary:
        if ch.added:
            parts.append("\n**Added:**\n" + "\n".join(f"- {l[:300]}" for l in ch.added[:12]))
        if ch.removed and s.get("method") != "ha_news":
            parts.append("\n**Removed:**\n" + "\n".join(f"- ~~{l[:300]}~~" for l in ch.removed[:12]))
    if with_diff and ch.unified:
        lines = ch.unified.splitlines()
        shown = "\n".join(lines[:MAX_DIFF_LINES])
        more = f"\n… {len(lines) - MAX_DIFF_LINES} more diff lines in `{ch.record}`" if len(lines) > MAX_DIFF_LINES else ""
        parts.append(f"\n<details><summary>Full diff</summary>\n\n```diff\n{shown}\n```{more}\n</details>")
    return "\n".join(parts)


def plain_text(md: str) -> str:
    md = re.sub(r"</?details>|<summary>.*?</summary>", "", md)
    return re.sub(r"\*\*|~~", "", md)


def notify(title: str, body_md: str, labels: list[str]) -> None:
    mention = os.environ.get("ALERT_MENTION", "").strip()
    footer = "\n\n---\n_Sent by immi-watch. Always verify against the official page before advising clients._"
    gh_body = body_md + footer + (f"\n\ncc @{mention.lstrip('@')}" if mention else "")
    sent = []

    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if token and repo:
        try:
            r = requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                              headers={"Authorization": f"Bearer {token}",
                                       "Accept": "application/vnd.github+json"},
                              json={"title": title, "body": gh_body[:65000], "labels": labels})
            r.raise_for_status()
            sent.append(f"issue {r.json().get('html_url')}")
        except Exception as e:
            print(f"  ! GitHub issue failed: {e}")

    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_EMAIL_TO")
    if host and to:
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "immi-watch")
            msg["To"] = to
            msg.set_content(plain_text(body_md + footer))
            port = int(os.environ.get("SMTP_PORT", "587"))
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls()
                if os.environ.get("SMTP_USER"):
                    smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                smtp.send_message(msg)
            sent.append(f"email {to}")
        except Exception as e:
            print(f"  ! email failed: {e}")

    hook = os.environ.get("ALERT_WEBHOOK_URL")
    if hook:
        try:
            text = f"**{title}**\n\n{plain_text(re.sub(r'```diff.*?```', '', body_md, flags=re.S))}"
            requests.post(hook, json={"text": text[:20000]}, timeout=30).raise_for_status()
            sent.append("webhook")
        except Exception as e:
            print(f"  ! webhook failed: {e}")

    print(f"  -> alert '{title}' sent via: {', '.join(sent) or 'nothing configured (printed only)'}")
    if not sent:
        print(body_md)


# -------------------------------------------------------------------------- main

def load_sources() -> list[dict]:
    cfg = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    defaults = cfg.get("defaults", {})
    srcs = [{**defaults, **s} for s in cfg["sources"]]
    ids = [s["id"] for s in srcs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        sys.exit(f"duplicate source ids: {dupes}")
    return srcs


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"sources": {}, "pending_digest": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_record(ch: Change, now: dt.datetime) -> str:
    folder = CHANGE_DIR / now.strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now.strftime('%Y%m%d-%H%M')}_{ch.src['id']}.md"
    ch.record = str(path.relative_to(ROOT)).replace("\\", "/")
    body = change_md(ch, with_diff=False)
    path.write_text(f"{body}\n\nDetected {now.isoformat(timespec='minutes')}\n\n```diff\n{ch.unified}\n```\n",
                    encoding="utf-8")
    return ch.record


def fetch_all(fetcher: Fetcher, sources: list[dict]) -> dict[str, list[str] | Exception]:
    """Fetch every source. Different sites run in parallel threads, pages on the same site run
    one after another with REQUEST_DELAY between them; browser pages run on the main thread."""
    results: dict[str, list[str] | Exception] = {}

    def fetch_one(src: dict) -> None:
        try:
            lines = fetcher.fetch(src)
            if lines != [MISSING_LINE] and sum(map(len, lines)) < MIN_TEXT_CHARS:
                raise FetchError(f"almost no content extracted ({sum(map(len, lines))} chars)")
            results[src["id"]] = lines
        except Exception as e:
            results[src["id"]] = e

    def site_worker(group: list[dict]) -> None:
        for i, src in enumerate(group):
            if i:
                time.sleep(REQUEST_DELAY)
            fetch_one(src)

    by_site: dict[str, list[dict]] = defaultdict(list)
    browser_srcs = []
    for src in sources:
        if src.get("method") == "browser":
            browser_srcs.append(src)
        else:
            by_site[urlsplit(src["url"]).netloc].append(src)
    with ThreadPoolExecutor(HTTP_WORKERS) as pool:
        futures = [pool.submit(site_worker, group) for group in by_site.values()]
        for src in browser_srcs:
            fetch_one(src)
        for f in futures:
            f.result()
    return results


def run(args) -> int:
    sources = load_sources()
    if args.only:
        wanted = set(args.only.split(","))
        sources = [s for s in sources if s["id"] in wanted]
    state = load_state()
    now = dt.datetime.now(TZ)
    hour_index = int(time.time() // 3600)
    SNAP_DIR.mkdir(exist_ok=True)
    fetcher = Fetcher()
    changes: list[Change] = []
    health: list[str] = []
    stats = {"checked": 0, "baseline": 0, "changed": 0, "failed": 0, "skipped": 0}

    due = [s for s in sources if args.only or not hour_index % int(s.get("every", 1))]
    stats["skipped"] = len(sources) - len(due)

    try:
        results = fetch_all(fetcher, due)
        for src in due:
            sid = src["id"]
            st = state["sources"].setdefault(sid, {})
            st["url"] = src["url"]
            lines = results[sid]
            if isinstance(lines, Exception):
                e = lines
                stats["failed"] += 1
                st["fail_count"] = st.get("fail_count", 0) + 1
                st["last_error"] = f"{now.isoformat(timespec='minutes')} {type(e).__name__}: {e}"[:300]
                print(f"FAIL {sid}: {st['last_error']}")
                if st["fail_count"] == FAIL_ALERT_AT:
                    health.append(f"- ❌ **{src['name']}** has failed {FAIL_ALERT_AT} checks in a row "
                                  f"— {st['last_error']} ([page]({src['url']}))")
                continue

            if st.get("fail_count", 0) >= FAIL_ALERT_AT:
                health.append(f"- ✅ **{src['name']}** is reachable again.")
            st["fail_count"] = 0
            st.pop("last_error", None)
            st["last_checked"] = now.isoformat(timespec="minutes")
            stats["checked"] += 1
            text = "\n".join(lines) + "\n"
            snap = SNAP_DIR / f"{sid}.txt"

            if not snap.exists():
                stats["baseline"] += 1
                print(f"BASE {sid}: {len(lines)} lines")
                if not args.dry_run:
                    snap.write_text(text, encoding="utf-8")
                    st["baseline"] = now.isoformat(timespec="minutes")
                continue

            old_lines = snap.read_text(encoding="utf-8").splitlines()
            if old_lines == lines:
                print(f"same {sid}")
                continue

            # Re-fetch once to make sure the change is real (not rotating content / a glitch).
            time.sleep(REQUEST_DELAY * 2)
            try:
                confirm = fetcher.fetch(src)
            except Exception:
                confirm = None
            if confirm != lines:
                st["unstable"] = st.get("unstable", 0) + 1
                print(f"UNSTABLE {sid}: content differed between two fetches; ignored this run")
                continue

            added, removed, unified = diff_lines(old_lines, lines)
            if src.get("method") == "ha_news" and not added:
                # Only old items aged out of the list; nothing new.
                if not args.dry_run:
                    snap.write_text(text, encoding="utf-8")
                print(f"same {sid} (old news items rolled off)")
                continue

            ch = Change(src, added, removed, unified)
            ch.severity = heuristic_severity(ch)
            used_ai = ai_review(ch)
            stats["changed"] += 1
            print(f"CHANGED {sid}: +{len(added)} -{len(removed)} severity={ch.severity}"
                  f"{' (AI)' if used_ai else ''}")
            if not args.dry_run:
                write_record(ch, now)
                snap.write_text(text, encoding="utf-8")
                st["last_changed"] = now.isoformat(timespec="minutes")
            changes.append(ch)
    finally:
        fetcher.close()

    print(f"\nSummary: {stats}")
    if args.dry_run:
        for ch in changes:
            print("\n" + change_md(ch))
        return 0

    # ---- alerts
    urgent = [c for c in changes if c.severity in ("high", "medium")]
    for c in changes:
        if c.severity == "low":
            state.setdefault("pending_digest", []).append(
                {"when": now.isoformat(timespec="minutes"), "md": change_md(c, with_diff=False),
                 "record": c.record})

    stamp = now.strftime("%a %d %b %Y, %I:%M %p")
    if urgent:
        urgent.sort(key=lambda c: (c.severity != "high", c.src.get("group", "")))
        n_high = sum(c.severity == "high" for c in urgent)
        names = ", ".join(dict.fromkeys(c.src["name"] for c in urgent))
        title = f"{'🔴' if n_high else '🟠'} {len(urgent)} update{'s' * (len(urgent) > 1)}: {names}"
        if len(title) > 200:
            title = title[:197] + "…"
        body = f"Detected **{stamp}** (Melbourne time).\n\n" + "\n\n".join(change_md(c) for c in urgent)
        notify(title, body, ["update", "high" if n_high else "medium"])

    if health:
        notify(f"⚠️ Monitor health: {len(health)} source(s) need attention",
               "\n".join(health) + "\n\nIf a page moved, update its URL in `sources.yaml`.",
               ["monitor-health"])

    pending = state.get("pending_digest", [])
    if pending and (now.hour == DIGEST_HOUR or args.force_digest):
        body = (f"Minor (low-severity) changes since the last digest — {len(pending)} item(s).\n\n"
                + "\n\n".join(f"{p['md']}\n_{p['when']} · `{p['record']}`_" for p in pending))
        notify(f"⚪ Daily digest: {len(pending)} minor change(s)", body, ["digest"])
        state["pending_digest"] = []

    state["last_run"] = {"at": now.isoformat(timespec="minutes"), **stats}
    save_state(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated source ids")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", metavar="ID", help="print extracted text for one source and exit")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--force-digest", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.test_alert:
        notify("✅ immi-watch test alert",
               "This is a test. If you can read this, alerts are reaching you.\n\n"
               f"Watching **{len(load_sources())}** pages every hour.", ["test"])
        return 0
    if args.show:
        src = next((s for s in load_sources() if s["id"] == args.show), None)
        if not src:
            sys.exit(f"no source {args.show!r}")
        f = Fetcher()
        try:
            print("\n".join(f.fetch(src)))
        finally:
            f.close()
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
