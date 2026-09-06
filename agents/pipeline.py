"""
Pipeline orchestrator — runs all four agents for one user_id + signal_id
and returns ONE consolidated result. This is what the API layer calls;
nothing outside this file should need to know there are four separate
agent scripts underneath.

Usage:
    python agents/pipeline.py USER001 SIG001
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from signal_agent import run_signal_agent
from role_intelligence_agent import run_role_intelligence_agent
from planner_agent import run_planner_agent
from pathfinder_agent import run_pathfinder_agent


def run_full_pipeline(user_id: str, signal_id: str, model_key: str = "claude") -> dict:
    # verbose=False on every sub-agent call: each one would otherwise print
    # its own verdict, which is useful when running that agent standalone
    # but just duplicates output here — the pipeline prints ONE consolidated
    # result at the end instead. Hallucination WARNING prints (inside each
    # agent's validate_* function) are unconditional and still show up
    # regardless of verbose, since those matter even in pipeline mode.
    signal_verdict = run_signal_agent(signal_id, model_key=model_key, verbose=False)
    role_verdict = run_role_intelligence_agent(user_id, signal_id, model_key=model_key, verbose=False)
    # Pass the already-computed verdict through so the Planner doesn't
    # make a second, identical Bedrock call for the same thing.
    plan = run_planner_agent(user_id, signal_id, model_key=model_key, verdict=role_verdict, verbose=False)
    pathfinder = run_pathfinder_agent(user_id, signal_id, model_key=model_key, verbose=False)

    return {
        "user_id": user_id,
        "signal_id": signal_id,
        "signal": signal_verdict,
        "role_intelligence": role_verdict,
        "plan": plan,
        "pathfinder": pathfinder,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id")
    parser.add_argument("signal_id")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    args = parser.parse_args()

    result = run_full_pipeline(args.user_id, args.signal_id, model_key=args.model)
    print("\n\n=== FULL PIPELINE RESULT ===")
    print(json.dumps(result, indent=2))