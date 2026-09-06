"""
Market Watcher Agent — the daily scan that keeps the rest of the system fed.

Runs once a day. Builds a watch list from the roles people have actually
saved (`saved_roles` — their current role plus anything they marked
interested/watching), pulls candidate headlines from the feeds in
`data/watch_sources.csv`, drops anything already in the `signals` table,
and asks the cheap model one narrow question per candidate: does this
matter to any role on the watch list?

Anything that survives is written into `signals` as an unvalidated row.
It is NOT presented to anyone yet — the Signal Checker validates the
evidence first, exactly as it does for the pre-seeded signals. This agent
finds things; it doesn't get to decide they're true.

    python agents/market_watcher_agent.py                  # scan the real feeds
    python agents/market_watcher_agent.py --dry-run        # show what it'd write
    python agents/market_watcher_agent.py --source seeded  # replay signals.csv, no network

Running it daily (pick one):
  - EventBridge rule -> Lambda, `rate(1 day)`, if this moves to Lambda
  - cron on any always-on box:  0 7 * * *  python agents/market_watcher_agent.py
  - GitHub Actions schedule, if the repo has AWS creds

A note on the feeds: `watch_sources.csv` ships with five plausible feeds,
several marked VERIFY. A feed that 404s or changes shape is skipped with a
warning rather than crashing the run — one dead source must never stop the
daily scan. Check the summary line for how many sources actually answered.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from config import AWS_REGION, BEDROCK_REGION, CLAUDE_MODEL_ID, NOVA_MICRO_MODEL_ID, table_name

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USER_AGENT = "CareerGuardian/1.0 (hackathon project; daily career-signal scan)"
FETCH_TIMEOUT = 15


def decimal_to_native(obj):
    if isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def scan_all(short_table):
    table = dynamodb.Table(table_name(short_table))
    items, resp = [], table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return decimal_to_native(items)


def build_watch_list() -> list[dict]:
    """Every role anyone is in or curious about. One person marking a role
    'watching' is enough to start scanning for it — that's the whole point
    of letting people save future roles."""
    saved = scan_all("saved_roles")
    by_role = {}
    for row in saved:
        role = row.get("target_role")
        if not role:
            continue
        entry = by_role.setdefault(role, {"target_role": role, "role_family": row.get("role_family"), "watchers": 0})
        entry["watchers"] += 1
    return sorted(by_role.values(), key=lambda r: -r["watchers"])


def read_sources() -> list[dict]:
    path = DATA_DIR / "watch_sources.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch_feed(source: dict) -> list[dict]:
    """Parse an RSS or Atom feed with the stdlib. Returns [] and warns on
    any failure — a dead source is a bad day for that source, not for the
    scan."""
    url = source["feed_url"]
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            raw = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"  [skip] {source['name']}: {e}")
        return []

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as e:
        print(f"  [skip] {source['name']}: not parseable as XML ({e})")
        return []

    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(".//item") or root.findall(f".//{atom}entry")

    items = []
    for entry in entries[:15]:
        title = entry.findtext("title") or entry.findtext(f"{atom}title") or ""
        link = entry.findtext("link") or ""
        if not link:
            link_el = entry.find(f"{atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
        summary = (
            entry.findtext("description")
            or entry.findtext(f"{atom}summary")
            or entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            or ""
        )
        published = (
            entry.findtext("pubDate")
            or entry.findtext(f"{atom}published")
            or entry.findtext(f"{atom}updated")
            or ""
        )
        if not title.strip():
            continue
        items.append(
            {
                "title": strip_html(title),
                "url": link.strip(),
                "summary": strip_html(summary)[:600],
                "published": published.strip(),
                "source": source["name"],
                "source_id": source["source_id"],
            }
        )
    print(f"  [ok]   {source['name']}: {len(items)} items")
    return items


def seeded_candidates() -> list[dict]:
    """Offline mode: replay signals.csv as if it had just arrived. Lets you
    demo and test the triage logic with no network and no spend."""
    path = DATA_DIR / "signals.csv"
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [
        {
            "title": r["title"],
            "url": r["source_url"],
            "summary": r.get("signal_summary", ""),
            "published": r.get("date", ""),
            "source": r.get("source", "seeded"),
            "source_id": "SEEDED",
        }
        for r in rows
    ]


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def drop_known(candidates: list[dict], existing: list[dict]) -> list[dict]:
    """Dedupe on URL first, then on normalised title — the same story gets
    re-syndicated under slightly different headlines all the time."""
    seen_urls = {(s.get("source_url") or "").strip().rstrip("/") for s in existing}
    seen_titles = {normalise(s.get("title")) for s in existing}

    fresh, seen_this_run = [], set()
    for c in candidates:
        url_key = c["url"].strip().rstrip("/")
        title_key = normalise(c["title"])
        if url_key and url_key in seen_urls:
            continue
        if title_key in seen_titles or title_key in seen_this_run:
            continue
        seen_this_run.add(title_key)
        fresh.append(c)
    return fresh


def build_system_prompt(watch_list: list[dict]) -> str:
    roles = [w["target_role"] for w in watch_list]
    return f"""You are the Market Watcher in a career-disruption-monitoring system
for Singapore workers. Today is {date.today().isoformat()}.

You triage incoming headlines. For each one, decide whether it says
something real about how WORK ITSELF is changing for any role on this
watch list: {roles}

Keep an item only if it describes technology, automation or hiring shifts
that change what people in those roles actually do day to day.

Discard: funding rounds, product launches with no workforce angle, company
gossip, opinion pieces with no evidence, and anything about a sector nobody
on the watch list works in. When in doubt, discard — a false positive costs
someone's attention and a validation call downstream.

You are triaging, not judging whether the claim is true. A later agent
checks the evidence properly. Your only question is "could this matter to
one of these roles?"

CRITICAL: affected_roles must only contain roles from the watch list above.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "keep": [
    {{
      "index": <the item's index number as given>,
      "affected_roles": [<roles from the watch list>],
      "sector": "<Technology, Finance, Marketing, Science or All>",
      "technology": "<the technology or shift in 2-4 words>",
      "why_it_matters": "<one sentence on what changes for those roles>"
    }}
  ]
}}"""


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0.2},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def triage(candidates: list[dict], watch_list: list[dict], model_id: str) -> list[dict]:
    if not candidates:
        return []

    payload = [
        {"index": i, "title": c["title"], "summary": c["summary"], "source": c["source"]}
        for i, c in enumerate(candidates)
    ]
    raw = call_model(build_system_prompt(watch_list), json.dumps(payload, indent=2), model_id)
    verdict = parse_agent_json(raw)

    valid_roles = {w["target_role"] for w in watch_list}
    kept = []
    for item in verdict.get("keep", []):
        idx = item.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(candidates):
            continue
        roles = [r for r in item.get("affected_roles", []) if r in valid_roles]
        if not roles:
            continue
        kept.append({**candidates[idx], "affected_roles": roles,
                     "sector": item.get("sector", "All"),
                     "technology": item.get("technology", ""),
                     "why_it_matters": item.get("why_it_matters", "")})
    return kept


def signal_id_for(item: dict) -> str:
    """Stable ID from the URL, so re-running the scan can't create a second
    row for the same story even if dedupe upstream misses it."""
    basis = (item["url"] or item["title"]).strip().rstrip("/")
    return f"SIGW{hashlib.sha1(basis.encode()).hexdigest()[:8].upper()}"


def to_signal_row(item: dict) -> dict:
    return {
        "signal_id": signal_id_for(item),
        "title": item["title"],
        "sector": item.get("sector", "All"),
        "technology": item.get("technology", ""),
        "date": item.get("published") or date.today().isoformat(),
        "source": item["source"],
        "source_url": item["url"],
        "evidence": item.get("summary", ""),
        # Left blank on purpose: the Signal Checker sets these after it
        # looks at the evidence. A watcher that pre-filled severity would
        # be grading its own homework.
        "severity": "Unrated",
        "frequency": "Unrated",
        "source_credibility": "Unrated",
        "affected_roles": item["affected_roles"],
        "signal_summary": item.get("why_it_matters", ""),
        "discovered_by": "market_watcher",
        "discovered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "validated": False,
    }


def write_signals(rows: list[dict]):
    table = dynamodb.Table(table_name("signals"))
    with table.batch_writer() as batch:
        for row in rows:
            batch.put_item(Item=json.loads(json.dumps(row), parse_float=Decimal))


def run_market_watcher(source_mode: str = "feeds", model_key: str = "nova", dry_run: bool = False) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    watch_list = build_watch_list()
    if not watch_list:
        print("No saved roles across any user — nothing to watch for. Seed saved_roles.csv first.")
        return {"scanned": 0, "kept": 0, "written": 0, "watch_list": []}

    print(f"Watching {len(watch_list)} roles: {', '.join(w['target_role'] for w in watch_list[:6])}"
          f"{' …' if len(watch_list) > 6 else ''}")

    if source_mode == "seeded":
        candidates = seeded_candidates()
        print(f"Seeded mode: replaying {len(candidates)} rows from signals.csv")
    else:
        sources = read_sources()
        print(f"Fetching {len(sources)} feeds:")
        candidates = [item for s in sources for item in fetch_feed(s)]

    existing = scan_all("signals")
    fresh = drop_known(candidates, existing)
    print(f"{len(candidates)} fetched, {len(candidates) - len(fresh)} already known, {len(fresh)} new")

    kept = triage(fresh, watch_list, model_id) if fresh else []
    print(f"{len(kept)} passed triage")

    rows = [to_signal_row(k) for k in kept]

    if dry_run:
        print("\n--dry-run: not writing. Would add:")
        print(json.dumps(rows, indent=2))
    elif rows:
        write_signals(rows)
        print(f"Wrote {len(rows)} unvalidated signals. Run signal_agent.py on each to validate.")
        for row in rows:
            print(f"  {row['signal_id']}  {row['title'][:70]}")
    else:
        print("Nothing new worth adding today.")

    return {
        "scanned": len(candidates),
        "new": len(fresh),
        "kept": len(kept),
        "written": 0 if dry_run else len(rows),
        "signal_ids": [r["signal_id"] for r in rows],
        "watch_list": [w["target_role"] for w in watch_list],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["feeds", "seeded"], default="feeds")
    parser.add_argument("--model", choices=["claude", "nova"], default="nova")
    parser.add_argument("--dry-run", action="store_true", help="triage but don't write to DynamoDB")
    args = parser.parse_args()

    run_market_watcher(source_mode=args.source, model_key=args.model, dry_run=args.dry_run)
