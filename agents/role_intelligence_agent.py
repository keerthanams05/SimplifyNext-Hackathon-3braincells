"""
Role Intelligence Agent — second step in the demo pipeline.

Takes a user_id + signal_id. Fetches that persona's own tasks and skill
gaps, plus the signal, and asks a Bedrock model to determine which of
THIS SPECIFIC PERSON's tasks and skill gaps are actually affected by
THIS SPECIFIC SIGNAL, with an explanation — matching the contract in
README.txt:

    {
      "user_id": "USER001",
      "signal_id": "SIG001",
      "affected_tasks": [...],
      "affected_skills": [...],
      "skill_gaps": [...],
      "explanation": "..."
    }

Two guardrails baked into the prompt, both learned from testing the
Signal Agent:
  1. Grounded with today's real date + told not to fact-check sources
     via its own memory (same fix as signal_agent.py).
  2. Explicitly told to ONLY pick task_id/gap_id values that actually
     appear in the lists provided — otherwise models tend to invent
     plausible-sounding IDs that don't exist in your data.

Usage:
    python agents/role_intelligence_agent.py USER001 SIG001
    python agents/role_intelligence_agent.py USER001 SIG001 --model nova
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


def build_system_prompt(valid_task_ids: list[str], valid_gap_ids: list[str]) -> str:
    return f"""You are the Role Intelligence Agent in a career-disruption-monitoring system.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access and your training data has a cutoff before today, so you
will see signals and dates from after that cutoff — that is expected, not
a sign of fabrication. Never reject or downgrade something just because you
don't personally recognize it or can't verify it independently.

You will be given ONE disruption signal and ONE person's current tasks and
skill gaps. Decide which of THIS PERSON's tasks and skill gaps are actually
affected by THIS SIGNAL, and explain why in plain language.

CRITICAL: you may ONLY use task_id values from this exact list: {valid_task_ids}
and gap_id values from this exact list: {valid_gap_ids}. Do not invent new
IDs. If none of the given tasks/gaps are affected, return empty lists rather
than making something up.

Respond with ONLY a JSON object, no other text, no markdown fences, in this
exact shape:
{{
  "user_id": "<given user_id>",
  "signal_id": "<given signal_id>",
  "affected_tasks": [<task_id strings from the allowed list only>],
  "affected_skills": [<skill_area or skill strings related to the affected tasks>],
  "skill_gaps": [<gap_id strings from the allowed list only, prioritized by relevance to this signal>],
  "explanation": "<two to three sentences explaining the reasoning, referencing the specific signal and specific tasks>"
}}"""


def build_user_prompt(signal: dict, tasks: list[dict], gaps: list[dict]) -> str:
    payload = {
        "signal": {
            "signal_id": signal.get("signal_id"),
            "title": signal.get("title"),
            "technology": signal.get("technology"),
            "sector": signal.get("sector"),
            "signal_summary": signal.get("signal_summary"),
            "evidence": signal.get("evidence"),
        },
        "persons_current_tasks": [
            {
                "task_id": t.get("task_id"),
                "task": t.get("task"),
                "skill_area": t.get("skill_area"),
                "baseline_impact": t.get("baseline_impact"),
            }
            for t in tasks
        ],
        "persons_current_skill_gaps": [
            {
                "gap_id": g.get("gap_id"),
                "skill": g.get("skill"),
                "gap_priority": g.get("gap_priority"),
            }
            for g in gaps
        ],
    }
    return json.dumps(payload, indent=2)


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 500, "temperature": 0.2},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def validate_ids(result: dict, valid_task_ids: set, valid_gap_ids: set):
    """Catches hallucinated IDs before they propagate to the Planner."""
    bad_tasks = set(result.get("affected_tasks", [])) - valid_task_ids
    bad_gaps = set(result.get("skill_gaps", [])) - valid_gap_ids
    if bad_tasks:
        print(f"  WARNING: model invented task_ids not in the real data: {bad_tasks}")
    if bad_gaps:
        print(f"  WARNING: model invented gap_ids not in the real data: {bad_gaps}")


def run_role_intelligence_agent(user_id: str, signal_id: str, model_key: str = "claude") -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    signal = get_item("signals", {"signal_id": signal_id})
    if not signal:
        raise ValueError(f"No signal found: {signal_id}")

    tasks = query_by_user("role_tasks", user_id)
    gaps = query_by_user("skill_gaps", user_id)
    if not tasks:
        raise ValueError(f"No role_tasks found for user_id={user_id}")

    valid_task_ids = [t["task_id"] for t in tasks]
    valid_gap_ids = [g["gap_id"] for g in gaps]

    system_prompt = build_system_prompt(valid_task_ids, valid_gap_ids)
    user_prompt = build_user_prompt(signal, tasks, gaps)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    validate_ids(result, set(valid_task_ids), set(valid_gap_ids))

    print(f"\nRole Intelligence verdict for {user_id} / {signal_id} (model: {model_id}):")
    print(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("signal_id", help="e.g. SIG001")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    args = parser.parse_args()

    run_role_intelligence_agent(args.user_id, args.signal_id, model_key=args.model)