# travel_concierge

A multi agent airline service desk on Amazon Bedrock AgentCore. A Travel Desk agent answers callers using Duffel
(test mode), the traveler's record (bookings plus AgentCore Memory) and the desk policy, and hands every booking
change to a Flight Ops agent. It is graded by layered evals. `PLAN.md` has the full design and the step order.

**Status: step 4 of 6.** The desk runs on AgentCore Runtime (and locally) with AgentCore Memory, every Duffel
tool behind AgentCore Gateway with Cedar policies in ENFORCE mode, and a Bedrock Guardrail on both agents. All
infrastructure is declared in `agentcore/agentcore.json` and its CDK stack. Evals come next
(see [Roadmap](#roadmap)).

## How it works

```
session start: traveler record = bookings (Gateway list_orders) + AgentCore Memory  ─┐
               policy rules (agents/policy.py)                                     ─┤ into the desk prompt
                                                                                    ▼
caller ─► AgentCore Runtime ─► Travel Desk agent (agents/travel_desk.py, runtime_app.py)   [Bedrock Guardrail]
            ├─► read tools ──────────────────────────── as role TravelConcierge-DeskGateway ──┐
            └─► flight_ops ─► Flight Ops agent ─► writes* ─ as role TravelConcierge-FlightOpsGateway ─┤
                                                                                              ▼
                       AgentCore Gateway (AWS_IAM) ─► Cedar policy engine (ENFORCE, default deny)
                                                   ─► Lambda TravelConcierge-Duffel ─► Duffel (token in Secrets Manager)
            * book_offer, change_order, cancel_order, add_bag: quote first, execute only after an explicit yes
```

Why this shape: a caller turn has to answer in a few seconds, and every agent hop costs 3 to 9 seconds of extra
model calls. The traveler record and the policy rules are lookups, so they are loaded once per session into the
desk's prompt (no tool call, no hop). The desk calls Duffel reads itself. Only writes go to a second agent, Flight
Ops, because it is the one principal Cedar allows to write. See [Latency](#latency) for measured turn times.

The session (verified actor, turn counter, pending writes) lives in `agents/common.py`.

What the code enforces, regardless of what the model decides:

| Rule | Where |
|---|---|
| A write runs only after a later user turn whose text starts with an explicit yes | `common.gated` (agent) |
| Only the Flight Ops identity may call book/change/cancel/add_bag, and only for a verified caller; the desk identity never can | Cedar: `policies/*.cedar` |
| Anything no policy permits is denied | Gateway policy engine, ENFORCE mode |
| Orders are only visible to the traveler they belong to (Duffel order `metadata.actor_id`) | `tools/lambda_handler.py` (`_owned`) |
| No cancellation when departure is under 24 hours | `tools/lambda_handler.py` (`cancel_order`) |
| `actor_id` and `guest_verified` come from the verified session, never from the model (the model's tool schemas don't have them) | `common.call_tool` |
| The traveler record is loaded for the session's verified actor only | `traveler_profile.load` |
| Memory is read only under the session actor's namespaces, and stray records are dropped | `common.memory_records` |
| The desk persists conversations to Memory but never retrieves from it on its own | `travel_desk.memory_session` |
| Card and passport numbers are blocked on input and masked on output; visa and medical advice are denied | Bedrock Guardrail (CDK stack) |
| Models never see raw Duffel JSON; the Duffel token exists only in Secrets Manager and the Lambda | `tools/lambda_handler.py`, `tools/duffel.py` (`norm_*`) |

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
| `MEMORY_ID` | agents | AgentCore Memory id (`agentcore status --type memory`). Empty runs without Memory |
| `GATEWAY_URL`, `GATEWAY_ROLE_DESK`, `GATEWAY_ROLE_FLIGHT_OPS` | agents | Gateway URL and the two caller roles, from the `agentcore deploy` outputs |
| `GUARDRAIL_ID`, `GUARDRAIL_VERSION` | agents | Bedrock Guardrail, from the deploy outputs. Empty runs without it |
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

One AgentCore Memory resource, declared in `agentcore/agentcore.json` and owned by the CDK stack (it was created
in step 3 and imported with `agentcore import memory`, so its records were kept), with three strategies:

| Strategy | Namespace | Feeds |
|---|---|---|
| UserPreference | `/users/{actorId}/preferences/` | `preferences` in the profile |
| Semantic | `/users/{actorId}/facts/` | `facts` in the profile |
| Summary | `/summaries/{actorId}/{sessionId}/` | `recent_session_summaries`, from which the profile derives `open_issues` |

```bash
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

## Deploy (AgentCore)

Everything in AWS is declared in `agentcore/agentcore.json` (Memory, Gateway, Lambda target with its tool schemas,
policy engine, Runtime) plus `agentcore/cdk/lib/cdk-stack.ts` (the two Gateway caller roles, the Bedrock Guardrail,
the runtime role's extra permissions), and deployed with the AgentCore CLI (`@aws/agentcore`, CDK under the hood).

```bash
npm install -g @aws/agentcore                # Node 20+; replaces the deprecated Python starter toolkit
(cd agentcore/cdk && npm ci)
uv run python scripts/prepare_gateway.py     # once: Duffel token -> Secrets Manager, tag seeded orders with actor_id
agentcore deploy -y                          # uv must be on PATH (it packages the Lambda and the runtime)
```

Copy the deploy outputs into `.env`: `GatewayTravelDeskGatewayUrlOutput` → `GATEWAY_URL`, `DeskGatewayRoleArn` /
`FlightOpsGatewayRoleArn` → `GATEWAY_ROLE_DESK` / `GATEWAY_ROLE_FLIGHT_OPS`, `GuardrailIdOutput` /
`GuardrailVersionOutput` → `GUARDRAIL_ID` / `GUARDRAIL_VERSION`. The runtime gets the same values from `envVars`
in `agentcore.json`.

**Policies.** `policies/*.cedar` are the source of truth. The CLI keeps a copy of each statement inline in
`agentcore.json`, which is what deploys, so after editing a `.cedar` file:

```bash
uv run python scripts/sync_policies.py       # --check to only report stale statements
agentcore deploy -y
uv run python scripts/check_gateway_policies.py            # live: 8 checks, writes only quote
uv run python scripts/check_gateway_policies.py --writes   # also books and cancels a flight for cust_nikos
```

A new Gateway tool has to be deployed before a policy that names it: the policy engine validates statements
against the Gateway's tool list, so add the tool, deploy, then sync the policy and deploy again.

**Identities.** The Gateway uses AWS_IAM inbound auth. Each agent assumes its own role
(`TravelConcierge-DeskGateway`, `TravelConcierge-FlightOpsGateway`) and signs its MCP calls with SigV4, so Cedar
sees `AgentCore::IamEntity::"arn:aws:sts::<account>:assumed-role/<role>"` and can tell the desk from Flight Ops. The
traveler (`actor_id`) and `guest_verified` are added to every tool call by `common.call_tool` from the verified
session, where Cedar reads `context.input.guest_verified` and the Lambda checks `actor_id` against the order's
Duffel `metadata.actor_id`.

**Runtime.** `agents/` is the runtime code (`runtime_app.py`, dependencies in `agents/pyproject.toml`). Talk to the
deployed desk with the traveler in the `X-Actor-Id` header (allowlisted) or in the payload:

```bash
agentcore invoke --runtime TravelDesk -H "X-Actor-Id: cust_marios" --session-id <33+ chars> "What's the status of YLNUD2?"
uv run python scripts/invoke_runtime.py --actor cust_marios "first question" "second question"   # timed, one session
agentcore logs --runtime TravelDesk          # runtime logs;  agentcore traces list --runtime TravelDesk
```

Spans from the runtime go to AgentCore Observability (CloudWatch, GenAI observability) through the runtime's
OpenTelemetry instrumentation.

## Run the desk locally

```bash
uv run python agents/travel_desk.py --actor cust_marios
```

The local REPL uses the same Gateway, Cedar and guardrail as the deployed desk. `--actor` stands in for caller
verification. Press Enter on an empty line to quit. Things to try:

- `Find me a flight to London next Friday` (profile lookup first, then a ZZ search from ATH)
- `What's the status of my bookings?`
- `Can I move my first London trip to <date>? What would it cost?` (policy, then a change quote)
- `Cancel booking TYBXUT`, then answer `yes` (quote, confirmation turn, execution)
- `Book a flight to Las Vegas on 2027-01-27`, then `yes` (search, quote, confirmation, booking)
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

On AgentCore Runtime the same spans go to AgentCore Observability (CloudWatch) instead of a file.

### Reviewing traces in a UI

The spans are standard OpenTelemetry with the GenAI conventions (`gen_ai.*`), emitted by Strands itself. To browse
them, set `OTEL_EXPORTER_OTLP_ENDPOINT` and the desk also streams every span to that endpoint (the file keeps
being written). Two local options, both free:

**Arize Phoenix** (recommended: built for agent traces, shows prompts, outputs, tool calls and tokens per step):

```bash
docker compose up -d        # Phoenix from compose.yaml: pinned image, restarts on its own, traces kept in a volume
```

Then set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006` in `.env` and every desk run shows up live at
http://localhost:6006 in the **travel-concierge** project. Arize's OpenInference processor for Strands converts the spans,
so Phoenix shows prompts, answers and tool arguments per step, groups turns into sessions (`session.id`)
and filters by traveler (`user.id`). Upgrade by changing the image tag in `compose.yaml` and running
`docker compose up -d` again. Without Docker: `uvx --from arize-phoenix phoenix serve`.

**Jaeger** (plain timing waterfall, needs Docker):

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/jaeger:2.10.0   # UI on http://localhost:16686
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 uv run python agents/travel_desk.py --actor cust_marios
```

Only runs made with the variable set show up; the JSONL file is in the console exporter's format, which these
tools do not import. Spans are tagged with `session.id`, `actor.id` and `agent.role` for filtering.

## Latency

Same four questions on each setup, Sonnet 5.5, warm session (seconds, total turn time; on the Runtime also the
time to the first streamed words):

| Question | Direct to Duffel (step 3) | Local, via Gateway + Cedar + guardrail | Local, via Gateway, no guardrail | AgentCore Runtime (first words / total) |
|---|---|---|---|---|
| Flight search to London | 5.2 | 8.3 | 6.1 | 1.6 / 6.5 |
| Status of YLNUD2 | 3.2 | 5.8 | 3.7 | 1.5 / 5.0 |
| Refund if I cancel YLNUD2 (Flight Ops quote) | 10.6 | 12.4 | 9.1 | 2.1 / 11.0 |
| Usual seat (from the record) | 1.8 | 2.1 | 1.6 | 1.6 / 2.1 |

- The Gateway adds about 0.2 to 0.3s per warm tool call (Lambda + Cedar): `get_order` 0.5s vs 0.2s direct,
  `search_flights` 1.5s vs 1.3s. A cold Lambda adds 1 to 2s on its first call after idle.
- The guardrail costs about 0.5 to 1s per model call even in async mode: the input is still checked before the
  model starts. That is most of the gap between the two local columns.
- The first turn of a session pays for setting it up (two STS role assumptions and MCP handshakes, the traveler
  record from the Gateway and Memory): about 3s locally and 10s on a fresh Runtime microVM. Both Gateway sessions
  are opened in the background while the record loads.
- Possible next steps: provisioned concurrency for the Lambda (removes cold starts, costs per hour), apply the
  guardrail to output only on read turns, and keep a warm Runtime session per caller.

## Checks

Each module with non trivial logic has an assert based self check that needs no network and no AWS:

```bash
uv run python agents/common.py            # confirmation gate
uv run python tools/duffel.py             # Duffel normalizers
uv run python agents/traveler_profile.py  # history summary
uv run python tools/lambda_handler.py     # Gateway Lambda: ownership, 24h rule, dispatch
uv run python scripts/sync_policies.py --check   # Cedar files and agentcore.json agree
```

Against the deployed stack: `uv run python scripts/check_gateway_policies.py` (Cedar and Lambda guardrails, see
[Deploy](#deploy-agentcore)).

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
agents/                runtime code (deployed as a CodeZip)
  travel_desk.py       desk agent + local REPL
  runtime_app.py       AgentCore Runtime entrypoint
  flight_ops.py        model facing tools (thin Gateway calls) + Flight Ops agent for writes
  traveler_profile.py  traveler record: bookings via Gateway + Memory records (plain code, loaded per session)
  policy.py            desk policy rules (plain text, in the desk prompt)
  common.py            models + guardrail, tracing, session, Gateway client, answer cache, confirmation gate
  pyproject.toml       runtime dependencies
tools/
  lambda_handler.py    Gateway Lambda target "Duffel": all tools, ownership, 24h rule
  duffel.py            Duffel client + normalizers
policies/              Cedar policies (reads, writes, forbid_desk_writes)
agentcore/
  agentcore.json       Memory, Gateway + Lambda target + tool schemas, policy engine, Runtime
  cdk/lib/cdk-stack.ts Gateway caller roles, Bedrock Guardrail, runtime permissions
  .cli/deployed-state.json  deployed resource ids (committed)
scripts/
  seed_duffel_history.py    book past orders (tagged with actor_id), write the fixture
  seed_memory_sessions.py   seed sessions, measure recall and isolation
  prepare_gateway.py        Duffel token -> Secrets Manager, tag orders with actor_id
  sync_policies.py          policies/*.cedar -> agentcore.json
  check_gateway_policies.py live Cedar + Lambda checks
  invoke_runtime.py         timed conversation with the deployed desk
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
- Policy rules are inline text in `agents/policy.py`, not a knowledge base yet; the guardrail has no grounding check.
- One caller per process (one per microVM on Runtime). The session is a module level dict.
- Confirmation is detected with a regex on the start of the user's message.
- The traveler is asserted by the caller of the Runtime (`X-Actor-Id`), not proven by a token. The upgrade is
  CUSTOM_JWT inbound auth with the traveler's verified identity as a claim.
- `list_orders` lists the whole Duffel test account and filters by `metadata.actor_id`.
- Gateway role credentials are 1 hour STS credentials, not refreshed within a session.
- Resource ids (Gateway URL, roles, guardrail) are copied into `.env` and the runtime `envVars` by hand.
- The profile lists all of a traveler's Memory records rather than searching them, which is fine at about
  60 records per traveler.

## Roadmap

1. ~~Duffel test account + seeded history~~
2. ~~Strands agents locally, Duffel direct~~
3. ~~AgentCore Memory (UserPreference, Semantic, Summary) + session manager + seeded sessions~~
4. ~~Duffel tools behind AgentCore Gateway, Cedar (default deny, writes only for Flight Ops), Bedrock Guardrails,
   deploy to Runtime~~
5. Eval dataset (~150 cases) + ground truth
6. AgentCore Evaluations + Strands Decider calibration + CloudWatch dashboard
