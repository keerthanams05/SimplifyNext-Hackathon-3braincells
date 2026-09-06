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
from pydantic import BaseModel
from datetime import date
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key

from config import AWS_REGION, AGENT_MODEL_TIER, table_name
from agent_errors import AgentOutputError, MissingDataError
from pipeline import run_full_pipeline
from progress_agent import run_progress_agent
from planner_agent import run_planner_agent
from resource_connector_agent import run_resource_connector_agent
from explainer_agent import run_explainer_agent
from opportunity_finder_agent import run_opportunity_finder_agent
from prep_agent import run_prep_agent
from market_watcher_agent import run_market_watcher, build_watch_list

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


AGENT_DIRECTORY = [
    {
        "key": "market_watcher",
        "name": "Watcher",
        "asks": "Has anything changed since yesterday?",
        "does": "Scans news feeds every morning for the roles you're in or curious about.",
        "in_full": (
            "Runs daily, before you open the app. It builds its watch list from the roles "
            "you've saved — the one you're in now plus anything you've starred — and reads "
            "industry feeds looking for changes to how that work is done. It throws out "
            "funding rounds and product launches, keeps the things that change someone's "
            "actual day, and hands them to the Signal Checker. It never decides something "
            "is true; it only decides something is worth a second look."
        ),
        "reads": ["your saved roles", "news feeds"],
        "hands_to": "signal_agent",
        "trigger": "Every day, automatically",
    },
    {
        "key": "signal_agent",
        "name": "Signal Checker",
        "asks": "Is this real, and how big a deal is it?",
        "does": "Reads the evidence behind a story and decides whether it's solid enough to act on.",
        "in_full": (
            "Before anything reaches you, this one checks the homework. It looks at who "
            "reported it, what evidence they actually have, and how often the same thing "
            "is being reported elsewhere — then scores how strongly the evidence supports "
            "the claim. Weak stories stop here. It's deliberately never shown the "
            "dataset's own expected answer, so its judgment is its own."
        ),
        "reads": ["the story and its source"],
        "hands_to": "role_intelligence",
        "trigger": "When the Watcher finds something",
    },
    {
        "key": "role_intelligence",
        "name": "Role Reader",
        "asks": "Which parts of your week does this actually touch?",
        "does": "Maps a validated story onto your real tasks — not your job title.",
        "in_full": (
            "This is the one that makes the whole thing personal. It has your actual list "
            "of tasks, and it works out which of THOSE change because of this story, and "
            "why. Two people with the same job title get different answers here, because "
            "they do different things all day. If a story doesn't touch your work, it says "
            "so and the chain stops — nobody downstream invents a plan you don't need."
        ),
        "reads": ["your tasks", "your skills", "your skill gaps"],
        "hands_to": "resource_connector",
        "trigger": "After the Signal Checker validates something",
    },
    {
        "key": "resource_connector",
        "name": "Course Finder",
        "asks": "What could you actually sign up for?",
        "does": "Matches each gap to real SkillsFuture and WSG courses that fit your budget and hours.",
        "in_full": (
            "It takes the gaps the Role Reader found and searches the course catalogue "
            "against your real constraints — what you can spend, how many hours a week you "
            "genuinely have, whether you want evening classes or self-paced. It picks one "
            "or two per gap and shows you the choices rather than deciding for you. If "
            "nothing in the catalogue genuinely fits a gap, it says nothing rather than "
            "padding the list."
        ),
        "reads": ["your gaps", "your budget and hours", "the course catalogue"],
        "hands_to": "planner",
        "trigger": "After the Role Reader finds gaps",
    },
    {
        "key": "planner",
        "name": "Plan Builder",
        "asks": "What do you do first, second, third?",
        "does": "Turns the courses you picked into a 30/60/90-day plan built around your week.",
        "in_full": (
            "It sequences things: what makes sense in the first month, what that unlocks "
            "in the second, what you'd be ready for by the third. It plans against the "
            "hours you actually have, not an ideal week. You can swap any phase's focus or "
            "swap a course out and it rebuilds around your choice — the plan is a draft "
            "you edit, not a verdict you receive."
        ),
        "reads": ["your chosen courses", "your gaps", "your weekly hours"],
        "hands_to": "prep_coach",
        "trigger": "After you pick your courses",
    },
    {
        "key": "pathfinder",
        "name": "Path Scout",
        "asks": "Where else could you go from here?",
        "does": "Scores the roles your current experience already transfers into.",
        "in_full": (
            "It looks at everything you can already do and finds roles that are closer "
            "than you'd think — scoring each on how much of your experience carries over "
            "and naming exactly what's missing. When something changes in your industry, "
            "it re-ranks them, because a shift that makes one path harder often makes "
            "another one more valuable. Star any of them and the Watcher starts tracking "
            "that role for you too."
        ),
        "reads": ["your skills", "your experience", "adjacent roles"],
        "hands_to": "opportunity_finder",
        "trigger": "Alongside the plan, and whenever you star a role",
    },
    {
        "key": "opportunity_finder",
        "name": "Opportunity Finder",
        "asks": "Where do you actually go to get in?",
        "does": "Collects the job boards, communities and events for each path you're following.",
        "in_full": (
            "For every role you've saved, it gathers the real places that matter: where "
            "those jobs get posted, where the people already doing it gather, and what's "
            "worth turning up to — a hackathon if you're heading into tech, an industry "
            "body if you're heading into finance. Every link comes from a checked "
            "catalogue; it isn't allowed to write a web address itself, so nothing it "
            "shows you can be a dead link it made up."
        ),
        "reads": ["your saved roles", "the SG opportunity catalogue"],
        "hands_to": "prep_coach",
        "trigger": "Whenever your saved roles change",
    },
    {
        "key": "prep_coach",
        "name": "Prep Coach",
        "asks": "Are you ready to go for it?",
        "does": "Rewrites your real experience into résumé lines and preps you for the questions.",
        "in_full": (
            "The one that helps you actually apply. It turns the tasks you already do into "
            "résumé lines aimed at the role you want, gives you the questions you'll really "
            "be asked — including the awkward one about why you're switching — and tells "
            "you honestly how ready you are. It will never invent experience you don't "
            "have, and it won't coach you to hide a gap; it gives you language for what "
            "you're doing about it instead."
        ),
        "reads": ["your tasks", "your skills", "your progress", "your target role"],
        "hands_to": "explainer",
        "trigger": "When you're getting close to applying",
    },
    {
        "key": "replanning_trigger",
        "name": "Progress Coach",
        "asks": "Is this plan still working for you?",
        "does": "Checks in weekly and rebuilds the plan when life gets in the way.",
        "in_full": (
            "Three taps once a week. It knows the difference between 'not finished yet' "
            "and genuinely stuck, and when enough has slipped it asks the Plan Builder for "
            "a new plan rather than leaving you quietly falling behind a schedule that "
            "stopped being realistic. Falling behind changes the plan, not your standing."
        ),
        "reads": ["your weekly check-ins", "your current plan"],
        "hands_to": "planner",
        "trigger": "Weekly, and when you update a task",
    },
    {
        "key": "explainer",
        "name": "Translator",
        "asks": "What does all this mean, in normal words?",
        "does": "Rewrites everything above as plain language, task-first and never alarmist.",
        "in_full": (
            "The only one writing for you rather than for the system. It takes all the "
            "scores and IDs and turns them into a few honest sentences: what's changing, "
            "what it means for your Tuesday, what to do first. It is explicitly forbidden "
            "from talking about your job being at risk or putting odds on your employment "
            "— it talks about tasks changing, because that's the thing that's actually "
            "true and the thing you can act on."
        ),
        "reads": ["everything the others produced"],
        "hands_to": None,
        "trigger": "Last, before anything reaches you",
    },
]


@app.get("/api/agents")
def list_agents():
    """The agent directory the UI uses to show who is working on your case,
    what each one is for, and which agent it hands to next. Deliberately
    does NOT expose which model each runs on — that's an implementation
    detail of ours, not something a person needs to see."""
    return AGENT_DIRECTORY


@app.get("/api/pipeline")
def get_pipeline_result(
    user_id: str, signal_id: str, model: str = "claude", explain: bool = True, parallel: bool = True
):
    """The main endpoint: runs every agent for one persona + signal.

    The three agents that don't depend on each other run concurrently, so
    this is roughly a third faster than it looks on paper. The response
    carries a `timings` block saying where the time actually went — pass
    parallel=false to compare against the old one-at-a-time behaviour."""
    try:
        return run_full_pipeline(user_id, signal_id, model_key=model, explain=explain, parallel=parallel)
    except MissingDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentOutputError as e:
        # The data is fine; Bedrock returned something unusable. Calling this
        # a 404 sent people hunting for missing records that were never the
        # problem.
        raise HTTPException(status_code=502, detail=f"The model returned something we couldn't use: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {type(e).__name__}: {e}")


@app.get("/api/resources")
def get_resource_matches(user_id: str, model: str = "nova"):
    """Standalone Course Finder: match this person's gaps to programmes,
    without running the whole pipeline. Cheap tier, so it's fine to call
    from a browse screen."""
    try:
        return run_resource_connector_agent(user_id, model_key=model, verbose=False)
    except MissingDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentOutputError as e:
        # The data is fine; Bedrock returned something unusable. Calling this
        # a 404 sent people hunting for missing records that were never the
        # problem.
        raise HTTPException(status_code=502, detail=f"The model returned something we couldn't use: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resource connector error: {type(e).__name__}: {e}")


@app.get("/api/explain")
def explain(user_id: str, signal_id: str, model: str = "claude"):
    """Plain-language version only. Runs the pipeline behind the scenes,
    so prefer /api/pipeline?explain=true if you also need the data."""
    try:
        return run_explainer_agent(user_id, signal_id, model_key=model, verbose=False)
    except MissingDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentOutputError as e:
        # The data is fine; Bedrock returned something unusable. Calling this
        # a 404 sent people hunting for missing records that were never the
        # problem.
        raise HTTPException(status_code=502, detail=f"The model returned something we couldn't use: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Explainer error: {type(e).__name__}: {e}")


@app.get("/api/progress")
def get_progress(user_id: str, signal_id: str = None, model: str = "nova"):
    """Standalone endpoint: how is this person doing against their plan,
    and does it look like they need a re-plan? Cheap/fast — no dependency
    on the full 4-agent pipeline, so this is safe to call on every
    dashboard load rather than only when the user clicks in."""
    try:
        return run_progress_agent(user_id, signal_id=signal_id, model_key=model, verbose=False)
    except MissingDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentOutputError as e:
        raise HTTPException(status_code=502, detail=f"The model returned something we couldn't use: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Progress agent error: {type(e).__name__}: {e}")


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
    except MissingDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentOutputError as e:
        # The data is fine; Bedrock returned something unusable. Calling this
        # a 404 sent people hunting for missing records that were never the
        # problem.
        raise HTTPException(status_code=502, detail=f"The model returned something we couldn't use: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Replan error: {type(e).__name__}: {e}")


# ----------------------------------------------------------------------
# Saved roles — the thing everything else personalises around.
# ----------------------------------------------------------------------

class SavedRole(BaseModel):
    user_id: str
    target_role: str
    status: str = "interested"   # current | interested | watching
    role_family: str = "All"
    note: str = ""


@app.get("/api/saved-roles")
def get_saved_roles(user_id: str):
    """Roles this person is in or curious about. The Watcher scans for
    these daily and the Opportunity Finder organises links around them."""
    table = dynamodb.Table(table_name("saved_roles"))
    items = table.query(KeyConditionExpression=Key("user_id").eq(user_id)).get("Items", [])
    order = {"current": 0, "interested": 1, "watching": 2}
    return decimal_to_native(sorted(items, key=lambda r: order.get(r.get("status"), 3)))


@app.post("/api/saved-roles")
def save_role(role: SavedRole):
    """Star a role. Called when someone taps the heart on a Path Scout
    suggestion — from then on the Watcher includes it in the daily scan."""
    if role.status not in ("current", "interested", "watching"):
        raise HTTPException(status_code=400, detail="status must be current, interested or watching")

    table = dynamodb.Table(table_name("saved_roles"))
    item = {
        "user_id": role.user_id,
        "target_role": role.target_role,
        "status": role.status,
        "role_family": role.role_family,
        "note": role.note,
        "added_on": date.today().isoformat(),
    }
    table.put_item(Item=item)
    return item


@app.delete("/api/saved-roles")
def unsave_role(user_id: str, target_role: str):
    """Un-star a role. It drops out of the daily scan on the next run."""
    table = dynamodb.Table(table_name("saved_roles"))
    table.delete_item(Key={"user_id": user_id, "target_role": target_role})
    return {"removed": target_role, "user_id": user_id}


@app.get("/api/opportunities")
def get_opportunities(user_id: str, role: str = None, model: str = "nova"):
    """Job boards, communities and events for each pathway this person is
    following. Every link comes from the catalogue by ID — the model picks
    which ones belong where, it never writes a URL."""
    try:
        return run_opportunity_finder_agent(user_id, target_role=role, model_key=model, verbose=False)
    except MissingDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentOutputError as e:
        # The data is fine; Bedrock returned something unusable. Calling this
        # a 404 sent people hunting for missing records that were never the
        # problem.
        raise HTTPException(status_code=502, detail=f"The model returned something we couldn't use: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Opportunity finder error: {type(e).__name__}: {e}")


@app.get("/api/prep")
def get_prep(user_id: str, target_role: str = None, model: str = "claude"):
    """Résumé lines, practice questions and an honest readiness score for a
    role they've saved. Defaults to their top 'interested' role."""
    try:
        return run_prep_agent(user_id, target_role, model_key=model, verbose=False)
    except MissingDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentOutputError as e:
        # The data is fine; Bedrock returned something unusable. Calling this
        # a 404 sent people hunting for missing records that were never the
        # problem.
        raise HTTPException(status_code=502, detail=f"The model returned something we couldn't use: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prep coach error: {type(e).__name__}: {e}")


@app.get("/api/watch-list")
def get_watch_list():
    """What the daily scan is currently watching, across everyone. Handy
    for showing 'we're tracking N roles for you' and for debugging why a
    role isn't producing signals."""
    return {"watching": build_watch_list()}


@app.post("/api/watch-run")
def run_watch(source: str = "feeds", dry_run: bool = True, model: str = "nova"):
    """Kick the daily scan by hand. Normally this runs on a schedule
    (EventBridge/cron); this endpoint exists so you can demo it live.
    Defaults to dry_run so a demo tap can't write junk into signals."""
    try:
        return run_market_watcher(source_mode=source, model_key=model, dry_run=dry_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Market watcher error: {e}")
