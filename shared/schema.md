# Career Guardian — Reconciled DynamoDB Schema

Based on the actual dataset (`career_guardian_dataset.zip`) provided by the team. This replaces the earlier planned schema — use this as the single source of truth. Table and field names follow the dataset's own naming so no relabeling is needed when loading data.

---

## 1. `users`
Source: `personas.csv`

| Field | Type | Notes |
|---|---|---|
| `user_id` (PK) | String | e.g. `USER001` |
| `name` | String | |
| `age` | Number | |
| `occupation` | String | |
| `industry` | String | |
| `country` | String | |
| `years_experience` | Number | |
| `education` | String | |
| `employment_status` | String | |
| `employer_context` | String | |
| `career_goal` | String | |
| `preferred_direction` | String | |
| `willing_to_change_career` | Boolean | |
| `alternative_career_interest` | List<String> | |
| `weekly_learning_hours` | Number | drives Planner's pacing |
| `budget` | String (Low/Medium/High) | drives resource filtering |
| `preferred_learning_style` | String | |
| `preferred_course_type` | String | |
| `timeline_months` | Number | |
| `career_change_exposure` | Number | **Do not present as probability of unemployment** — README is explicit on this |

Onboarding infra (Cognito, Textract) writes here too — for the demo, this table is pre-seeded from `personas.csv`.

---

## 2. `user_skills`
Source: `current_skills.csv`

| Field | Type | Notes |
|---|---|---|
| `user_id` (PK) | String | |
| `skill` (SK) | String | composite key with user_id |
| `role` | String | |
| `proficiency` | String | Beginner/Intermediate/Advanced |

---

## 3. `role_tasks`
Source: `role_tasks.csv`

| Field | Type | Notes |
|---|---|---|
| `user_id` (PK) | String | |
| `task_id` (SK) | String | e.g. `TASK001` |
| `role` | String | |
| `task` | String | |
| `skill_area` | String | |
| `baseline_impact` | String | High/Medium/Low |
| `impact_reason` | String | |

This is the **Role Intelligence Agent's** primary output/lookup table — task-level exposure, not whole-job exposure. This is the mechanism behind the demo's "we don't tell Sarah her job is at risk, we tell her which tasks are changing" framing.

---

## 4. `skill_gaps`
Source: `skill_gaps.csv`

| Field | Type | Notes |
|---|---|---|
| `user_id` (PK) | String | |
| `gap_id` (SK) | String | e.g. `SKG001` |
| `role` | String | |
| `skill` | String | |
| `gap_priority` | String | High/Medium/Low |
| `why_it_matters` | String | |

Feeds directly into Planner Agent.

---

## 5. `signals`
Source: `signals.csv`

| Field | Type | Notes |
|---|---|---|
| `signal_id` (PK) | String | e.g. `SIG001` |
| `title` | String | |
| `sector` | String | |
| `technology` | String | |
| `date` | String (ISO date) | |
| `source` | String | |
| `source_url` | String | |
| `evidence` | String | |
| `severity` | String | High/Medium/Low |
| `frequency` | String | High/Medium/Low |
| `source_credibility` | String | High/Medium/Low |
| `affected_roles` | List<String> | |
| `signal_summary` | String | |
| `expected_signal_output` | Map (JSON) | **pre-computed Change Validation Agent output** — `{validated, relevance_score, severity, reason}` |
| `expected_role_output` | List<Map> | **pre-computed Role Intelligence Agent output** per persona — `{persona, affected_tasks, impact_level, affected_skills}` |

This is your Market Watcher's output already baked in — for the demo, **skip building live scraping and start the pipeline from Change Validation Agent**, feeding it these 20 rows. `expected_signal_output` and `expected_role_output` also double as your ground-truth/fallback if a live Bedrock call fails during judging (per `DEMO_SCRIPT.txt`'s fallback plan).

---

## 6. `resources`
Source: `resources.csv`

| Field | Type | Notes |
|---|---|---|
| `resource_id` (PK) | String | e.g. `RES001` |
| `name` | String | |
| `provider` | String | |
| `type` | String | |
| `target_roles` | String | |
| `skills` | String | |
| `funding` | String | |
| `url` | String | |
| `source_url` | String | |
| `notes` | String | **check before demo** — several rows say "verify exact course funding at demo time" |

---

## 7. `plans` (30/60/90-day roadmap)
Source: `plans_30_60_90.csv`

| Field | Type | Notes |
|---|---|---|
| `plan_id` (PK) | String | e.g. `PLAN001`, `PLAN001B`, `PLAN001C` (A/B/C = 30/60/90-day phases) |
| `user_id` | String | |
| `phase` | String | "Days 1–30" etc. |
| `goal` | String | |
| `milestones` | String | |
| `tasks` | String | |
| `resource_ids` | List<String> | FK into `resources` |
| `hours_per_week` | Number | |

This single table covers **both** the Planner Agent's roadmap output and the Action Agent's 30/60/90 sequencing — the dataset has already merged what we'd separately called `roadmaps` and `active_plans`. Recommend keeping it merged rather than splitting, since it saves a handoff step you don't need for the demo.

---

## 8. `pathfinder_results`
Source: `pathfinder.csv`

| Field | Type | Notes |
|---|---|---|
| `user_id` (PK) | String | |
| `target_role` (SK) | String | |
| `similarity_score` | Number | |
| `transferable_skills` | String | |
| `gap_to_role` | String | |
| `reason` | String | |

---

## 9. `progress`
Source: `progress.csv`

| Field | Type | Notes |
|---|---|---|
| `progress_id` (PK) | String | e.g. `PROG001` |
| `user_id` | String | |
| `task` | String | |
| `status` | String | Completed / In Progress / Not Started |
| `completion_pct` | Number | |

This is the Evaluator Agent's state — used to trigger the Planner re-plan loop (the demo's "wow moment").

---

## What changed vs. the earlier planned schema

- `raw_signals` + `validated_trends` → merged into one `signals` table, since the dataset pre-computes both the raw signal and its validated output in the same row (`expected_signal_output` field)
- `role_task_map` → split into two normalized tables, `role_tasks` and `skill_gaps`, matching the dataset's own structure — cleaner for querying either tasks or skill gaps independently
- `roadmaps` + `active_plans` → merged into one `plans` table (see note under table 7)
- Added `user_skills` — wasn't in the original plan, but the dataset separates current skills from role/task data, which is useful if Pathfinder needs to query skills independently of tasks

## Build note

Because `expected_signal_output` and `expected_role_output` already contain what Change Validation and Role Intelligence *should* produce, you can build and demo the pipeline in two stages:
1. **Fast path (safe for demo):** agents read the pre-computed `expected_*` fields directly — guarantees the demo works even if a live Bedrock call is slow/fails
2. **Real path (if time permits):** agents actually call Bedrock and compare their live output against `expected_*` as a sanity check / eval set

This gives you a working demo early, with room to make it "more real" incrementally rather than being blocked on live AI calls working perfectly by Sep 6.
