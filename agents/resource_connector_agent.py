"""
Resource Connector Agent — matches skill gaps to real SkillsFuture/WSG
programmes, so the Planner doesn't have to.

Takes a user_id (and optionally the skill gaps the Role Intelligence Agent
just flagged, so it can narrow to the gaps this signal actually raised).
Pulls the person's learning constraints (budget, weekly hours, preferred
course type) and the full resources catalogue, and asks a Bedrock model to
pick the best-fitting programmes for each gap:

    {
      "user_id": "USER001",
      "matches": [
        {
          "gap_id": "SKG001",
          "skill": "AI-Assisted Software Development",
          "gap_priority": "High",
          "resources": [
            {"resource_id": "RES002", "name": "...", "type": "...", "funding": "...", "url": "..."}
          ],
          "match_reason": "..."
        }
      ],
      "shortlist_resource_ids": ["RES002", "RES005"]
    }

This is the "resource_connector" tier in scripts/config.py's
AGENT_MODEL_TIER — structured matching against a known list, closer to
routing than open-ended reasoning — so it defaults to Nova Micro. Running
it before the Planner also means the Planner sees a short, relevant
shortlist instead of the entire catalogue, which keeps the expensive
Claude call smaller.

Usage:
    python agents/resource_connector_agent.py USER001
    python agents/resource_connector_agent.py USER001 --model claude
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
from aws_clients import dynamodb, bedrock  # thread-safe shared handles


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


def build_system_prompt(valid_resource_ids: list[str], valid_gap_ids: list[str]) -> str:
    return f"""You are the Resource Connector Agent in a career-disruption-monitoring
system for Singapore workers.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access — never reject or downgrade a programme just because you
don't personally recognize it. Treat the catalogue you are given as
accurate and current.

You will be given ONE person's learning constraints (budget, weekly study
hours, preferred course type and learning style) and their prioritised
skill gaps, plus a catalogue of real SkillsFuture / Workforce Singapore
programmes. For each skill gap, pick the 1-2 catalogue entries that best
close it for THIS person.

Matching rules:
- Respect their constraints. A "Low" budget person should be matched to
  subsidised or credit-claimable options before unfunded ones; someone
  with few weekly hours should not be matched to a full-time programme.
- A skills framework or insights report is reference material, not
  training — only match one when the gap is genuinely about understanding
  a career landscape, not building a skill.
- If nothing in the catalogue genuinely fits a gap, return an empty
  resources list for it. Do not force a match.

CRITICAL: you may ONLY use resource_id values from this exact list:
{valid_resource_ids}
and gap_id values from this exact list:
{valid_gap_ids}
Do not invent new IDs.

Respond with ONLY a JSON object, no other text, no markdown fences, in this
exact shape:
{{
  "matches": [
    {{
      "gap_id": "<gap_id from the allowed list>",
      "resource_ids": [<resource_id strings from the allowed list, best first, at most 2>],
      "match_reason": "<one sentence on why these fit this person's constraints>"
    }}
  ]
}}"""


def build_user_prompt(persona: dict, gaps: list[dict], resources: list[dict]) -> str:
    payload = {
        "persons_constraints": {
            "occupation": persona.get("occupation"),
            "budget": persona.get("budget"),
            "weekly_learning_hours": persona.get("weekly_learning_hours"),
            "preferred_course_type": persona.get("preferred_course_type"),
            "preferred_learning_style": persona.get("preferred_learning_style"),
            "timeline_months": persona.get("timeline_months"),
        },
        "skill_gaps": [
            {
                "gap_id": g.get("gap_id"),
                "skill": g.get("skill"),
                "gap_priority": g.get("gap_priority"),
                "why_it_matters": g.get("why_it_matters"),
            }
            for g in gaps
        ],
        "resource_catalogue": [
            {
                "resource_id": r.get("resource_id"),
                "name": r.get("name"),
                "provider": r.get("provider"),
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
        inferenceConfig={"maxTokens": 700, "temperature": 0.2},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def validate_ids(result: dict, valid_resource_ids: set, valid_gap_ids: set):
    """Catches hallucinated IDs before they reach the Planner."""
    bad_gaps, bad_resources = set(), set()
    for match in result.get("matches", []):
        if match.get("gap_id") not in valid_gap_ids:
            bad_gaps.add(match.get("gap_id"))
        bad_resources |= set(match.get("resource_ids", [])) - valid_resource_ids
    if bad_gaps:
        print(f"  WARNING: model invented gap_ids not in the real data: {bad_gaps}")
    if bad_resources:
        print(f"  WARNING: model invented resource_ids not in the catalogue: {bad_resources}")


def enrich_result(result: dict, user_id: str, gaps: list[dict], resources: list[dict]) -> dict:
    """
    Replace the model's bare ID pairs with the real, trusted records from
    our own tables — one self-contained list the Planner (or a frontend)
    can use directly, plus a flat shortlist of resource_ids for the
    Planner's allow-list.
    """
    gap_lookup = {g["gap_id"]: g for g in gaps}
    resource_lookup = {r["resource_id"]: r for r in resources}

    matches, shortlist = [], []
    for match in result.get("matches", []):
        gap_id = match.get("gap_id")
        if gap_id not in gap_lookup:
            continue
        gap = gap_lookup[gap_id]
        matched_resources = []
        for rid in match.get("resource_ids", []):
            if rid not in resource_lookup:
                continue
            r = resource_lookup[rid]
            matched_resources.append(
                {
                    "resource_id": rid,
                    "name": r.get("name"),
                    "provider": r.get("provider"),
                    "type": r.get("type"),
                    "funding": r.get("funding"),
                    "url": r.get("url"),
                }
            )
            if rid not in shortlist:
                shortlist.append(rid)

        matches.append(
            {
                "gap_id": gap_id,
                "skill": gap.get("skill"),
                "gap_priority": gap.get("gap_priority"),
                "resources": matched_resources,
                "match_reason": match.get("match_reason", ""),
            }
        )

    return {
        "user_id": user_id,
        "matches": matches,
        "shortlist_resource_ids": shortlist,
    }


def run_resource_connector_agent(
    user_id: str, gaps: list[dict] = None, model_key: str = "nova", verbose: bool = True
) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    persona = get_item("users", {"user_id": user_id})
    if not persona:
        raise ValueError(f"No persona found: {user_id}")

    # Caller (e.g. the pipeline) can pass the gaps the Role Intelligence
    # Agent just flagged, so we only match what this signal actually
    # raised. Otherwise fall back to every gap on file.
    if not gaps:
        gaps = query_by_user("skill_gaps", user_id)
    if not gaps:
        result = {"user_id": user_id, "matches": [], "shortlist_resource_ids": []}
        if verbose:
            print(f"\nResource Connector for {user_id}: no skill gaps on file, nothing to match.")
        return result

    resources = scan_all("resources")
    if not resources:
        raise ValueError("No resources found — check scripts/load_data.py ran for resources.csv")

    valid_gap_ids = [g["gap_id"] for g in gaps]
    valid_resource_ids = [r["resource_id"] for r in resources]

    system_prompt = build_system_prompt(valid_resource_ids, valid_gap_ids)
    user_prompt = build_user_prompt(persona, gaps, resources)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    validate_ids(result, set(valid_resource_ids), set(valid_gap_ids))
    result = enrich_result(result, user_id, gaps, resources)

    if verbose:
        print(f"\nResource Connector matches for {user_id} (model: {model_id}):")
        print(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("--model", choices=["claude", "nova"], default="nova")
    args = parser.parse_args()

    run_resource_connector_agent(args.user_id, model_key=args.model)
