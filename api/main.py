"""
Minimal local API wrapping the 4-agent pipeline, for the frontend to call.

This runs locally (no Lambda/API Gateway needed yet) — free, fast to
iterate on, and easy to swap for a real Lambda handler later without
touching the actual pipeline logic (see run_full_pipeline in
agents/pipeline.py, which this just calls).

Usage:
    uvicorn api.main:app --reload --port 8000

Then open frontend/index.html in a browser (it calls http://localhost:8000).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key

from config import AWS_REGION, table_name
from pipeline import run_full_pipeline
from progress_agent import run_progress_agent
from planner_agent import run_planner_agent

app = FastAPI(title="CareerGuardian API")


def decimal_to_native(obj):
    if isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj

# Allows the local static frontend (opened as a file:// page or a
# different localhost port) to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


def scan_all(short_table):
    table = dynamodb.Table(table_name(short_table))
    items, resp = [], table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


@app.get("/api/personas")
def list_personas():
    """For a dropdown: every persona with their name/role, not just user_id."""
    users = scan_all("users")
    return [
        {"user_id": u.get("user_id"), "name": u.get("name"), "occupation": u.get("occupation")}
        for u in users
    ]


@app.get("/api/signals")
def list_signals():
    """For a dropdown: every signal with its title, not just signal_id."""
    signals = scan_all("signals")
    return [
        {"signal_id": s.get("signal_id"), "title": s.get("title"), "sector": s.get("sector")}
        for s in signals
    ]


@app.get("/api/profile")
def get_profile(user_id: str):
    """Full profile for the 'logged in as Sarah' screen: persona details + skills."""
    table = dynamodb.Table(table_name("users"))
    persona = table.get_item(Key={"user_id": user_id}).get("Item")
    if not persona:
        raise HTTPException(status_code=404, detail=f"No persona found: {user_id}")

    skills_table = dynamodb.Table(table_name("user_skills"))
    skills = skills_table.query(KeyConditionExpression=Key("user_id").eq(user_id)).get("Items", [])

    return decimal_to_native({
        "user_id": persona.get("user_id"),
        "name": persona.get("name"),
        "age": persona.get("age"),
        "occupation": persona.get("occupation"),
        "industry": persona.get("industry"),
        "years_experience": persona.get("years_experience"),
        "education": persona.get("education"),
        "career_goal": persona.get("career_goal"),
        "skills": [{"skill": s.get("skill"), "proficiency": s.get("proficiency")} for s in skills],
    })


@app.get("/api/alerts")
def get_alerts(user_id: str):
    """
    Lightweight dashboard feed: which signals are relevant to this person's
    role, WITHOUT running the full (expensive, slow) agent pipeline just to
    build a list. This is plain data matching against affected_roles — the
    real agentic reasoning only runs once the user clicks into one specific
    alert, via /api/pipeline.
    """
    users_table = dynamodb.Table(table_name("users"))
    persona = users_table.get_item(Key={"user_id": user_id}).get("Item")
    if not persona:
        raise HTTPException(status_code=404, detail=f"No persona found: {user_id}")

    occupation = persona.get("occupation")
    signals = scan_all("signals")

    matching = [
        {
            "signal_id": s.get("signal_id"),
            "title": s.get("title"),
            "sector": s.get("sector"),
            "severity": s.get("severity"),
            "date": s.get("date"),
        }
        for s in signals
        if occupation in (s.get("affected_roles") or [])
    ]

    return {"user_id": user_id, "occupation": occupation, "alerts": matching}


@app.get("/api/pipeline")
def get_pipeline_result(user_id: str, signal_id: str, model: str = "claude"):
    """The main endpoint: runs all 4 agents for one persona + signal."""
    try:
        return run_full_pipeline(user_id, signal_id, model_key=model)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {e}")


@app.get("/api/progress")
def get_progress(user_id: str, signal_id: str = None, model: str = "nova"):
    """Standalone endpoint: how is this person doing against their plan,
    and does it look like they need a re-plan? Cheap/fast — no dependency
    on the full 4-agent pipeline, so this is safe to call on every
    dashboard load rather than only when the user clicks in."""
    try:
        return run_progress_agent(user_id, signal_id=signal_id, model_key=model, verbose=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Progress agent error: {e}")


@app.get("/api/replan")
def replan(user_id: str, signal_id: str, model: str = "claude"):
    """
    Closes the loop between the Progress Agent and the Planner: checks
    whether this person is behind, and if so, generates a FRESH plan
    (verdict=None forces the Planner to re-derive the Role Intelligence
    verdict rather than reuse a stale cached one — the point of a re-plan
    is that circumstances may have changed since the original plan).

    If they're not behind, this deliberately does NOT call Bedrock again
    for a plan they don't need — same "don't call the model just to
    confirm nothing changed" philosophy as the Planner's own short-circuit
    for irrelevant signals.
    """
    try:
        progress = run_progress_agent(user_id, signal_id=signal_id, model_key="nova", verbose=False)
        if not progress.get("needs_replan"):
            return {"needs_replan": False, "progress": progress, "plan": None}

        new_plan = run_planner_agent(user_id, signal_id, model_key=model, verdict=None, verbose=False)
        return {"needs_replan": True, "progress": progress, "plan": new_plan}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Replan error: {e}")