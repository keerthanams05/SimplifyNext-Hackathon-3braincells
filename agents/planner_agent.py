"""
Planner Agent — third step in the demo pipeline.

Takes a user_id + signal_id. Runs (or reuses) the Role Intelligence Agent's
verdict for that pair, fetches the global resource catalogue, and asks a
Bedrock model to produce a personalised 30/60/90-day upskilling plan, with
each phase's resources returned as full merged objects (not bare IDs) so a
frontend can render them directly:

    {
      "user_id": "USER001",
      "signal_id": "SIG001",
      "phases": [
        {
          "phase": "Days 1-30",
          "goal": "...",
          "milestones": "...",
          "tasks": "...",
          "resources": [
            {"resource_id": "RES001", "name": "...", "type": "...", "funding": "..."}
          ],
          "hours_per_week": 6
        },
        ... (Days 31-60, Days 61-90)
      ],
      "explanation": "..."
    }

Same two guardrails as role_intelligence_agent.py:
  1. Grounded with today's real date + told not to fact-check via memory.
  2. Explicitly told to ONLY pick resource_id values that actually appear
     in the catalogue provided — otherwise models invent plausible IDs.

Usage:
    python agents/planner_agent.py USER001 SIG001
    python agents/planner_agent.py USER001 SIG001 --model nova
    python agents/planner_agent.py USER001 SIG001 --save
"""

import argparse
import json
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from config import AWS_REGION, BEDROCK_REGION, CLAUDE_MODEL_ID, NOVA_MICRO_MODEL_ID, table_name

# Reuse the Role Intelligence Agent instead of re-deriving affected
# tasks/gaps here — keeps the two agents' verdicts consistent.
from role_intelligence_agent import run_role_intelligence_agent

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)


def decimal_to_native(obj):
    if isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def scan_all(short_table):
    """resources has no per-user partition key, so we scan the whole table.
    Fine at hackathon scale (a dozen-ish rows); swap for a query if this
    table grows."""
    table = dynamodb.Table(table_name(short_table))
    items, resp = [], table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return decimal_to_native(items)


def build_system_prompt(valid_resource_ids: list[str]) -> str:
    return f"""You are the Planner Agent in a career-disruption-monitoring system.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access and your training data has a cutoff before today, so you
will see signals, tasks, and skill gaps from after that cutoff — that is
expected, not a sign of fabrication. Never reject or downgrade something
just because you don't personally recognize it or can't verify it
independently.

You will be given ONE person's Role Intelligence verdict for ONE disruption
signal: their affected tasks, affected skills, and prioritised skill gaps,
plus the explanation of why they matter. Turn this into a concrete 30/60/90
day upskilling plan with exactly three phases: "Days 1-30", "Days 31-60",
and "Days 61-90". Each later phase should build on the previous one.

CRITICAL: you may ONLY use resource_id values from this exact list:
{valid_resource_ids}. Do not invent new IDs, and do not cite a resource
whose target_roles/skills clearly don't match this person's gaps. Prefer
resources whose "skills" field overlaps with the person's skill gaps.

Respond with ONLY a JSON object, no other text, no markdown fences, in this
exact shape:
{{
  "user_id": "<given user_id>",
  "signal_id": "<given signal_id>",
  "phases": [
    {{
      "phase": "Days 1-30",
      "goal": "<one short goal statement>",
      "milestones": "<semicolon-separated milestones>",
      "tasks": "<semicolon-separated concrete tasks>",
      "resource_ids": [<resource_id strings from the allowed list only>],
      "hours_per_week": <integer>
    }},
    {{ "phase": "Days 31-60", ... same shape ... }},
    {{ "phase": "Days 61-90", ... same shape ... }}
  ],
  "explanation": "<two to three sentences on why this progression makes sense given the role intelligence verdict>"
}}"""


def build_user_prompt(verdict: dict, resources: list[dict]) -> str:
    payload = {
        "role_intelligence_verdict": verdict,
        "available_resources": [
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


def validate_resource_ids(result: dict, valid_resource_ids: set):
    """Catches hallucinated resource_ids before they propagate downstream."""
    used = set()
    for phase in result.get("phases", []):
        used.update(phase.get("resource_ids", []))
    bad = used - valid_resource_ids
    if bad:
        print(f"  WARNING: model invented resource_ids not in the catalogue: {bad}")


def enrich_phases(result: dict, resources: list[dict]) -> dict:
    """
    REPLACE each phase's bare resource_ids list with full resource objects
    looked up from our own trusted catalogue (not the model) — one merged
    object per resource, ready for a frontend to render without a second
    lookup or having to zip an ID list against a separate catalogue itself.
    """
    resource_lookup = {r["resource_id"]: r for r in resources}

    for phase in result.get("phases", []):
        phase["resources"] = [
            {
                "resource_id": rid,
                "name": resource_lookup[rid].get("name"),
                "type": resource_lookup[rid].get("type"),
                "funding": resource_lookup[rid].get("funding"),
            }
            for rid in phase.get("resource_ids", [])
            if rid in resource_lookup
        ]
        phase.pop("resource_ids", None)

    return result


def save_plan(result: dict):
    """Writes the generated plan back to DynamoDB (plan_id + user_id as a
    composite you'll need a table for — adjust to your actual schema in
    scripts/create_table.py before relying on this)."""
    table = dynamodb.Table(table_name("generated_plans"))
    plan_id = f"PLAN-{result['user_id']}-{result['signal_id']}"
    # DynamoDB rejects Python floats, and the model can return one anywhere
    # in the plan (e.g. hours_per_week: 5.5), so round-trip through JSON
    # with parse_float=Decimal — same trick as signal_agent.save_result.
    item = json.loads(json.dumps({"plan_id": plan_id, **result}), parse_float=Decimal)
    table.put_item(Item=item)
    print(f"  Saved plan {plan_id} to DynamoDB.")


def run_planner_agent(
    user_id: str, signal_id: str, model_key: str = "claude", save: bool = False, verdict: dict = None,
    verbose: bool = True,
) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    # Reuse Role Intelligence Agent's verdict rather than re-deriving
    # affected tasks/gaps here. If the caller already computed it (e.g.
    # the pipeline orchestrator, which needs it standalone too), accept it
    # directly instead of paying for a second, identical Bedrock call.
    if verdict is None:
        verdict = run_role_intelligence_agent(user_id, signal_id, model_key=model_key, verbose=verbose)

    # Short-circuit: if this signal doesn't actually affect this person,
    # don't call Bedrock to invent a plan anyway. Testing showed the model
    # will fabricate a low-effort plan even for clearly irrelevant signals
    # (e.g. a marketing-sector signal against a finance persona) if you
    # let it try — so we skip the call entirely instead of relying on the
    # model to say "no" on its own.
    if not verdict.get("affected_tasks") and not verdict.get("skill_gaps"):
        result = {
            "user_id": user_id,
            "signal_id": signal_id,
            "phases": [],
            "no_plan_reason": (
                "Role Intelligence Agent found no affected tasks or skill gaps for this "
                "person from this signal, so no upskilling plan was generated. "
                f"Underlying explanation: {verdict.get('explanation', 'none provided')}"
            ),
        }
        if verbose:
            print(f"\nPlanner verdict for {user_id} / {signal_id}: SKIPPED (signal not relevant to this person)")
            print(json.dumps(result, indent=2))
        if save:
            save_plan(result)
        return result

    # Step 2: pull the global resource catalogue.
    resources = scan_all("resources")
    if not resources:
        raise ValueError("No resources found — check scripts/load_data.py ran for resources.csv")

    valid_resource_ids = [r["resource_id"] for r in resources]

    system_prompt = build_system_prompt(valid_resource_ids)
    user_prompt = build_user_prompt(verdict, resources)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    result["user_id"] = user_id
    result["signal_id"] = signal_id
    validate_resource_ids(result, set(valid_resource_ids))
    result = enrich_phases(result, resources)

    if verbose:
        print(f"\nPlanner verdict for {user_id} / {signal_id} (model: {model_id}):")
        print(json.dumps(result, indent=2))

    if save:
        save_plan(result)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("signal_id", help="e.g. SIG001")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    parser.add_argument("--save", action="store_true", help="write the result back to DynamoDB")
    args = parser.parse_args()

    run_planner_agent(args.user_id, args.signal_id, model_key=args.model, save=args.save)