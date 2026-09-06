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

# End-to-End Pipeline

**File:** `agents/pipeline.py`

The main pipeline connects the core agents:

```text
Signal Agent
     ↓
Role Intelligence Agent
     ↓
Planner Agent
     ↓
Pathfinder Agent
```

The Role Intelligence result is passed directly to Planner to avoid unnecessary duplicate model calls.

Run the full pipeline:

```bash
python agents/pipeline.py USER001 SIG001
```

The combined response contains:

```json
{
  "user_id": "USER001",
  "signal_id": "SIG001",
  "signal": {},
  "role_intelligence": {},
  "plan": {},
  "pathfinder": {}
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
| Resource Connector | Nova       | Structured matching concept  |
| Replanning Trigger | Nova       | Narrow decision concept      |
| Explainer          | Claude     | User-facing synthesis        |

The final three are currently represented in configuration but are not separate standalone agents in the repository.

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
| `generated_plans`    | Configured generated-plan table |

The schema is centralised in:

```text
scripts/config.py
```

---

# Data Loading

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
| Agent pipeline                       | ✅ Implemented      |
| FastAPI backend                      | ✅ Implemented      |
| Browser demo                         | ✅ Implemented      |
| Progress/replanning endpoint         | ✅ Implemented      |
| Comprehensive integration tests      | ⚠️ Limited         |
| Production authentication            | ❌ Not implemented  |
| Resume parsing                       | ❌ Not implemented  |
| Lambda/API Gateway deployment        | ❌ Not implemented  |
| Planner/Progress saved-plan contract | ⚠️ Needs alignment |

---

# Known Limitations

### Planner and Progress use different saved-plan table paths

The current Planner and Progress implementations do not yet use the same saved-plan table.

Planner currently saves through the `plans` table path, while Progress looks for generated plans through `generated_plans`.

This should be aligned before treating the persistent re-planning loop as production-ready.

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
