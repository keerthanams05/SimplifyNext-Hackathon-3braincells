# CareerGuardian

### AI-powered career resilience for a changing workforce

CareerGuardian is an agentic AI system that helps workers understand how real-world technology and labour-market disruptions affect **their specific role**, what skills they should build next, which adjacent career paths may become more attractive, and when their learning plan needs to be updated.

Built for the **SimplifyNext Agentic AI Hackathon 2026**.

---

## Overview

Most career tools stop at generic advice:

> "AI is changing software engineering. Learn AI."

CareerGuardian goes further.

It connects a real-world disruption signal to a person's actual:

* role
* tasks
* current skills
* skill gaps
* learning resources
* career options
* progress

This creates an adaptive career-planning loop:

```text
Disruption Signal
       ↓
Signal Agent
       ↓
Role Intelligence
       ↓
Personalised 30/60/90 Plan
       ↓
Adjacent Career Paths
       ↓
Progress Monitoring
       ↓
Re-planning when necessary
```

---

## Key Features

### Personalised disruption analysis

CareerGuardian does not assume that every disruption affects everyone equally.

The system determines which parts of a specific person's role are affected by a signal.

### AI-generated 30/60/90-day plans

The Planner Agent converts identified skill gaps into an actionable:

* Days 1–30 plan
* Days 31–60 plan
* Days 61–90 plan

Each phase includes goals, milestones, tasks, relevant learning resources, and expected weekly effort.

### Grounded learning resources

Planner recommendations are restricted to resources that actually exist in the application's resource catalogue.

Unknown or hallucinated resource IDs are rejected.

### Adjacent career navigation

The Pathfinder Agent ranks precomputed adjacent career options according to the current disruption.

### Progress-aware replanning

The Progress Agent checks whether someone is falling behind and determines whether their plan should be regenerated.

### End-to-end browser demo

A lightweight frontend demonstrates the complete workflow through a FastAPI backend connected to AWS services.

---

# Architecture

```text
                          ┌────────────────────┐
                          │   Disruption Data  │
                          └─────────┬──────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │    Signal Agent    │
                          │                    │
                          │ Validate signal   │
                          │ Score relevance    │
                          │ Assess severity    │
                          └─────────┬──────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │ Role Intelligence  │
                          │      Agent         │
                          │                    │
                          │ Signal + person    │
                          │ + tasks + skills   │
                          │ + skill gaps       │
                          └─────────┬──────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │   Planner Agent    │
                          │                    │
                          │ Personalised       │
                          │ 30 / 60 / 90 plan  │
                          └─────────┬──────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │ Pathfinder Agent   │
                          │                    │
                          │ Rank adjacent      │
                          │ career options     │
                          └────────────────────┘


                    ┌──────────────────────────┐
                    │     Progress Agent       │
                    │                          │
                    │ Is the person behind?    │
                    │ Does the plan need       │
                    │ to be regenerated?       │
                    └────────────┬─────────────┘
                                 │
                          needs_replan?
                            /         \
                          No           Yes
                          │             │
                          ▼             ▼
                       Continue      Planner
```

---

# Agent Architecture

## 1. Signal Agent

**File:** `agents/signal_agent.py`

The Signal Agent evaluates a disruption signal using its supplied evidence.

It produces:

* validation result
* relevance score
* severity
* explanation

Example:

```json
{
  "signal_id": "SIG001",
  "validated": true,
  "relevance_score": 96,
  "severity": "High",
  "reason": "..."
}
```

Run it directly:

```bash
python agents/signal_agent.py SIG001
```

---

## 2. Role Intelligence Agent

**File:** `agents/role_intelligence_agent.py`

This is the main personalisation layer.

It combines:

* the disruption signal
* the person's role
* the person's tasks
* current skills
* skill gaps

to determine what is actually affected.

Conceptually:

```text
Signal
  +
Person
  +
Tasks
  +
Current Skills
  +
Skill Gaps
      ↓
Role Intelligence
      ↓
Affected Tasks
Affected Skills
Priority Gaps
Explanation
```

Run it with:

```bash
python agents/role_intelligence_agent.py USER001 SIG001
```

---

## 3. Planner Agent

**File:** `agents/planner_agent.py`

The Planner converts the Role Intelligence verdict into a personalised 30/60/90-day upskilling plan.

Each phase contains:

```text
goal
milestones
tasks
resource_ids
hours_per_week
```

The Planner has several important safeguards:

* exactly three phases are required
* phase order is enforced
* required fields are validated
* resource IDs must exist in the trusted resource catalogue
* invalid resource IDs are rejected
* irrelevant signals can short-circuit without another Bedrock call
* an existing Role Intelligence verdict can be reused rather than recomputed

Run it with:

```bash
python agents/planner_agent.py USER001 SIG001
```

Save the generated plan:

```bash
python agents/planner_agent.py USER001 SIG001 --save
```

---

## 4. Pathfinder Agent

**File:** `agents/pathfinder_agent.py`

The Pathfinder ranks adjacent career options based on the current disruption.

It works from the person's existing candidate career paths rather than inventing arbitrary roles.

Example:

```text
Candidate roles
      +
Current disruption
      ↓
Pathfinder
      ↓
Ranked adjacent careers
```

Run it with:

```bash
python agents/pathfinder_agent.py USER001 SIG001
```

---

## 5. Progress Agent

**File:** `agents/progress_agent.py`

The Progress Agent determines whether a person is falling behind and whether a new plan is required.

Example output:

```json
{
  "behind_items": [
    "PROG002"
  ],
  "needs_replan": true,
  "explanation": "..."
}
```

Run it with:

```bash
python agents/progress_agent.py USER001
```

---

## 6. Resource Connector Agent

**File:** `agents/resource_connector_agent.py`

Matches each skill gap to 1-2 real SkillsFuture/WSG programmes, respecting the person's budget, weekly hours and preferred course type. Its `shortlist_resource_ids` is handed to the Planner, so the expensive planning call sees a short pre-filtered list instead of the whole catalogue — and the Planner's allow-list becomes that shortlist, so it cannot cite a course the Connector didn't approve.

Returns an empty list for a gap rather than forcing a bad match.

```bash
python agents/resource_connector_agent.py USER004
```

---

## 7. Opportunity Finder Agent

**File:** `agents/opportunity_finder_agent.py`

For every role a person has saved, gathers the real places to go — job boards, professional bodies, communities, events, hackathons — grouped into plain-language sections ("Where the jobs are posted", "People doing this already", "Things to turn up to").

**The model never writes a URL.** Every link comes out of `opportunities.csv` or `resources.csv` by ID, and `filter_to_real_ids()` discards anything that isn't a real ID before it reaches a person. A hallucinated course link in a careers product is the worst failure this app could have.

```bash
python agents/opportunity_finder_agent.py USER004
python agents/opportunity_finder_agent.py USER004 --role "Finance Analyst"
```

---

## 8. Prep Coach Agent

**File:** `agents/prep_agent.py`

Turns someone's real tasks into résumé lines aimed at a target role, plus the interview questions they'd actually face and an honest readiness score.

Two hard rules in the prompt: it must **never invent experience** (every bullet names the real task it rewrites, and `validate_bullets()` warns if one doesn't trace back), and it must **never coach someone to hide a gap** — it names gaps and gives honest language for what they're doing about them.

```bash
python agents/prep_agent.py USER004 "Finance Analyst"
```

---

## 9. Explainer Agent

**File:** `agents/explainer_agent.py`

The only agent whose audience is the user rather than the code. Rewrites the whole pipeline result as plain language: headline, summary, what changed, first step.

Its system prompt **bans probability-of-unemployment framing outright** and forces task-level language. Everything else in the system produces data; this is where tone is decided.

```bash
python agents/explainer_agent.py USER004 SIG016
```

---

## 10. Market Watcher Agent

**File:** `agents/market_watcher_agent.py`

The daily scan. Builds its watch list from the roles people have saved, pulls headlines from `data/watch_sources.csv` (RSS and Atom, stdlib only), drops anything already known by URL *and* by normalised title (stories get re-syndicated under different headlines), and triages the rest on the cheap tier.

Survivors are written as **unvalidated** signals (`severity: "Unrated"`, `validated: false`) for the Signal Agent to assess — the Watcher finds things, it never decides they are true. A dead feed is skipped with a warning rather than failing the run.

```bash
python agents/market_watcher_agent.py --source seeded --dry-run   # offline, no spend
python agents/market_watcher_agent.py                             # the daily run
```

Feed URLs in `watch_sources.csv` are marked VERIFY — check them before the demo. Scheduling is not yet wired up; EventBridge `rate(1 day)` or a cron line both work.

---

# End-to-End Pipeline

**File:** `agents/pipeline.py`

The main pipeline connects the core agents:

The chain is four waves, not six steps. Signal, Role Intelligence and
Pathfinder do not depend on each other, so they run concurrently:

```text
wave 1   Signal Agent  ‖  Role Intelligence  ‖  Pathfinder
              ↓
wave 2   Resource Connector   (skipped if nothing was affected)
              ↓
wave 3   Planner              (gets wave 1's verdict + wave 2's shortlist)
              ↓
wave 4   Explainer            (optional; plain-language layer)
```

The Role Intelligence result is passed directly to Planner to avoid duplicate
model calls, and the Resource Connector's shortlist narrows what the Planner
is allowed to cite.

Concurrency is only safe because every agent shares thread-local AWS handles
from `scripts/aws_clients.py` — boto3 resources are not thread-safe, and each
thread gets its own Session-backed instance. **New agents should import
`dynamodb`/`bedrock` from there rather than calling `boto3.resource()` at
module level.**

Run the full pipeline:

```bash
python agents/pipeline.py USER001 SIG001            # parallel (default)
python agents/pipeline.py USER001 SIG001 --serial   # one at a time, to compare
python agents/pipeline.py USER001 SIG001 --no-explain
```

Both modes print a `WHERE THE TIME WENT` table, and `/api/pipeline` returns the
same numbers in a `timings` block, so pipeline speed stays measured rather than
assumed.

The combined response contains:

```json
{
  "user_id": "USER001",
  "signal_id": "SIG001",
  "signal": {},
  "role_intelligence": {},
  "resources": {},
  "plan": {},
  "pathfinder": {},
  "explanation": {},
  "timings": {}
}
```

---

# AWS Architecture

CareerGuardian uses AWS for data storage and model inference.

```text
CSV Dataset
     ↓
     S3
     ↓
Data Loader
     ↓
DynamoDB
     ↓
Agents
     ↓
Amazon Bedrock
     ↓
FastAPI
     ↓
Frontend
```

### AWS services used

| Service         | Purpose                     |
| --------------- | --------------------------- |
| Amazon Bedrock  | Agent model inference       |
| Amazon DynamoDB | Structured application data |
| Amazon S3       | CSV/data storage            |
| FastAPI         | Backend API                 |

---

# Model Strategy

Different agents use different model tiers depending on the complexity of the task.

| Agent              | Model Tier | Reason                       |
| ------------------ | ---------- | ---------------------------- |
| Signal Agent       | Claude     | Signal/evidence reasoning    |
| Role Intelligence  | Claude     | Person-specific reasoning    |
| Planner            | Claude     | Multi-stage planning         |
| Pathfinder         | Claude     | Comparative career reasoning |
| Resource Connector | Nova       | Structured matching          |
| Opportunity Finder | Nova       | Picks links from a catalogue |
| Replanning Trigger | Nova       | Narrow decision              |
| Market Watcher     | Nova       | Headline triage              |
| Explainer          | Claude     | User-facing synthesis        |
| Prep Coach         | Claude     | Rewriting real experience    |

All of these are now standalone agents in `agents/`.

Model tier is **not surfaced in the UI** — `/api/agents` deliberately omits it. Which model runs which agent is an implementation detail, not something a user needs to read.

---

# Data Model

The project uses CSV files as the source dataset and loads them into DynamoDB.

```text
data/
├── personas.csv
├── current_skills.csv
├── role_tasks.csv
├── skill_gaps.csv
├── signals.csv
├── resources.csv
├── plans_30_60_90.csv
├── pathfinder.csv
└── progress.csv
```

### DynamoDB tables

| Table                | Purpose                         |
| -------------------- | ------------------------------- |
| `users`              | Persona/profile information     |
| `user_skills`        | Current skills                  |
| `role_tasks`         | Person-specific tasks           |
| `skill_gaps`         | Identified skill gaps           |
| `signals`            | Disruption signals              |
| `resources`          | Learning resources              |
| `plans`              | Plan/reference-plan data        |
| `progress`           | Progress records                |
| `pathfinder_results` | Candidate career paths          |
| `generated_plans`    | Plans written by `planner_agent --save` |
| `saved_roles`        | **What everything personalises around** — the role someone is in, plus roles they've starred |
| `opportunities`      | 20 real SG job boards, communities, events and professional bodies |

The schema is centralised in:

```text
scripts/config.py
```

---

# Data Loading

**The repo is the source of truth, not the S3 bucket.** `data/*.csv` is
version-controlled and reviewed; the bucket is a snapshot someone uploaded at
some point. `load_data.py` reads the repo by default:

```bash
python scripts/load_data.py                  # from data/ (default)
python scripts/load_data.py --from-s3        # pull from the bucket first
python scripts/load_data.py --push-to-s3     # load, then refresh the bucket from the repo
```

Use `--push-to-s3` once to bring the bucket back in line after data changes.


Create the DynamoDB tables:

```bash
python scripts/create_table.py
```

Load the dataset:

```bash
python scripts/load_data.py
```

Load a single file:

```bash
python scripts/load_data.py --only personas.csv
```

The loader:

1. reads the source CSV data
2. parses configured structured fields
3. converts values into DynamoDB-compatible types
4. writes records in batches
5. records load information in `processed/load_summary.json`

---

# API

**File:** `api/main.py`

Start the API:

```bash
uvicorn api.main:app --reload --port 8000
```

### Available endpoints

| Endpoint                                      | Purpose                                 |
| --------------------------------------------- | --------------------------------------- |
| `GET /api/personas`                           | List demo personas                      |
| `GET /api/signals`                            | List signals                            |
| `GET /api/profile?user_id=...`                | Fetch a persona profile                 |
| `GET /api/alerts?user_id=...`                 | Get role-relevant alerts                |
| `GET /api/pipeline?user_id=...&signal_id=...` | Run the full pipeline                   |
| `GET /api/progress?user_id=...&signal_id=...` | Run Progress Agent                      |
| `GET /api/replan?user_id=...&signal_id=...`   | Check progress and re-plan if necessary |
| `GET /api/agents`                             | Agent directory: what each is for, what it hands to next |
| `GET/POST/DELETE /api/saved-roles`            | List, star and un-star roles |
| `GET /api/opportunities?user_id=...&role=...` | Job boards, communities and events per pathway |
| `GET /api/prep?user_id=...&target_role=...`   | Résumé lines, practice questions, readiness |
| `GET /api/resources?user_id=...`              | Course matches on their own (cheap tier) |
| `GET /api/explain?user_id=...&signal_id=...`  | Plain-language output only |
| `GET /api/watch-list`                         | What the daily scan is watching |
| `POST /api/watch-run?dry_run=true`            | Kick the daily scan by hand (for demos) |

The alerts endpoint intentionally performs lightweight role matching first instead of running the full Bedrock pipeline for every alert.

---

# Frontend

**File:** `frontend/index.html`

The frontend demonstrates the complete CareerGuardian experience:

```text
Choose Persona
      ↓
View Profile
      ↓
View Current Skills
      ↓
View Relevant Disruptions
      ↓
Open Disruption
      ↓
Run Agent Pipeline
      ↓
View Impact
      ↓
View Personalised Plan
      ↓
View Adjacent Careers
      ↓
Check Progress
      ↓
Re-plan if needed
```

The current frontend is a hackathon demo rather than a production application.

---

# Example Persona

The repository includes a demo software engineer persona:

### Sarah Tan

```text
Role: Software Engineer
Experience: 5 years
Location: Singapore
```

Her current skills include areas such as:

* Python
* Java
* SQL
* Git
* REST APIs
* Backend Development
* Unit Testing

Her skill gaps include areas such as:

* AI-Assisted Software Development
* LLM Application Development
* Prompt / Context Engineering
* Cloud Deployment
* AI System Integration
* MLOps / AI Evaluation Fundamentals

This makes her a useful example for demonstrating how an AI-related disruption is translated into personalised career action.

---

# Example Disruption

A representative signal is `SIG001`, which concerns the increasing adoption of agentic AI and coding agents.

The intended reasoning chain is:

```text
SIG001
  ↓
Software engineering role affected
  ↓
Which tasks are affected?
  ↓
Which skill gaps matter?
  ↓
What should the person learn?
  ↓
Generate 30/60/90 plan
  ↓
Which adjacent careers are relevant?
  ↓
Monitor progress
```

---

# Guardrails

CareerGuardian intentionally separates model reasoning from trusted application data.

### Resource validation

Planner can only use `resource_id` values present in the actual resource catalogue.

### Structured validation

Planner validates:

* phase count
* phase order
* required fields
* resource IDs
* basic output structure

### Person-specific grounding

Role Intelligence is grounded in the person's actual:

```text
role
tasks
skills
skill gaps
```

rather than generic assumptions about their profession.

### No-plan handling

When a signal does not affect a person, Planner can return without generating an unnecessary generic learning plan.

---

# Testing

The repository currently includes a basic data-load sanity test:

```bash
python tests/test_load.py
```

The current test coverage focuses mainly on verifying that expected data has been successfully loaded into DynamoDB.

A more complete automated regression suite is still a future improvement.

---

# Running the Demo

## 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure AWS

Configure access to:

* Amazon Bedrock
* Amazon DynamoDB
* Amazon S3

Set the required configuration values used by `scripts/config.py`.

Do not commit AWS credentials or secrets to GitHub.

## 3. Create tables

```bash
python scripts/create_table.py
```

## 4. Load data

```bash
python scripts/load_data.py
```

## 5. Start the API

```bash
uvicorn api.main:app --reload --port 8000
```

## 6. Open the frontend

Open:

```text
frontend/index.html
```

---

# Project Structure

```text
SimplifyNext-Hackathon-3braincells/
│
├── agents/
│   ├── signal_agent.py
│   ├── role_intelligence_agent.py
│   ├── planner_agent.py
│   ├── pathfinder_agent.py
│   ├── progress_agent.py
│   └── pipeline.py
│
├── api/
│   └── main.py
│
├── frontend/
│   └── index.html
│
├── data/
│   ├── personas.csv
│   ├── current_skills.csv
│   ├── role_tasks.csv
│   ├── skill_gaps.csv
│   ├── signals.csv
│   ├── resources.csv
│   ├── plans_30_60_90.csv
│   ├── pathfinder.csv
│   └── progress.csv
│
├── scripts/
│   ├── config.py
│   ├── create_table.py
│   ├── load_data.py
│   └── bedrock_test.py
│
├── tests/
│   └── test_load.py
│
├── requirements.txt
└── README.md
```

---

# Current Status

| Component                            | Status             |
| ------------------------------------ | ------------------ |
| Data model                           | ✅ Implemented      |
| S3 → DynamoDB ingestion              | ✅ Implemented      |
| DynamoDB table creation              | ✅ Implemented      |
| Signal Agent                         | ✅ Implemented      |
| Role Intelligence Agent              | ✅ Implemented      |
| Planner Agent                        | ✅ Implemented      |
| Planner resource validation          | ✅ Implemented      |
| Pathfinder Agent                     | ✅ Implemented      |
| Progress Agent                       | ✅ Implemented      |
| Resource Connector Agent             | ✅ Implemented      |
| Opportunity Finder Agent             | ✅ Implemented      |
| Prep Coach Agent                     | ✅ Implemented      |
| Explainer Agent                      | ✅ Implemented      |
| Market Watcher Agent                 | ✅ Implemented      |
| Saved roles                          | ✅ Implemented      |
| Agent pipeline                       | ✅ Implemented      |
| Pipeline parallelisation             | ✅ Implemented      |
| FastAPI backend                      | ✅ Implemented      |
| Browser demo                         | ✅ Implemented      |
| Progress/replanning endpoint         | ✅ Implemented      |
| Comprehensive integration tests      | ⚠️ Limited         |
| Production authentication            | ❌ Not implemented  |
| Resume parsing                       | ❌ Not implemented  |
| Lambda/API Gateway deployment        | ❌ Not implemented  |
| Planner/Progress saved-plan contract | ✅ Aligned          |
| Daily Market Watcher scheduling      | ❌ Not scheduled    |

---

# Known Limitations

### ~~Planner and Progress use different saved-plan table paths~~ — resolved

Both now use `generated_plans`. The `plans` table is left for the gold-standard
reference rows seeded from `plans_30_60_90.csv`, which the demo compares
generated output against; mixing generated plans into it would spoil that
comparison.

### The daily scan is not scheduled

`market_watcher_agent.py` runs when someone runs it. Making it genuinely daily
needs EventBridge → Lambda or a cron line. Its feed URLs are also unverified —
several are marked VERIFY in `watch_sources.csv`.

### Course Finder and Plan Builder are not yet re-pickable

The frontend renders what the agents chose. Letting someone swap a course or
change their weekly hours and re-plan needs an endpoint that accepts their
selection.

### Local API deployment

The current backend is intended for the hackathon demo and runs through local FastAPI rather than a deployed Lambda/API Gateway architecture.

### Demo authentication and resume flow

The frontend uses preloaded personas. Production authentication and live resume parsing are not currently implemented.

---

# Why CareerGuardian?

Career disruption is not the same for everyone.

The same technology change can:

* remove some tasks
* change other tasks
* create new skill requirements
* make some adjacent careers more attractive
* require a different learning strategy over time

CareerGuardian is designed to continuously connect those changes to the individual:

```text
What changed?
     ↓
Why does it matter to me?
     ↓
What should I learn?
     ↓
What could I become?
     ↓
Am I keeping up?
     ↓
Should my plan change?
```

That is the core idea behind CareerGuardian:

> **Don't just predict which jobs will change. Help the individual respond to that change.**
