"""Create synthetic past orders in Duffel test mode for 3 travelers.

Usage: DUFFEL_TEST_TOKEN=duffel_test_... uv run python scripts/seed_duffel_history.py
Writes evals/fixtures/history.json: {actor_id: [order summaries]} for the Traveler Profile agent.
Only Duffel Airways (ZZ) offers are booked so results are reproducible.
"""
import json, os, sys, urllib.request, datetime as dt
from pathlib import Path

TOKEN = os.environ.get("DUFFEL_TEST_TOKEN", "")
assert TOKEN.startswith("duffel_test_"), "set DUFFEL_TEST_TOKEN to a Duffel *test* token"
API = "https://api.duffel.com/air"

TRAVELERS = {
    "cust_marios": dict(given="Marios", family="Papadakis", born="1983-03-14", email="marios@example.com",
                        trips=[("ATH", "LHR", 2), ("ATH", "JFK", 2), ("ATH", "CDG", 1), ("ATH", "LHR", 1)]),
    "cust_eleni":  dict(given="Eleni", family="Georgiou", born="1990-07-02", email="eleni@example.com",
                        trips=[("SKG", "MUC", 2), ("ATH", "FCO", 1), ("ATH", "AMS", 2)]),
    "cust_nikos":  dict(given="Nikos", family="Antoniou", born="1975-11-20", email="nikos@example.com",
                        trips=[("ATH", "DXB", 1), ("ATH", "JFK", 1), ("ATH", "LHR", 2), ("ATH", "BCN", 1)]),
}


def call(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}", method=method,
        data=json.dumps({"data": body}).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Duffel-Version": "v2",
                 "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)["data"]


def book(t, origin, dest, days_ahead):
    date = (dt.date.today() + dt.timedelta(days=days_ahead)).isoformat()
    req = call("POST", "/offer_requests?return_offers=true", {
        "slices": [{"origin": origin, "destination": dest, "departure_date": date}],
        "passengers": [{"type": "adult"}], "cabin_class": "economy"})
    offers = [o for o in req["offers"] if o["owner"]["iata_code"] == "ZZ"]
    if not offers:
        return None
    offer, pax_id = offers[0], req["passengers"][0]["id"]
    order = call("POST", "/orders", {
        "type": "instant", "selected_offers": [offer["id"]],
        "payments": [{"type": "balance", "amount": offer["total_amount"], "currency": offer["total_currency"]}],
        "passengers": [{"id": pax_id, "title": "mr", "gender": "m", "given_name": t["given"],
                        "family_name": t["family"], "born_on": t["born"], "email": t["email"],
                        "phone_number": "+306900000000"}]})
    seg = order["slices"][0]["segments"][0]
    return dict(order_id=order["id"], booking_reference=order["booking_reference"], origin=origin, destination=dest,
                departure=seg["departing_at"], airline=seg["marketing_carrier"]["name"], cabin="economy",
                total=order["total_amount"], currency=order["total_currency"])


def main():
    out, day = {}, 7  # ponytail: "history" is future dated; Duffel test mode cannot book the past. Upgrade: shift dates in the fixture when presenting to the LLM.
    for actor, t in TRAVELERS.items():
        out[actor] = []
        for origin, dest, n in t["trips"]:
            for _ in range(n):
                o = book(t, origin, dest, day); day += 3
                if o:
                    out[actor].append(o); print(actor, origin, dest, o["booking_reference"])
    path = Path("evals/fixtures/history.json"); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    assert all(out.values()), "a traveler ended up with zero orders; check ZZ availability"
    print("wrote", path, sum(map(len, out.values())), "orders")


if __name__ == "__main__":
    sys.exit(main())
