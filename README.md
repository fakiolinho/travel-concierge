# travel_concierge

A multi agent airline service desk on Amazon Bedrock AgentCore. A Travel Desk orchestrator routes each caller
request to three specialist agents (Flight Ops, Traveler Profile, Policy), books against Duffel in test mode,
and is graded by layered evals. `PLAN.md` has the full design and the step order.

**Status: step 2 of 6.** The agents run locally against Duffel directly. Gateway, Cedar, Guardrails, Memory and
evals come in later steps (see [Roadmap](#roadmap)).

## How it works

```
caller ─► Travel Desk (agents/travel_desk.py)
            ├─► traveler_profile ─► Traveler Profile agent ─► get_history (evals/fixtures/history.json)
            ├─► flight_ops       ─► Flight Ops agent ─► Duffel: search, orders, seats, change/bag options,
            │                                          change_order*, cancel_order*, add_bag*
            └─► policy           ─► Policy agent ─► desk rules + order fare conditions
            * write: quote first, executes only after the traveler says yes in a later turn
```

Each specialist is a Strands `Agent` wrapped in a `@tool` function ("agents as tools"). A fresh specialist is
created for every call, so specialists keep no state between calls. The session (verified actor, turn counter,
pending writes) lives in `agents/common.py`.

What the code enforces, regardless of what the model decides:

| Rule | Where |
|---|---|
| A write runs only after a later user turn whose text starts with an explicit yes | `common.gated` |
| Tools only see orders that belong to the verified actor | `common.owned`, `common.history` |
| The profile tool takes no actor argument, so it cannot read another traveler | `traveler_profile.get_history` |
| Flight Ops refuses until Traveler Profile has been called in the session | `travel_desk.flight_ops` |
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
| `MODEL_ID` | agents | Bedrock model or inference profile id; defaults to `us.anthropic.claude-opus-5-5` |

AWS credentials come from the normal boto3 chain. With SSO:

```bash
aws sso login --profile <your-profile>
export AWS_PROFILE=<your-profile>
```

Check that your account can see the model before the first run. If the id differs, set `MODEL_ID` in `.env`:

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude')].inferenceProfileId"
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

## Checks

Each module with non trivial logic has an assert based self check that needs no network and no AWS:

```bash
uv run python agents/common.py            # confirmation gate
uv run python tools/duffel.py             # Duffel normalizers
uv run python agents/traveler_profile.py  # history summary
```

## Layout

```
agents/
  travel_desk.py       orchestrator + REPL
  flight_ops.py        Duffel read and write tools
  traveler_profile.py  history summary (Memory joins in step 3)
  policy.py            desk rules + fare conditions
  common.py            model factory, session, ownership, confirmation gate
tools/duffel.py        Duffel client + normalizers
scripts/seed_duffel_history.py
evals/fixtures/        generated, gitignored
PLAN.md                design and step order
CLAUDE.md              working rules for this repo
```

Shortcuts are marked `# ponytail:` in the code, each naming its limit and upgrade path:
`grep -rn "ponytail:" agents tools scripts`.

## Known limits (step 2)

- Search returns Duffel Airways (`ZZ`) offers only, so results are reproducible. Schedules and prices are not
  realistic.
- Some test fares offer no extra bags after booking, in which case `bag_options` returns an empty list.
- Policy rules are inline text in `agents/policy.py`, not a knowledge base yet.
- One caller per process. The session is a module level dict.
- Confirmation is detected with a regex on the start of the user's message.

## Roadmap

1. ~~Duffel test account + seeded history~~
2. ~~Strands agents locally, Duffel direct~~
3. AgentCore Memory (UserPreference, Semantic, Summary) + session manager + seeded sessions
4. Duffel tools behind AgentCore Gateway, Cedar (default deny, writes only for Flight Ops), Bedrock Guardrails,
   deploy to Runtime (`agentcore configure -e agents/travel_desk.py && agentcore launch`)
5. Eval dataset (~150 cases) + ground truth
6. AgentCore Evaluations + Strands Decider calibration + CloudWatch dashboard
