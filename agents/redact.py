"""PCI and PII masking for exported traces (TRACE_PII_MASK=on; set for the deployed desk in agentcore.json).

Strands' own switch (OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes=) is all or nothing: every message
becomes [REDACTED], which also blinds AgentCore online evaluation, and it skips the memory.query/memory.content
events. This processor instead masks only the sensitive values in every string attribute and event attribute of
every span, before any exporter (CloudWatch, OTLP, the local file) sees it:
- PCI: card numbers (13 to 19 digits, Luhn valid), CVV/CVC, card expiry.
- PII: passport numbers, emails, phone numbers, dates of birth, and passenger fields from Duffel (given_name,
  family_name, born_on, email, phone_number, `passengers` lists). Passenger names seen in a tool result are also
  masked wherever they appear later in the process (the desk's answer, Memory summaries).
Booking references, order ids, prices and traveler ids (cust_...) stay: they are what traces and evals are for.

ponytail: patterns, so a name or address typed in free text by the caller is not caught unless a tool result named
it first. Upgrade: Amazon Comprehend DetectPiiEntities (or the Bedrock guardrail with ANONYMIZE) in the exporter.
"""

import re

from opentelemetry.sdk.trace import Event, SpanProcessor

LABELS = r"(?:passport|passeport|reisepass\w*|pasaporte|διαβατ\w*)"
PATTERNS = [
    # Labelled values first, so their digits are not left for the generic rules.
    (re.compile(r"(?i)\b(cvv2?|cvc2?|cid|security code|sicherheitscode|cryptogramme)(\W{0,12})\d{3,4}\b"), r"\1\2[CVV]"),
    (re.compile(r"(?i)\b(exp\w*|valid thru|gültig bis|ablauf\w*|λήξη)(\W{0,12})(0[1-9]|1[0-2])\s?[/-]\s?\d{2,4}\b"),
     r"\1\2[EXPIRY]"),
    (re.compile(rf"(?i)\b({LABELS})(\D{{0,20}}?)\b[A-Z]{{0,2}}\d{{6,9}}\b"), r"\1\2[PASSPORT]"),
    (re.compile(r"(?i)\b(born on|date of birth|dob|geburtsdatum|né le|née le|ημερομηνία γέννησης)(\W{0,12})"
                r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b"), r"\1\2[DOB]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"(?<![\w+])(?:\+|00)\d(?:[\s().-]?\d){7,14}\b"), "[PHONE]"),
    (re.compile(r"(?i)\b(phone|tel|mobile|telefon|téléphone|τηλ\w*)(\W{0,12})\d(?:[\s().-]?\d){7,14}\b"), r"\1\2[PHONE]"),
    # Duffel passenger fields, in plain or escaped (JSON inside a JSON string) form.
    (re.compile(r'(\\*"(?:given_name|family_name|born_on|email|phone_number)\\*"\s*:\s*\\*")[^"\\]*'), r"\1[PII]"),
]  # fmt: skip
CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
PASSENGERS = re.compile(r'(\\*"passengers\\*"\s*:\s*\[)([^\]]*)\]')
NAME = re.compile(r'\\*"([^"\\]{2,80})\\*"')


def luhn(digits):
    total = 0
    for i, d in enumerate(int(c) for c in reversed(digits)):
        total += d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2)
    return total % 10 == 0


NAMES = set()  # passenger names known in this process; shared by every Masker


def learn(full_names):
    """Mask these passenger names (and their parts) from now on, in any text: called with the traveler's own names
    when the bookings record loads, before Memory or answers can mention them."""
    for full in full_names:
        NAMES.update({full.strip(), *(part for part in full.split() if len(part) >= 3)} - {""})


class Masker:
    """Masks one string; remembers passenger names from tool results to mask them in later text."""

    def __init__(self):
        self.names = NAMES

    def __call__(self, text):
        for m in PASSENGERS.finditer(text):  # learn names before masking them
            learn(n for n in NAME.findall(m[2]) if n.strip() and n.strip() != "[NAME]")
        text = PASSENGERS.sub(lambda m: f'{m[1]}"[NAME]"]', text)
        for pattern, repl in PATTERNS:
            text = pattern.sub(repl, text)
        text = CARD.sub(lambda m: "[CARD]" if luhn(re.sub(r"\D", "", m[0])) else m[0], text)
        for name in sorted(self.names, key=len, reverse=True):  # "Nikos Antoniou" before "Nikos"
            text = text.replace(name, "[NAME]")
        return text


def mask_value(v, mask):
    if isinstance(v, str):
        return mask(v)
    if isinstance(v, (list, tuple)):
        return type(v)(mask(x) if isinstance(x, str) else x for x in v)
    return v


class PiiMaskProcessor(SpanProcessor):
    """Rewrites a finished span's attributes and events in place. Must run before every exporting processor
    (add_first), the same in place technique as Arize's OpenInference processor."""

    def __init__(self):
        self.mask = Masker()

    def on_end(self, span):
        span._attributes = {k: mask_value(v, self.mask) for k, v in (span._attributes or {}).items()}
        span._events = [
            Event(e.name, {k: mask_value(v, self.mask) for k, v in (e.attributes or {}).items()}, e.timestamp)
            for e in span._events
        ]


def add_first(provider, processor):
    """Put `processor` ahead of the provider's existing ones (on the Runtime, the ADOT exporter is registered by the
    time our code is imported), so nothing is exported before it masked the span."""
    multi = provider._active_span_processor
    with multi._lock:
        multi._span_processors = (processor, *multi._span_processors)


if __name__ == "__main__":
    m = Masker()
    assert m("card 4111 1111 1111 1111 exp 12/27 cvv 123") == "card [CARD] exp [EXPIRY] cvv [CVV]"
    assert m("order total 1234567890123 EUR") == "order total 1234567890123 EUR"  # not Luhn: not a card
    assert m("Mein Reisepass ist AB1234567, Mail a.b@example.com") == "Mein Reisepass ist [PASSPORT], Mail [EMAIL]"
    assert m("call +30 694 123 4567 or phone: 2310 123456") == "call [PHONE] or phone: [PHONE]"
    assert (
        m("dob 1985-03-02, flight 2026-12-05 03:05, total 98.98") == "dob [DOB], flight 2026-12-05 03:05, total 98.98"
    )
    raw = '{"order_id":"ord_1","booking_reference":"YZMRYU","passengers":["Nikos Antoniou"],"total":"98.98"}'
    assert m(raw) == '{"order_id":"ord_1","booking_reference":"YZMRYU","passengers":["[NAME]"],"total":"98.98"}'
    assert m("The booking is for Nikos Antoniou.") == "The booking is for [NAME]."  # learned from the tool result
    assert m('{\\"given_name\\": \\"Eleni\\", \\"born_on\\": \\"1990-01-01\\"}') == (
        '{\\"given_name\\": \\"[PII]\\", \\"born_on\\": \\"[PII]\\"}'
    )
    assert m("ord_0000BBW6ouPKAPHggbobGC at 2026-10-18T20:56:41Z, cust_nikos") == (
        "ord_0000BBW6ouPKAPHggbobGC at 2026-10-18T20:56:41Z, cust_nikos"
    )
    learn(["Eleni Georgiou"])
    assert Masker()("The user (Eleni Georgiou) owns a cello; Ms Georgiou prefers window") == (
        "The user ([NAME]) owns a cello; Ms [NAME] prefers window"
    )
    print("redact: ok")
