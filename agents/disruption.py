"""Disruption agent: weather and disruption risk for a booking or route, and rebooking options when risk is high.

Desk ─► disruption (Strands Agent.as_tool) ─► TripIntel tools (Gateway MCP): airport_weather (METAR), airport_forecast
        (TAF), destination_forecast (Open-Meteo), public_holidays (Nager.Date), and Duffel get_order
                                       └─► flight_ops (Strands agent as tool): read only Flight Ops, get_order and
                                           change_options only, signed as the Disruption identity (Cedar: no writes)
The report comes back to the desk as Strands structured output (JSON). It never changes a booking: any rebooking
goes back through the desk's quote and confirmation with the writing Flight Ops agent.
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

import common, policy as pol

INTEL_TOOLS = ["airport_weather", "airport_forecast", "destination_forecast", "public_holidays"]


class RebookOption(BaseModel):
    change_offer_id: str = Field(description="verbatim from change_options")
    new_departure: str = Field(description="departure date and time of the new flight, verbatim")
    change_total: str = Field(description="amount to pay, verbatim")
    currency: str


class DisruptionReport(BaseModel):
    """What the Disruption agent hands back to the desk."""

    risk: Literal["low", "medium", "high", "unknown"] = Field(
        description="worst risk across the flight's airports; unknown when no forecast covers the flight yet"
    )
    summary: str = Field(description="one or two short plain sentences for the caller")
    evidence: list[str] = Field(
        description="at most 5 short lines; each names the airport, the source (METAR, TAF, forecast) and the value"
    )
    compensation: str = Field(description="one sentence: what the disruption rule means here, no promises")
    rebooking_options: list[RebookOption] = Field(default=[], description="only when risk is high, from flight_ops")


OPS_PROMPT = """You are Flight Ops answering a read only request from the disruption desk. Now is {now} UTC.
You can look up an order (get_order) and price date changes (change_options). You cannot change anything.
Report ids, times and amounts verbatim from the tool results, in a few lines. Never estimate."""

PROMPT = """You are the Disruption desk of an airline contact center. Now is {now} UTC. You judge whether weather
puts a flight at risk, and what the traveler's options are. You never change a booking.

How to work:
- You need the flight's airports and departure time. If the request has them, use them. If it only has an order
  id, look it up with get_order.
- Departure within about 30 hours: airport_weather and airport_forecast for the departure and arrival airports.
- Departure within 15 days: destination_forecast for both airports on the travel date.
- Further out: there is no forecast yet. Say so, give the date it becomes available, and set risk to unknown.
  Never describe weather you did not get from a tool.
- Busy travel days only if asked: public_holidays.
- Risk is the worst level the tools report for the flight's window. Each evidence line names the airport, the
  source (METAR, TAF or forecast) and the value. Quote the signals the tools give.
- High risk: ask flight_ops for change_options to the next day (and the day after if that is still close) and
  return up to 3 options verbatim. The desk will quote and confirm any change with the traveler.
- Compensation: apply the disruption rule below. Weather is an extraordinary circumstance: no fixed compensation,
  care is still owed. Quote the rule, promise nothing.

Disruption rule: {rule}"""


def build(state):
    """The Disruption agent with its read only Flight Ops, as the desk's `disruption` tool. Built once per session;
    as_tool(preserve_context=False) resets both to a clean conversation (and their construction time state) on every
    call. Both sign Gateway calls as the Disruption identity, which Cedar refuses every write."""
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    ops = common.agent(
        "flight_ops_read",
        state,
        system_prompt=OPS_PROMPT.format(now=now),
        tools=common.gateway_tools("disruption", ["get_order", "change_options"]),  # no write exists in this context
    )
    agent = common.agent(
        "disruption",
        state,
        system_prompt=PROMPT.format(now=now, rule=pol.RULES["disruption"]),
        tools=[
            *common.gateway_tools("disruption", [*INTEL_TOOLS, "get_order"]),  # get_order direct: saves an agent hop
            ops.as_tool(
                name="flight_ops",
                description="Read only Flight Ops, for rebooking options when risk is high: priced options to move "
                "an order to other dates. Give the order id and the new date(s). Changes nothing.",
            ),
        ],
        structured_output_model=DisruptionReport,
    )
    return agent.as_tool(
        name="disruption",
        description="Disruption desk: weather and disruption risk for a booking or route (airport observations, "
        "airport forecasts, destination forecast), what the disruption policy means, and rebooking options when "
        "risk is high. Give the order id or booking reference, or the route and date, and the traveler's question. "
        "Returns a JSON report; it never changes a booking.",
    )


if __name__ == "__main__":
    r = DisruptionReport(risk="unknown", summary="s", evidence=["LAS forecast: not available until 2027-01-12"],
                         compensation="quoted rule")  # fmt: skip
    assert r.rebooking_options == [] and r.model_dump(mode="json")["risk"] == "unknown"
    assert set(DisruptionReport.model_json_schema()["properties"]) >= {"risk", "evidence", "rebooking_options"}
    print("disruption: ok")
