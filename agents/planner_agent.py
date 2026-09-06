"""
Planner Agent — third step in the demo pipeline.

Takes a user_id + signal_id. Runs (or reuses) the Role Intelligence Agent's
verdict for that pair, fetches the global resource catalogue, and asks a
Bedrock model to produce a personalised 30/60/90-day upskilling plan.

Important behaviour:
  1. If Role Intelligence finds no affected tasks, skills, or skill gaps,
     the Planner short-circuits without making a second Bedrock call and
     returns an empty plan with a no_plan_reason.
  2. Resource IDs are allow-listed from the actual DynamoDB resources table
     and invalid IDs are rejected before the result is returned or saved.
  3. The Planner uses the model tier configured for the planner in
     AGENT_MODEL_TIER when no --model override is supplied.

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


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from config import (  # noqa: E402
    AWS_REGION,
    BEDROCK_REGION,
    CLAUDE_MODEL_ID,
    NOVA_MICRO_MODEL_ID,
    table_name,
)
from aws_clients import dynamodb, bedrock  # noqa: E402  thread-safe shared handles
from agent_errors import AgentOutputError, MissingDataError  # noqa: E402

try:  # Keep compatibility with older config.py files.
    from config import AGENT_MODEL_TIER  # type: ignore  # noqa: E402
except ImportError:  # pragma: no cover - compatibility fallback
    AGENT_MODEL_TIER = {}

# Reuse the Role Intelligence Agent instead of re-deriving affected
# tasks/gaps here — keeps the two agents' verdicts consistent.
from role_intelligence_agent import run_role_intelligence_agent  # noqa: E402


EXPECTED_PHASES = ["Days 1-30", "Days 31-60", "Days 61-90"]
MODEL_IDS = {
    "claude": CLAUDE_MODEL_ID,
    "nova": NOVA_MICRO_MODEL_ID,
}


def decimal_to_native(obj):
    if isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def scan_all(short_table):
    """Scan a global DynamoDB table and return all items."""
    table = dynamodb.Table(table_name(short_table))
    items, resp = [], table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return decimal_to_native(items)


def resolve_model_key(model_key: str | None = None) -> str:
    """Resolve an explicit CLI model or the configured Planner model tier."""
    if model_key is not None:
        if model_key not in MODEL_IDS:
            raise ValueError(
                f"Unsupported planner model {model_key!r}; expected one of {sorted(MODEL_IDS)}."
            )
        return model_key

    configured = AGENT_MODEL_TIER.get("planner") if isinstance(AGENT_MODEL_TIER, dict) else None
    if configured in MODEL_IDS:
        return configured

    # Also accept a direct model ID in AGENT_MODEL_TIER["planner"].
    for key, model_id in MODEL_IDS.items():
        if configured == model_id:
            return key

    # Preserve compatibility if the config predates AGENT_MODEL_TIER.
    return "claude"


def has_role_impact(verdict: dict) -> bool:
    """Return True only when Role Intelligence found something to plan for."""
    return any(
        bool(verdict.get(field))
        for field in (
            "affected_tasks",
            "affected_skills",
            "skill_gaps",
            "prioritised_skill_gaps",
            "prioritized_skill_gaps",
        )
    )


def build_no_plan_result(user_id: str, signal_id: str, verdict: dict) -> dict:
    """Build the documented no-plan response without calling Bedrock."""
    explanation = verdict.get("explanation")
    reason = (
        "The Role Intelligence Agent found no affected tasks, skills, or skill "
        "gaps for this signal and user, so no upskilling plan is needed."
    )
    if explanation:
        reason += f" Underlying explanation: {explanation}"

    return {
        "user_id": user_id,
        "signal_id": signal_id,
        "phases": [],
        "no_plan_reason": reason,
    }


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
    """Parse the model response and reject anything other than a JSON object."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Be tolerant of a single accidental leading/trailing fence without
    # accepting arbitrary prose around the JSON object.
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise AgentOutputError(
            f"Model did not return valid JSON.\nRaw output:\n{raw_text}"
        ) from e
    if not isinstance(result, dict):
        raise AgentOutputError("Model output must be a JSON object.")
    return result


def validate_plan_shape(result: dict):
    """Reject malformed plans before they can be returned or persisted."""
    phases = result.get("phases")
    if not isinstance(phases, list) or len(phases) != 3:
        raise AgentOutputError("Planner output must contain exactly three phases.")

    for expected_phase, phase in zip(EXPECTED_PHASES, phases):
        if not isinstance(phase, dict):
            raise AgentOutputError(f"Planner phase {expected_phase!r} must be an object.")
        if phase.get("phase") != expected_phase:
            raise AgentOutputError(
                f"Planner phases must be ordered exactly as {EXPECTED_PHASES}; "
                f"got {phase.get('phase')!r}."
            )

        for field in ("goal", "milestones", "tasks"):
            if not isinstance(phase.get(field), str) or not phase[field].strip():
                raise AgentOutputError(
                    f"Planner phase {expected_phase!r} requires a non-empty {field!r}."
                )

        resource_ids = phase.get("resource_ids")
        if not isinstance(resource_ids, list) or not all(
            isinstance(resource_id, str) and resource_id.strip()
            for resource_id in resource_ids
        ):
            raise AgentOutputError(
                f"Planner phase {expected_phase!r} requires resource_ids as a list of non-empty strings."
            )

        hours = phase.get("hours_per_week")
        if isinstance(hours, bool) or not isinstance(hours, int) or hours < 0:
            raise AgentOutputError(
                f"Planner phase {expected_phase!r} requires a non-negative integer hours_per_week."
            )

    explanation = result.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise AgentOutputError("Planner output requires a non-empty explanation.")


def validate_resource_catalogue(resources: list[dict]) -> set[str]:
    """Validate the trusted resource catalogue and return its ID allow-list."""
    resource_ids = [r.get("resource_id") for r in resources]
    if any(not isinstance(resource_id, str) or not resource_id.strip() for resource_id in resource_ids):
        raise ValueError("Resources catalogue contains a missing or invalid resource_id.")

    valid_resource_ids = set(resource_ids)
    if len(valid_resource_ids) != len(resource_ids):
        raise ValueError("Resources catalogue contains duplicate resource_id values.")

    return valid_resource_ids


def validate_resource_ids(result: dict, valid_resource_ids: set[str]):
    """Reject hallucinated resource_ids before they propagate downstream."""
    used = {
        resource_id
        for phase in result["phases"]
        for resource_id in phase["resource_ids"]
    }
    bad = used - valid_resource_ids
    if bad:
        raise AgentOutputError(
            "Planner returned resource_id values that do not exist in the "
            f"resource catalogue: {sorted(bad)}"
        )


def split_points(text: str) -> list[str]:
    """The model returns milestones and tasks as one semicolon-separated
    string, matching the shape of plans_30_60_90.csv. That renders as a wall
    of text, so split it into the steps it already is."""
    if not isinstance(text, str):
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def enrich_phases(result: dict, resources: list[dict]) -> dict:
    """
    Turn each phase's bare resource_ids into the real records, and its
    semicolon strings into lists — same convention as the other agents:
    hand the frontend something self-contained rather than IDs it has to
    cross-reference and prose it has to parse.

    `resource_ids` is kept as-is so nothing downstream that already reads
    it breaks.
    """
    lookup = {r["resource_id"]: r for r in resources}
    for phase in result.get("phases", []):
        phase["task_steps"] = split_points(phase.get("tasks"))
        phase["milestone_steps"] = split_points(phase.get("milestones"))
        phase["resources"] = [
            {
                "resource_id": rid,
                "name": lookup[rid].get("name"),
                "provider": lookup[rid].get("provider"),
                "type": lookup[rid].get("type"),
                "funding": lookup[rid].get("funding"),
                "url": lookup[rid].get("url"),
            }
            for rid in phase.get("resource_ids", [])
            if rid in lookup
        ]
    return result


def save_plan(result: dict):
    """Persist a generated plan to the generated_plans table.

    Deliberately NOT the `plans` table: that one holds the gold-standard
    30/60/90 plans seeded from plans_30_60_90.csv, which the demo compares
    generated output against — mixing the two would spoil the comparison.
    `generated_plans` is also where progress_agent.get_saved_plan() looks,
    so writing anywhere else silently leaves the Progress Agent with no
    plan context.
    """
    table = dynamodb.Table(table_name("generated_plans"))
    plan_id = f"PLAN-{result['user_id']}-{result['signal_id']}"
    # DynamoDB rejects Python floats, and the model can return one anywhere
    # in the plan (e.g. hours_per_week: 5.5), so round-trip through JSON
    # with parse_float=Decimal — same trick as signal_agent.save_result.
    item = json.loads(json.dumps({"plan_id": plan_id, **result}), parse_float=Decimal)
    table.put_item(Item=item)
    print(f"  Saved plan {plan_id} to DynamoDB.")


def run_planner_agent(
    user_id: str,
    signal_id: str,
    model_key: str | None = None,
    save: bool = False,
    verdict: dict | None = None,
    verbose: bool = True,
    shortlist_resource_ids: list[str] | None = None,
) -> dict:
    """Run the Planner Agent for one user/signal pair."""
    model_key = resolve_model_key(model_key)
    model_id = MODEL_IDS[model_key]

    # Step 1: reuse an already-computed verdict when the caller has one;
    # otherwise compute it once via Role Intelligence.
    if verdict is None:
        verdict = run_role_intelligence_agent(
            user_id,
            signal_id,
            model_key=model_key,
            verbose=verbose,
        )

    # Step 2: do not spend another Bedrock call when the signal does not
    # actually affect this person.
    if not has_role_impact(verdict):
        result = build_no_plan_result(user_id, signal_id, verdict)
        if verbose:
            print(f"\nPlanner verdict for {user_id} / {signal_id}: no plan required")
            print(json.dumps(result, indent=2))
        if save:
            save_plan(result)
        return result

    # Step 3: pull the global resource catalogue.
    resources = scan_all("resources")
    if not resources:
        raise MissingDataError("The resources table is empty. Run: python scripts/load_data.py")

    # If the Resource Connector Agent already matched programmes to this
    # person's gaps, plan against that shortlist instead of the whole
    # catalogue — a smaller, pre-filtered prompt that already respects
    # their budget and weekly hours. Falls back to the full catalogue if
    # the shortlist is empty, so the Planner still works standalone.
    #
    # Narrowing happens BEFORE validation on purpose: the allow-list the
    # model is held to then becomes the shortlist, so it can't reach past
    # what the Resource Connector approved.
    if shortlist_resource_ids:
        shortlisted = [r for r in resources if r["resource_id"] in set(shortlist_resource_ids)]
        if shortlisted:
            resources = shortlisted

    valid_resource_ids = validate_resource_catalogue(resources)

    # Keep prompt order stable for reproducible debugging/logging.
    ordered_resource_ids = [r["resource_id"] for r in resources]
    system_prompt = build_system_prompt(ordered_resource_ids)
    user_prompt = build_user_prompt(verdict, resources)

    # Step 4: generate and validate the plan before returning or saving it.
    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    result["user_id"] = user_id
    result["signal_id"] = signal_id

    validate_plan_shape(result)
    validate_resource_ids(result, valid_resource_ids)
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
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_IDS),
        default=None,
        help="override the Planner model configured in AGENT_MODEL_TIER",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="write the result back to the plans DynamoDB table",
    )
    args = parser.parse_args()

    run_planner_agent(
        args.user_id,
        args.signal_id,
        model_key=args.model,
        save=args.save,
    )
