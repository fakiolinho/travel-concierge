"""Duffel (test mode) client + normalizers. Only norm_* output is ever handed to an LLM, never raw Duffel JSON."""
import json, os, urllib.error, urllib.request

API = "https://api.duffel.com/air"


class DuffelError(Exception):
    pass


def call(method, path, body=None):
    # ponytail: token from env for the local run. Upgrade: Lambda reads it from Secrets Manager (step 4).
    token = os.environ.get("DUFFEL_TEST_TOKEN", "")
    if not token.startswith("duffel_test_"):
        raise DuffelError("DUFFEL_TEST_TOKEN missing or not a test token")
    req = urllib.request.Request(f"{API}{path}", method=method,
        data=json.dumps({"data": body}).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Duffel-Version": "v2",
                 "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["data"]
    except urllib.error.HTTPError as e:
        try:
            msg = "; ".join(x.get("message", "") for x in json.load(e)["errors"])
        except Exception:
            msg = e.reason
        raise DuffelError(f"Duffel {e.code}: {msg}") from None


def _seg(s):
    c = s["marketing_carrier"]
    return {"from": s["origin"]["iata_code"], "to": s["destination"]["iata_code"],
            "departing_at": s["departing_at"], "arriving_at": s["arriving_at"],
            "flight": f'{c["iata_code"]}{s["marketing_carrier_flight_number"]}', "carrier": c["name"]}


def _slice(sl):
    return {"slice_id": sl.get("id"), "origin": sl["origin"]["iata_code"], "destination": sl["destination"]["iata_code"],
            "duration": sl.get("duration"), "segments": [_seg(s) for s in sl["segments"]]}


def _conditions(c):
    out = {}
    for src, name in (("change_before_departure", "change"), ("refund_before_departure", "refund")):
        v = (c or {}).get(src)
        out[name] = None if v is None else {"allowed": v["allowed"], "penalty": v.get("penalty_amount"),
                                            "currency": v.get("penalty_currency")}
    return out


def norm_offer(o):
    return {"offer_id": o["id"], "airline": o["owner"]["iata_code"], "total": o["total_amount"],
            "currency": o["total_currency"], "expires_at": o.get("expires_at"),
            "slices": [_slice(s) for s in o["slices"]], "conditions": _conditions(o.get("conditions"))}


def norm_order(o):
    return {"order_id": o["id"], "booking_reference": o["booking_reference"],
            "status": "cancelled" if o.get("cancelled_at") else "confirmed",
            "total": o["total_amount"], "currency": o["total_currency"],
            "slices": [_slice(s) for s in o["slices"]],
            "passengers": [f'{p["given_name"]} {p["family_name"]}' for p in o.get("passengers", [])],
            "services": [{"type": s["type"], "quantity": s.get("quantity"), "total": s.get("total_amount")}
                         for s in o.get("services") or []],
            "conditions": _conditions(o.get("conditions"))}


def norm_change_offer(c):
    return {"change_offer_id": c["id"], "change_total": c["change_total_amount"], "currency": c["change_total_currency"],
            "penalty": c.get("penalty_total_amount"), "new_total": c.get("new_total_amount"),
            "expires_at": c.get("expires_at"), "new_slices": [_slice(s) for s in c["slices"]["add"]]}


def norm_bag(s):
    m = s.get("metadata") or {}
    return {"service_id": s["id"], "kind": m.get("type"), "max_weight_kg": m.get("maximum_weight_kg"),
            "price": s["total_amount"], "currency": s["total_currency"], "max_quantity": s.get("maximum_quantity")}


def norm_seat_map(m, limit=30):
    seats = []
    for cabin in m.get("cabins", []):
        for row in cabin.get("rows", []):
            for sec in row.get("sections", []):
                for el in sec.get("elements", []):
                    if el.get("type") == "seat" and el.get("available_services"):
                        sv = el["available_services"][0]
                        seats.append({"designator": el["designator"], "price": sv["total_amount"],
                                      "currency": sv["total_currency"], "service_id": sv["id"]})
    # ponytail: first N available seats only, keeps the prompt small. Upgrade: filter by window/aisle/price args.
    return {"segment_id": m.get("segment_id"), "available": len(seats), "seats": seats[:limit]}


if __name__ == "__main__":
    place = lambda code: {"iata_code": code, "name": "secret raw field"}
    seg = {"origin": place("ATH"), "destination": place("LHR"), "departing_at": "2026-11-01T10:00:00",
           "arriving_at": "2026-11-01T12:30:00", "marketing_carrier_flight_number": "123",
           "marketing_carrier": {"iata_code": "ZZ", "name": "Duffel Airways", "logo_url": "x"}}
    sl = {"id": "sli_1", "origin": place("ATH"), "destination": place("LHR"), "duration": "PT3H30M", "segments": [seg]}
    order = {"id": "ord_1", "booking_reference": "ABC123", "cancelled_at": None, "total_amount": "120.50",
             "total_currency": "EUR", "slices": [sl], "passengers": [{"given_name": "A", "family_name": "B"}],
             "conditions": {"refund_before_departure": {"allowed": True, "penalty_amount": "20.00",
                                                        "penalty_currency": "EUR"}}, "owner": {"iata_code": "ZZ"}}
    n = norm_order(order)
    assert n["slices"][0]["segments"][0]["flight"] == "ZZ123" and n["total"] == "120.50" and n["status"] == "confirmed"
    assert n["conditions"] == {"change": None, "refund": {"allowed": True, "penalty": "20.00", "currency": "EUR"}}
    assert "secret raw field" not in json.dumps(n) and "logo_url" not in json.dumps(n)
    assert norm_order({**order, "cancelled_at": "2026-10-01"})["status"] == "cancelled"
    o = norm_offer({**order, "id": "off_1", "expires_at": "t"})
    assert o["offer_id"] == "off_1" and o["airline"] == "ZZ"
    el = lambda d, svc: {"type": "seat", "designator": d, "available_services": svc}
    sm = norm_seat_map({"segment_id": "seg_1", "cabins": [{"rows": [{"sections": [{"elements": [
        el("1A", [{"id": "ase_1", "total_amount": "15.00", "total_currency": "EUR"}]), el("1B", []),
        {"type": "exit_row"}]}]}]}]})
    assert sm == {"segment_id": "seg_1", "available": 1,
                  "seats": [{"designator": "1A", "price": "15.00", "currency": "EUR", "service_id": "ase_1"}]}
    print("duffel: ok")
