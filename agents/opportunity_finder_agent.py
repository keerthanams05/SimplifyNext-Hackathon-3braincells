"""
Opportunity Finder Agent — the "where do I actually go" agent.

Takes a user_id, reads the roles that person has saved (the one they're in
now, plus any they've marked interested/watching), and organises the real
Singapore career catalogue around EACH of those pathways:

    {
      "user_id": "USER004",
      "pathways": [
        {
          "target_role": "Finance Analyst",
          "status": "interested",
          "why_now": "...",
          "groups": {
            "Where the jobs are posted": [ {opportunity_id, name, url, ...} ],
            "People doing this already": [ ... ],
            "Things to turn up to":      [ ... ],
            "Courses":                   [ ... ]
          }
        }
      ]
    }

Design decision worth defending in the pitch: **the model never writes a
URL**. Every link comes out of `opportunities.csv` / `resources.csv` by ID,
and anything the model returns that isn't a real ID is dropped by
`filter_to_real_ids()`. The model's only job is choosing which of our
catalogue rows belong on which pathway, and writing the one-line reason.
That's why this is a Nova-tier agent and why the output is a browsable
list of links rather than a chat reply — a hallucinated course link in a
careers product is the worst possible failure.

Usage:
    python agents/opportunity_finder_agent.py USER004
    python agents/opportunity_finder_agent.py USER004 --role "Finance Analyst"
"""

import argparse
import json
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from config import AWS_REGION, BEDROCK_REGION, CLAUDE_MODEL_ID, NOVA_MICRO_MODEL_ID, table_name

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

# The buckets the UI renders as sections. Plain language on purpose —
# these are section headings a person reads, not internal categories.
GROUPS = {
    "Where the jobs are posted": ["Job board"],
    "People doing this already": ["Community", "Professional body", "Career service"],
    "Things to turn up to": ["Events", "Conference", "Hackathon", "Competition", "Industry news", "Research body"],
}


def decimal_to_native(obj):
    if isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def get_item(short_table, key):
    table = dynamodb.Table(table_name(short_table))
    item = table.get_item(Key=key).get("Item")
    return decimal_to_native(item) if item else None


def query_by_user(short_table, user_id):
    table = dynamodb.Table(table_name(short_table))
    items = table.query(KeyConditionExpression=Key("user_id").eq(user_id)).get("Items", [])
    return decimal_to_native(items)


def scan_all(short_table):
    table = dynamodb.Table(table_name(short_table))
    items, resp = [], table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return decimal_to_native(items)


def build_system_prompt(roles: list[str], opportunity_ids: list[str], resource_ids: list[str]) -> str:
    return f"""You are the Opportunity Finder Agent in a career-guidance system
for Singapore workers.

Today's real date is {date.today().isoformat()}. Treat the catalogue you are
given as accurate and current — never reject an entry because you don't
recognise it.

You will be given a person's profile, the career pathways they have saved,
and a catalogue of real Singapore job boards, communities, events,
competitions and courses. For EACH pathway, choose the catalogue entries
that genuinely help someone moving into or staying in that role.

Rules:
- Pick for the pathway, not for the person's current job title. A Finance
  Analyst pathway should get finance-analysis places even if they work in
  accounts payable today.
- Prefer entries whose role_family matches the pathway, plus the
  general-purpose ones that help anyone.
- A hackathon or competition belongs on a tech pathway; a research body or
  science festival belongs on a science pathway. Don't put a hackathon on a
  finance-processing pathway just to fill space.
- 3 to 6 entries per pathway is right. Fewer good ones beats a long list.

CRITICAL — you are choosing from fixed lists, never writing your own:
- opportunity_id must come from: {opportunity_ids}
- resource_id must come from: {resource_ids}
- target_role must come from: {roles}
Never write a URL. Never invent an ID. Anything not on these lists is
discarded before the person sees it.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "pathways": [
    {{
      "target_role": "<one of the given roles>",
      "why_now": "<one sentence on why these are the right places for this pathway right now>",
      "opportunity_ids": [<ids from the allowed opportunity list>],
      "resource_ids": [<ids from the allowed resource list, at most 3>]
    }}
  ]
}}"""


def build_user_prompt(persona: dict, saved_roles: list[dict], opportunities: list[dict], resources: list[dict]) -> str:
    payload = {
        "person": {
            "occupation": persona.get("occupation"),
            "industry": persona.get("industry"),
            "years_experience": persona.get("years_experience"),
            "career_goal": persona.get("career_goal"),
            "budget": persona.get("budget"),
            "weekly_learning_hours": persona.get("weekly_learning_hours"),
        },
        "saved_pathways": [
            {"target_role": r.get("target_role"), "status": r.get("status"), "role_family": r.get("role_family")}
            for r in saved_roles
        ],
        "opportunity_catalogue": [
            {
                "opportunity_id": o.get("opportunity_id"),
                "name": o.get("name"),
                "category": o.get("category"),
                "role_family": o.get("role_family"),
                "cadence": o.get("cadence"),
                "cost": o.get("cost"),
                "what_its_for": o.get("what_its_for"),
            }
            for o in opportunities
        ],
        "course_catalogue": [
            {
                "resource_id": r.get("resource_id"),
                "name": r.get("name"),
                "type": r.get("type"),
                "target_roles": r.get("target_roles"),
                "skills": r.get("skills"),
                "funding": r.get("funding"),
            }
            for r in resources
        ],
    }
    return json.dumps(payload, indent=2)


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 900, "temperature": 0.2},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def group_for(category: str) -> str:
    for group, categories in GROUPS.items():
        if category in categories:
            return group
    return "Things to turn up to"


def filter_to_real_ids(result: dict, saved_roles: list[dict], opportunities: list[dict], resources: list[dict]) -> list[dict]:
    """
    Rebuild the model's answer from our own records. Anything it invented
    simply isn't in the lookup, so it disappears here rather than reaching
    a person as a dead link.
    """
    opp_lookup = {o["opportunity_id"]: o for o in opportunities}
    res_lookup = {r["resource_id"]: r for r in resources}
    role_lookup = {r["target_role"]: r for r in saved_roles}

    chosen = {p.get("target_role"): p for p in result.get("pathways", []) if p.get("target_role") in role_lookup}

    pathways = []
    for role, saved in role_lookup.items():
        picked = chosen.get(role, {})
        groups = {name: [] for name in GROUPS}
        groups["Courses"] = []

        for oid in picked.get("opportunity_ids", []):
            o = opp_lookup.get(oid)
            if not o:
                continue
            groups[group_for(o.get("category"))].append(
                {
                    "opportunity_id": oid,
                    "name": o.get("name"),
                    "provider": o.get("provider"),
                    "category": o.get("category"),
                    "url": o.get("url"),
                    "cadence": o.get("cadence"),
                    "cost": o.get("cost"),
                    "what_its_for": o.get("what_its_for"),
                }
            )

        for rid in picked.get("resource_ids", [])[:3]:
            r = res_lookup.get(rid)
            if not r:
                continue
            groups["Courses"].append(
                {
                    "resource_id": rid,
                    "name": r.get("name"),
                    "provider": r.get("provider"),
                    "type": r.get("type"),
                    "url": r.get("url"),
                    "funding": r.get("funding"),
                }
            )

        pathways.append(
            {
                "target_role": role,
                "status": saved.get("status"),
                "role_family": saved.get("role_family"),
                "why_now": picked.get("why_now", ""),
                "groups": {name: items for name, items in groups.items() if items},
            }
        )

    # Current role first, then interested, then watching — the order
    # someone would actually want to read them in.
    order = {"current": 0, "interested": 1, "watching": 2}
    pathways.sort(key=lambda p: order.get(p["status"], 3))
    return pathways


def run_opportunity_finder_agent(
    user_id: str, target_role: str = None, model_key: str = "nova", verbose: bool = True
) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    persona = get_item("users", {"user_id": user_id})
    if not persona:
        raise ValueError(f"No persona found: {user_id}")

    saved_roles = query_by_user("saved_roles", user_id)
    if target_role:
        saved_roles = [r for r in saved_roles if r["target_role"] == target_role]
    if not saved_roles:
        result = {"user_id": user_id, "pathways": []}
        if verbose:
            print(f"\nOpportunity Finder for {user_id}: no saved roles yet — nothing to organise around.")
        return result

    opportunities = scan_all("opportunities")
    resources = scan_all("resources")
    if not opportunities:
        raise ValueError("No opportunities found — check load_data.py ran for opportunities.csv")

    system_prompt = build_system_prompt(
        [r["target_role"] for r in saved_roles],
        [o["opportunity_id"] for o in opportunities],
        [r["resource_id"] for r in resources],
    )
    user_prompt = build_user_prompt(persona, saved_roles, opportunities, resources)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    parsed = parse_agent_json(raw_output)
    pathways = filter_to_real_ids(parsed, saved_roles, opportunities, resources)

    result = {"user_id": user_id, "pathways": pathways}

    if verbose:
        print(f"\nOpportunity Finder for {user_id} (model: {model_id}):")
        print(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER004")
    parser.add_argument("--role", dest="target_role", default=None, help="just one saved pathway")
    parser.add_argument("--model", choices=["claude", "nova"], default="nova")
    args = parser.parse_args()

    run_opportunity_finder_agent(args.user_id, target_role=args.target_role, model_key=args.model)
