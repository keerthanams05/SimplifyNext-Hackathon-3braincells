"""
Pathfinder Agent — fourth step in the demo pipeline.

Takes a user_id + signal_id. Fetches that persona's precomputed candidate
adjacent-career options, and asks a Bedrock model to rank them by relevance
to THIS SPECIFIC disruption signal (not just generic similarity), adding a
short note on why each one matters given what's happening right now.

Output is a single list of fully-merged objects — no separate ID/name
split, matching the pattern used across the other three agents:

    {
      "user_id": "USER001",
      "signal_id": "SIG001",
      "pathfinder_options": [
        {
          "target_role": "AI Application Engineer",
          "similarity_score": 82,
          "transferable_skills": "Python, APIs, backend development, ...",
          "gap_to_role": "LLM application development, AI evaluation, ...",
          "reason": "<original baseline reason from the dataset>",
          "signal_relevance_note": "<why this pivot matters given THIS signal>"
        },
        ...
      ],
      "explanation": "..."
    }

Same guardrails as the other agents: grounded with today's real date, told
not to fact-check via memory, and told to only use target_role values that
actually exist in this person's candidate list (checked afterward).

Usage:
    python agents/pathfinder_agent.py USER001 SIG001
    python agents/pathfinder_agent.py USER001 SIG001 --model nova
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
from agent_errors import AgentOutputError, MissingDataError  # noqa: E402


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


def build_system_prompt(valid_target_roles: list[str]) -> str:
    return f"""You are the Pathfinder Agent in a career-disruption-monitoring system.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access and your training data has a cutoff before today, so you
will see signals from after that cutoff — that is expected, not a sign of
fabrication. Never reject or downgrade something just because you don't
personally recognize it or can't verify it independently.

You will be given ONE disruption signal and ONE person's precomputed list
of candidate adjacent career paths (each with a baseline similarity score,
transferable skills, and skill gaps). Your job is NOT to invent new roles —
it is to judge, given THIS specific signal, which of the given roles are
now MORE or LESS urgent/relevant for this person, and explain why.

CRITICAL: you may ONLY use target_role values from this exact list:
{valid_target_roles}. Do not invent new roles.

Respond with ONLY a JSON object, no other text, no markdown fences, in this
exact shape:
{{
  "user_id": "<given user_id>",
  "signal_id": "<given signal_id>",
  "ranked_target_roles": [<target_role strings, ordered most to least relevant given this signal>],
  "signal_relevance_notes": {{
    "<target_role>": "<one sentence on why this pivot matters (or doesn't) given this specific signal>"
  }},
  "explanation": "<two to three sentences summarizing the overall pivot recommendation given this signal>"
}}"""


def build_user_prompt(signal: dict, options: list[dict]) -> str:
    payload = {
        "signal": {
            "signal_id": signal.get("signal_id"),
            "title": signal.get("title"),
            "technology": signal.get("technology"),
            "sector": signal.get("sector"),
            "signal_summary": signal.get("signal_summary"),
        },
        "persons_candidate_pivots": [
            {
                "target_role": o.get("target_role"),
                "similarity_score": o.get("similarity_score"),
                "transferable_skills": o.get("transferable_skills"),
                "gap_to_role": o.get("gap_to_role"),
                "baseline_reason": o.get("reason"),
            }
            for o in options
        ],
    }
    return json.dumps(payload, indent=2)


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 600, "temperature": 0.2},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AgentOutputError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def validate_roles(result: dict, valid_roles: set):
    bad = set(result.get("ranked_target_roles", [])) - valid_roles
    if bad:
        print(f"  WARNING: model invented target_roles not in this person's data: {bad}")


def enrich_result(result: dict, user_id: str, signal_id: str, options: list[dict]) -> dict:
    """
    Merge the model's ranking + relevance notes back onto the full original
    option records, so the final output is one self-contained list — not a
    ranked-role-name list the frontend would have to cross-reference itself.
    """
    option_lookup = {o["target_role"]: o for o in options}
    notes = result.get("signal_relevance_notes", {})

    ranked = result.get("ranked_target_roles", [])
    # Any role the model didn't rank (or invented) is dropped; anything it
    # missed from the real list is appended at the end rather than lost.
    ranked = [r for r in ranked if r in option_lookup]
    for role in option_lookup:
        if role not in ranked:
            ranked.append(role)

    merged_options = []
    for role in ranked:
        base = option_lookup[role]
        merged_options.append(
            {
                "target_role": role,
                "similarity_score": base.get("similarity_score"),
                "transferable_skills": base.get("transferable_skills"),
                "gap_to_role": base.get("gap_to_role"),
                "reason": base.get("reason"),
                "signal_relevance_note": notes.get(role, ""),
            }
        )

    result["user_id"] = user_id
    result["signal_id"] = signal_id
    result["pathfinder_options"] = merged_options
    result.pop("ranked_target_roles", None)
    result.pop("signal_relevance_notes", None)

    return result


def run_pathfinder_agent(user_id: str, signal_id: str, model_key: str = "claude", verbose: bool = True) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    signal = get_item("signals", {"signal_id": signal_id})
    if not signal:
        raise MissingDataError(f"No signal found: {signal_id}")

    options = query_by_user("pathfinder_results", user_id)
    if not options:
        # No pathfinder candidates for this person — return an empty,
        # honest result rather than asking the model to invent some.
        result = {
            "user_id": user_id,
            "signal_id": signal_id,
            "pathfinder_options": [],
            "explanation": "No candidate adjacent roles are on file for this person.",
        }
        if verbose:
            print(f"\nPathfinder verdict for {user_id} / {signal_id}: no candidates on file.")
            print(json.dumps(result, indent=2))
        return result

    valid_roles = [o["target_role"] for o in options]

    system_prompt = build_system_prompt(valid_roles)
    user_prompt = build_user_prompt(signal, options)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    validate_roles(result, set(valid_roles))
    result = enrich_result(result, user_id, signal_id, options)

    if verbose:
        print(f"\nPathfinder verdict for {user_id} / {signal_id} (model: {model_id}):")
        print(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("signal_id", help="e.g. SIG001")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    args = parser.parse_args()

    run_pathfinder_agent(args.user_id, args.signal_id, model_key=args.model)