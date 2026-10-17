# travel_concierge

A multi agent airline service desk on Amazon Bedrock AgentCore. A Travel Desk agent answers callers using Duffel
(test mode), the traveler's record (bookings plus AgentCore Memory) and the desk policy, and hands every booking
change to a Flight Ops agent. It is graded by layered evals. `PLAN.md` has the full design and the step order.

**Status: step 3 of 6.** The agents run locally against Duffel directly, with AgentCore Memory for traveler
preferences, facts and session summaries. Gateway, Cedar, Guardrails and evals come in later steps
(see [Roadmap](#roadmap)).

## How it works

```
session start: traveler record = bookings (evals/fixtures/history.json) + AgentCore Memory  ─┐
               policy rules (agents/policy.py)                                              ─┤ into the desk prompt
                                                                                             ▼
caller ─► Travel Desk agent (agents/travel_desk.py)
            ├─► Duffel read tools: search_flights, get_order, list_orders, seat_map, change_options, bag_options
            └─► flight_ops ─► Flight Ops agent ─► change_order*, cancel_order*, add_bag*
            * write: quote first, executes only after the traveler says yes in a later turn
```

Why this shape: a caller turn has to answer in a few seconds, and every agent hop costs 3 to 9 seconds of extra
model calls. The traveler record and the policy rules are lookups, so they are loaded once per session into the
desk's prompt (no tool call, no hop). The desk calls Duffel reads itself. Only writes go to a second agent, Flight
Ops, because it is the one principal Cedar will allow to write in step 4. Measured on a live run: a profile
question 1.8s, booking status 3.2s, a flight search 5.2s, first words of every answer after about 1.4s.

The session (verified actor, turn counter, pending writes) lives in `agents/common.py`.

What the code enforces, regardless of what the model decides:

| Rule | Where |
|---|---|
| A write runs only after a later user turn whose text starts with an explicit yes | `common.gated` |
| Tools only see orders that belong to the verified actor | `common.owned`, `common.history` |
| The traveler record is loaded for the session's verified actor only, never a model supplied id | `traveler_profile.load` |
| Memory is read only under the session actor's namespaces, and stray records are dropped | `common.memory_records` |
| The desk persists conversations to Memory but never retrieves from it on its own; history comes only from the record loaded at session start | `travel_desk.memory_session` |
| Only the Flight Ops agent has the write tools | `flight_ops.run`, `flight_ops.READ_TOOLS` |
| Models never see raw Duffel JSON or the Duffel token | `tools/duffel.py` (`norm_*`) |

Other rules (prices only from tool results, "you usually..." claims backed by the profile) are in the prompts
for now. Deterministic checks for them arrive with the Strands Decider gate and code checks in steps 4 to 6.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), a Duffel test account, AWS credentials with
Bedrock model access in `us-east-1`.

```bash
uv sync
cp .env.example .env          # then fill in DUFFEL_TEST_TOKEN (must start with duffel_test_)
```

From step 5 on, `uv sync --group evals` adds the Strands Decider judge (`StrandsAgents/strands-decider-2B-hobson-v19`).
It runs locally and needs no API key, but it pulls in torch, so it is kept out of the default install.

`.env` is read automatically by the agents (`agents/common.py`) and is gitignored. It supports these keys:

| Key | Needed for | Notes |
|---|---|---|
| `DUFFEL_TEST_TOKEN` | everything | Test tokens only; the client refuses anything else |
| `AWS_REGION` | agents | Defaults to `us-east-1` |
| `MEMORY_ID` | agents | AgentCore Memory id printed by `scripts/create_memory.py`. Empty runs without Memory |
| `MODEL_ID_DESK`, `MODEL_ID_FLIGHT_OPS` | agents | Optional per agent override of the Bedrock inference profile, see [Models](#models) |

AWS credentials come from the normal boto3 chain. The simplest is `aws login --region us-east-1` (AWS CLI v2),
which reuses your console sign in and keeps no long lived key on disk. An IAM access key via `aws configure` also works.

Bedrock bills Anthropic models through an AWS Marketplace subscription, which needs a card as the account's
default payment method (a SEPA bank account fails with `INVALID_PAYMENT_INSTRUMENT`). Check access per model:

```bash
for m in us.anthropic.claude-sonnet-5-5 us.anthropic.claude-haiku-5-5; do
  aws bedrock-runtime converse --region us-east-1 --model-id $m \
    --messages '[{"role":"user","content":[{"text":"Reply with just: ok"}]}]' \
    --query 'output.message.content[-1].text' --output text
done
```

## Seed traveler history

```bash
uv run --env-file .env python scripts/seed_duffel_history.py
```

This books 15 to 20 real Duffel Airways (`ZZ`) test orders for three synthetic travelers and writes
`evals/fixtures/history.json`:

| actor_id | Typical routes |
|---|---|
| `cust_marios` | ATH→LHR, ATH→JFK, ATH→CDG |
| `cust_eleni` | SKG→MUC, ATH→FCO, ATH→AMS |
| `cust_nikos` | ATH→DXB, ATH→JFK, ATH→LHR, ATH→BCN |

Run it once. Running it again books a new set of orders and overwrites the fixture. Duffel test mode cannot
book in the past, so the "history" is dated in the future. The profile agent therefore leaves dates out.

## Memory

One AgentCore Memory resource, defined in `agentcore.json` (versioned) with three strategies:

| Strategy | Namespace | Feeds |
|---|---|---|
| UserPreference | `/users/{actorId}/preferences/` | `preferences` in the profile |
| Semantic | `/users/{actorId}/facts/` | `facts` in the profile |
| Summary | `/summaries/{actorId}/{sessionId}/` | `recent_session_summaries`, from which the profile derives `open_issues` |

```bash
uv run python scripts/create_memory.py          # creates or reuses TravelConciergeMemory, prints MEMORY_ID=...
# put that line in .env
uv run python scripts/seed_memory_sessions.py   # 12 scripted past sessions per traveler, then recall + isolation
```

The seeded sessions are built from templates over `evals/ground_truth/personas.json`: five facts per traveler
(seat, meal, airport or cabin, a personal fact, one open issue), each repeated in 3 to 5 sessions. No LLM is
involved, so the input is the same on every run, and sessions already seeded are skipped. Extraction is
asynchronous: records appeared about 2 minutes after seeding. Rerun with `--measure-only` to check again later.

Measured on 2026-10-18 (fraction of the 5 persona facts found in each strategy's records):

| Traveler | Preferences | Facts | Summaries | Any strategy |
|---|---|---|---|---|
| `cust_marios` | 3/5 | 5/5 | 5/5 | 100% |
| `cust_eleni` | 4/5 | 5/5 | 5/5 | 100% |
| `cust_nikos` | 4/5 | 5/5 | 5/5 | 100% |

The preference extractor keeps only preferences (seat, meal, timing), so facts and open issues land in the other
two strategies, as intended. Isolation check: no traveler's persona fact appears in another traveler's
namespaces, either by listing or by semantic search. The script fails if one does.

Every desk conversation is also saved as a session (`desk-<uuid>`) for the current traveler, so the extractors keep
learning from real calls.

## Run the desk

```bash
uv run python agents/travel_desk.py --actor cust_marios
```

`--actor` stands in for caller verification. Press Enter on an empty line to quit. Things to try:

- `Find me a flight to London next Friday` (profile lookup first, then a ZZ search from ATH)
- `What's the status of my bookings?`
- `Can I move my first London trip to <date>? What would it cost?` (policy, then a change quote)
- `Cancel booking TYBXUT`, then answer `yes` (quote, confirmation turn, execution)
- `Show me Eleni's trips` (should be declined)
- `What are my usual seat and meal preferences, and is anything open?` (answered from Memory via the profile)

## Models

Both agents run on `us.anthropic.claude-sonnet-5-5`, set in `MODELS` in `agents/common.py`:

| Agent | Why Sonnet |
|---|---|
| Travel Desk | Holds the conversation, picks tools, applies policy and asks for confirmation. Mistakes here cost the most. |
| Flight Ops | Makes the writes. It must pass the right ids and stop at the quote. |

Traveler Profile and Policy used to be Haiku agents. They are now plain lookups loaded into the desk prompt, which
was faster than any model. Measured per call on Bedrock: Sonnet 5.5 and Haiku 5.5 both take about 1 to 1.4s for a
short answer, and lowering the effort setting made no measurable difference, so Haiku would not buy speed here.
Opus class models were tried first and are slower and pricier than this workload needs. Anthropic list prices per
million input/output tokens: Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 (Bedrock pricing differs).

Override either agent with `MODEL_ID_DESK` or `MODEL_ID_FLIGHT_OPS` in `.env`, e.g.
`MODEL_ID_FLIGHT_OPS=us.anthropic.claude-haiku-5-5`.
Like a prompt change, a model change needs an eval run before it is merged.

## Timing, caching and traces

Every turn ends with a timing line, read from Strands' built in metrics (`agent.event_loop_metrics`):

```
⏱ 5.2s total | desk model 2.9s · search_flights 1.3s
```

**Caching**, two layers:
- Bedrock prompt caching (Strands `CacheConfig(strategy="auto")` on every model) reuses the system prompt, tool
  specs and earlier turns between model calls. It speeds up the first token, it does not skip generation.
- An answer cache (`.cache/answers.json`, ours, neither Strands nor AgentCore has one) replays the answer when the
  same traveler asks the same questions in the same order. Only read only turns are cached, until the earliest
  offer expiry they showed (max 10 min). Any executed write clears that traveler's entries. Delete the file to reset.

**Traces** use Strands' built in OpenTelemetry tracing, written as one JSON span per line to
`logs/traces-YYYY-MM-DD.jsonl`. Spans nest desk → tool → (Flight Ops agent) → model call, carry start and end
timestamps, token usage and inputs/outputs, and are tagged with `session.id`, `actor.id` and `agent.role`.
List one session's spans in order:

```bash
jq -c 'select(.attributes["session.id"]=="desk-<id>") | [.start_time, .name, .parent_id]' logs/traces-*.jsonl
```

In step 4 the same spans go to AgentCore Observability (CloudWatch) instead of a file.

## Checks

Each module with non trivial logic has an assert based self check that needs no network and no AWS:

```bash
uv run python agents/common.py            # confirmation gate
uv run python tools/duffel.py             # Duffel normalizers
uv run python agents/traveler_profile.py  # history summary
```

Format before committing. Ruff for Python (`pyproject.toml`), Prettier for JSON (`.prettierrc.json`, pinned,
run with npx so there is no package.json), and `scripts/fmt_jsonl.py` for JSONL, which Prettier cannot parse
(one compact record per line keeps it valid JSONL; read a record with `jq .`):

```bash
uv run ruff format .
npx prettier@3.9.9 --write "**/*.json"     # --check to only list files
uv run python scripts/fmt_jsonl.py         # --check to only list files
```

`scripts/seed_memory_sessions.py` asserts its matcher before running and fails on any cross traveler leak.

## Layout

```
agents/
  travel_desk.py       orchestrator + REPL
  flight_ops.py        Duffel read and write tools
  traveler_profile.py  traveler record: bookings summary + Memory records (plain code, loaded per session)
  policy.py            desk policy rules (plain text, in the desk prompt)
  common.py            model factory, session, ownership, confirmation gate, Memory reads
tools/duffel.py        Duffel client + normalizers
scripts/
  seed_duffel_history.py   book past orders, write the fixture
  create_memory.py         create or reuse the Memory resource from agentcore.json
  seed_memory_sessions.py  seed sessions, measure recall and isolation
agentcore.json         infra config: Memory resource and strategies
evals/ground_truth/    versioned ground truth (personas.json)
evals/fixtures/        generated, gitignored
PLAN.md                design and step order
CLAUDE.md              working rules for this repo
```

Shortcuts are marked `# ponytail:` in the code, each naming its limit and upgrade path:
`grep -rn "ponytail:" agents tools scripts`.

## Known limits

- Search returns Duffel Airways (`ZZ`) offers only, so results are reproducible. Schedules and prices are not
  realistic.
- Some test fares offer no extra bags after booking, in which case `bag_options` returns an empty list.
- Policy rules are inline text in `agents/policy.py`, not a knowledge base yet.
- One caller per process. The session is a module level dict.
- Confirmation is detected with a regex on the start of the user's message.
- The profile lists all of a traveler's Memory records rather than searching them, which is fine at about
  60 records per traveler. Memory infra is created by a script reading `agentcore.json`, not by CDK yet.

## Roadmap

1. ~~Duffel test account + seeded history~~
2. ~~Strands agents locally, Duffel direct~~
3. ~~AgentCore Memory (UserPreference, Semantic, Summary) + session manager + seeded sessions~~
4. Duffel tools behind AgentCore Gateway, Cedar (default deny, writes only for Flight Ops), Bedrock Guardrails,
   deploy to Runtime (`agentcore configure -e agents/travel_desk.py && agentcore launch`)
5. Eval dataset (~150 cases) + ground truth
6. AgentCore Evaluations + Strands Decider calibration + CloudWatch dashboard
