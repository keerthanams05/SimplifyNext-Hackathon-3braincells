"""
Signal Agent — the first step in the demo pipeline.

Takes a signal_id, fetches the raw signal from DynamoDB, and asks a Bedrock
model to independently assess whether the evidence actually supports the
claim, then returns a structured verdict matching the contract in README.txt:

    {
      "signal_id": "SIG001",
      "validated": true,
      "relevance_score": 96,
      "severity": "High",
      "reason": "..."
    }

Deliberately does NOT show the model the dataset's own
expected_signal_output — that would be feeding it the answer key. That
field is only printed afterwards as a reference so you can sanity-check
your agent's judgment against it while developing.

Usage:
    python agents/signal_agent.py SIG001
    python agents/signal_agent.py SIG001 --model nova   # cheaper, for bulk testing
    python agents/signal_agent.py SIG001 --save          # writes result back to DynamoDB
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

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

SYSTEM_PROMPT = f"""You are the Signal Agent in a career-disruption-monitoring system.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access and your own training data has a cutoff well before today —
so you will be shown signals, sources, and dates from AFTER your training
cutoff. That is expected and normal, not a sign of fabrication. Do NOT reject
a signal, lower its score, or call it unverifiable just because you personally
don't recognize the source, the date is after what you know about, or you
can't independently confirm it happened. You have no way to independently
verify any external source, ever — that is not your job here.

Your actual job: given the evidence text and metadata you ARE provided
(source_credibility, frequency, affected_roles, signal_summary), judge whether
that evidence, taken at face value, logically supports the severity/impact
being claimed. Treat the given source_credibility field as accurate input,
not something to second-guess.

You will be given one reported technology/AI disruption signal, including its
evidence and source. Judge ONLY whether the evidence supports the claim being
made, given the metadata provided.

Respond with ONLY a JSON object, no other text, no markdown fences, in exactly
this shape:
{{
  "signal_id": "<the signal_id given to you>",
  "validated": true or false,
  "relevance_score": <integer 0-100, how strongly the evidence supports this being a real, significant disruption signal>,
  "severity": "Low" or "Medium" or "High",
  "reason": "<one or two sentences explaining your judgment>"
}}"""


def decimal_to_native(obj):
    """DynamoDB returns Decimal for numbers; make it JSON-serializable."""
    if isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def get_signal(signal_id: str) -> dict:
    table = dynamodb.Table(table_name("signals"))
    response = table.get_item(Key={"signal_id": signal_id})
    item = response.get("Item")
    if not item:
        raise ValueError(f"No signal found with signal_id={signal_id}")
    return decimal_to_native(item)


def build_user_prompt(signal: dict) -> str:
    # Deliberately exclude expected_signal_output / expected_role_output —
    # those are the dataset's precomputed answer key, not real evidence.
    evidence_fields = {
        "signal_id": signal.get("signal_id"),
        "title": signal.get("title"),
        "sector": signal.get("sector"),
        "technology": signal.get("technology"),
        "date": signal.get("date"),
        "source": signal.get("source"),
        "source_url": signal.get("source_url"),
        "evidence": signal.get("evidence"),
        "baseline_severity": signal.get("severity"),
        "frequency": signal.get("frequency"),
        "source_credibility": signal.get("source_credibility"),
        "affected_roles": signal.get("affected_roles"),
        "signal_summary": signal.get("signal_summary"),
    }
    return json.dumps(evidence_fields, indent=2)


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0.2},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = raw_text.strip()
    # strip ```json ... ``` fences if the model added them despite instructions
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def save_result(signal_id: str, result: dict):
    table = dynamodb.Table(table_name("signals"))
    table.update_item(
        Key={"signal_id": signal_id},
        UpdateExpression="SET agent_validated_output = :v",
        ExpressionAttributeValues={":v": json.loads(json.dumps(result), parse_float=Decimal)},
    )


def run_signal_agent(signal_id: str, model_key: str = "claude", save: bool = False) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    signal = get_signal(signal_id)
    user_prompt = build_user_prompt(signal)
    raw_output = call_model(SYSTEM_PROMPT, user_prompt, model_id)
    result = parse_agent_json(raw_output)

    print(f"\nSignal Agent verdict for {signal_id} (model: {model_id}):")
    print(json.dumps(result, indent=2))

    if "expected_signal_output" in signal:
        print("\n(dataset reference — NOT shown to the agent, for your own sanity-check only)")
        print(json.dumps(decimal_to_native(signal["expected_signal_output"]), indent=2))

    if save:
        save_result(signal_id, result)
        print(f"\nSaved agent_validated_output back to signals table for {signal_id}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("signal_id", help="e.g. SIG001")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    parser.add_argument("--save", action="store_true", help="write the result back to DynamoDB")
    args = parser.parse_args()

    run_signal_agent(args.signal_id, model_key=args.model, save=args.save)