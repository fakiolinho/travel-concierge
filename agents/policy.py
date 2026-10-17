"""Desk policy rules. Short enough to sit in the desk's prompt, so no tool call or agent hop is needed.
Order specific fare conditions come with get_order."""

# ponytail: rules inline as text. Upgrade: Bedrock Knowledge Base + Guardrails grounding check (step 4).
RULES = {
    "change": "Voluntary changes follow the order's change condition (change_before_departure). If allowed, the "
    "traveler pays the change_total from the Duffel change quote (penalty plus fare difference). If not "
    "allowed, no voluntary change is possible; offer cancellation under the refund rule instead.",
    "refund": "The refund for a cancellation is exactly the refund_amount in the Duffel cancellation quote. Never "
    "estimate it. If the refund condition is not allowed the order can still be cancelled, with refund 0.",
    "24h": "Flights to or from the US booked at least 7 days before departure may be cancelled for a full refund "
    "within 24 hours of booking. Any cancellation with departure less than 24 hours away is not handled by "
    "this desk: hand the traveler to a human agent.",
    "disruption": "EU261 applies to flights departing the EU, or arriving in the EU on an EU carrier. Arrival delay "
    "of 3h or more, cancellation with under 14 days notice, or denied boarding gives fixed compensation: "
    "EUR 250 up to 1500 km, EUR 400 for intra EU over 1500 km or 1500 to 3500 km, EUR 600 over 3500 km. "
    "Not owed for extraordinary circumstances (weather, ATC, security). Care (meals, hotel) is owed "
    "regardless. The desk quotes this rule; it does not promise a payout.",
}
