"""AgentCore Gateway Lambda target "TripIntel": airport weather, destination forecast, public holidays, FX rates.

A separate target from "Duffel" so each Lambda holds only what it needs and Cedar names its tools apart
(TripIntel___airport_weather, ...). All sources are free and keyless:
- aviationweather.gov (NOAA) METAR / TAF: worldwide airport observations and forecasts. Max 100 requests/min,
  documented params only (ids, format), custom User-Agent requested. Cached per airport for 10 minutes.
- Open-Meteo: daily forecast for any coordinates, today to 15 days ahead.
- Nager.Date: public holidays per country (busy travel days).
- Frankfurter: ECB reference exchange rates (working days, ~16:00 CET).
- Duffel places: IATA airport -> ICAO, coordinates, country (needs the Duffel token, read from Secrets Manager).
Every response is normalized to a compact schema here; no model sees raw API JSON.
"""

import datetime as dt, json, os, time, urllib.parse, urllib.request
from decimal import ROUND_HALF_UP, Decimal

import duffel

UA = "travel-concierge/0.1 (+github.com/fakiolinho/travel-concierge)"  # requested by aviationweather.gov
WX_TTL = 600  # seconds; METARs update about hourly, TAFs a few times a day
FORECAST_DAYS = 16  # Open-Meteo: today + 15
SECRET_ID = os.environ.get("DUFFEL_SECRET_ID", "travel-concierge/duffel-test-token")

# ponytail: caches live in the Lambda container (lost on cold start, not shared). Upgrade: DynamoDB/ElastiCache
# with TTL if traffic grows towards aviationweather.gov's 100 req/min.
_wx_cache, _airports = {}, {}


def _get(url, params):
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r) if r.status == 200 else None


def airport(iata):
    """IATA -> {iata, icao, name, city, country, lat, lon, time_zone}, from Duffel's place search."""
    iata = iata.strip().upper()
    if iata not in _airports:
        if not os.environ.get("DUFFEL_TEST_TOKEN"):
            import boto3

            os.environ["DUFFEL_TEST_TOKEN"] = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)[
                "SecretString"
            ]
        places = duffel.call("GET", f"/places/suggestions?query={iata}", base="https://api.duffel.com")
        a = next((p for p in places if p.get("type") == "airport" and p.get("iata_code") == iata), None)
        if not a or not a.get("icao_code"):
            raise ValueError(f"unknown airport {iata}")
        _airports[iata] = {
            "iata": iata,
            "icao": a["icao_code"],
            "name": a.get("name"),
            "city": a.get("city_name"),
            "country": a.get("iata_country_code"),
            "lat": a.get("latitude"),
            "lon": a.get("longitude"),
            "time_zone": a.get("time_zone"),
        }
    return _airports[iata]


def _vis_sm(v):
    """aviationweather.gov visibility: a number in statute miles or a string like '6+' / '1/2'."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).rstrip("+")
    if " " in v:  # "1 1/2"
        whole, frac = v.split(" ", 1)
        return float(whole) + _vis_sm(frac)
    if "/" in v:
        n, d = v.split("/")
        return float(n) / float(d)
    return float(v)


def _ceiling_ft(clouds, vert_vis=None):
    """Lowest broken, overcast or obscured layer, the aviation definition of ceiling. None = no ceiling."""
    bases = [c["base"] for c in clouds or [] if c.get("cover") in ("BKN", "OVC", "OVX") and c.get("base") is not None]
    if vert_vis is not None:
        bases.append(vert_vis)
    return min(bases) if bases else None


def flight_category(ceiling_ft, vis_sm):
    """FAA flight categories, from the worse of ceiling and visibility."""
    c = float("inf") if ceiling_ft is None else ceiling_ft
    v = float("inf") if vis_sm is None else vis_sm
    if c < 500 or v < 1:
        return "LIFR"
    if c < 1000 or v < 3:
        return "IFR"
    if c <= 3000 or v <= 5:
        return "MVFR"
    return "VFR"


SEVERE_WX = ("TS", "FZ", "SN", "GR", "GS", "PL", "SQ", "FC", "+")  # thunder, freezing, snow, hail, squall, heavy


def signals(category, wind_kt, gust_kt, wx):
    """Plain rules that flag disruption risk, so the agent cites evidence instead of guessing. Level is the worst
    signal: high = likely delays/cancellations, medium = possible delays, low = nothing notable."""
    out = []
    if category == "LIFR":
        out.append(("high", "LIFR: ceiling under 500 ft or visibility under 1 mile"))
    elif category == "IFR":
        out.append(("medium", "IFR: low cloud or reduced visibility, reduced arrival rates likely"))
    w = max(wind_kt or 0, gust_kt or 0)
    if w >= 35:
        out.append(("high", f"wind/gusts {w} kt"))
    elif w >= 25:
        out.append(("medium", f"wind/gusts {w} kt"))
    for code in (wx or "").split():
        if any(s in code for s in SEVERE_WX):
            level = "high" if any(s in code for s in ("TS", "FZ", "GR", "SN", "+SN")) else "medium"
            out.append((level, f"weather {code}"))
    level = "high" if any(l == "high" for l, _ in out) else "medium" if out else "low"
    return level, [f"{l}: {why}" for l, why in out]


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="minutes") if epoch else None


def _cached(kind, icao, fetch):
    hit = _wx_cache.get((kind, icao))
    if hit and time.time() - hit[0] < WX_TTL:
        return hit[1]
    data = fetch()
    _wx_cache[(kind, icao)] = (time.time(), data)
    return data


def airport_weather(airport_code, **_):
    """Current observation (METAR) at an airport, with flight category and disruption signals."""
    a = airport(airport_code)
    rows = _cached("metar", a["icao"], lambda: _get("https://aviationweather.gov/api/data/metar",
                                                     {"ids": a["icao"], "format": "json"}))  # fmt: skip
    if not rows:
        return {"airport": a["iata"], "icao": a["icao"], "available": False, "reason": "no current observation"}
    m = rows[0]
    ceiling, vis = _ceiling_ft(m.get("clouds"), m.get("vertVis")), _vis_sm(m.get("visib"))
    cat = m.get("fltCat") or flight_category(ceiling, vis)
    level, why = signals(cat, m.get("wspd"), m.get("wgst"), m.get("wxString"))
    return {
        "airport": a["iata"],
        "icao": a["icao"],
        "name": a["name"],
        "observed_at": _iso(m.get("obsTime")),
        "flight_category": cat,
        "wind_kt": m.get("wspd"),
        "gust_kt": m.get("wgst"),
        "visibility_sm": vis,
        "ceiling_ft": ceiling,
        "weather": m.get("wxString"),
        "temp_c": m.get("temp"),
        "risk": level,
        "signals": why,
        "raw": m.get("rawOb"),
    }


def airport_forecast(airport_code, **_):
    """Terminal forecast (TAF, usually 24 to 30 hours) at an airport, period by period, with the worst risk."""
    a = airport(airport_code)
    rows = _cached("taf", a["icao"], lambda: _get("https://aviationweather.gov/api/data/taf",
                                                   {"ids": a["icao"], "format": "json"}))  # fmt: skip
    if not rows:
        return {"airport": a["iata"], "icao": a["icao"], "available": False,
                "reason": "no TAF for this airport; use destination_forecast"}  # fmt: skip
    t = rows[0]
    periods, worst = [], "low"
    for f in t.get("fcsts", []):
        ceiling, vis = _ceiling_ft(f.get("clouds"), f.get("vertVis")), _vis_sm(f.get("visib"))
        cat = flight_category(ceiling, vis)
        level, why = signals(cat, f.get("wspd"), f.get("wgst"), f.get("wxString"))
        if f.get("probability") and level != "low":  # PROB30/40: possible, not expected
            why = [f"{w} (probability {f['probability']}%)" for w in why]
        worst = max(worst, level, key=["low", "medium", "high"].index)
        periods.append({
            "from": _iso(f.get("timeFrom")), "to": _iso(f.get("timeTo")), "change": f.get("fcstChange"),
            "probability": f.get("probability"), "wind_kt": f.get("wspd"), "gust_kt": f.get("wgst"),
            "visibility_sm": vis, "ceiling_ft": ceiling, "weather": f.get("wxString"),
            "flight_category": cat, "risk": level, "signals": why,
        })  # fmt: skip
    return {
        "airport": a["iata"],
        "icao": a["icao"],
        "issued_at": t.get("issueTime"),
        "valid_from": _iso(t.get("validTimeFrom")),
        "valid_to": _iso(t.get("validTimeTo")),
        "risk": worst,
        "periods": periods,
        "raw": t.get("rawTAF"),
    }


WMO = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
       73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
       82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}  # fmt: skip


def forecast_window(date, today):
    """None if Open-Meteo has a daily forecast for `date`, else why not (date is in the past or too far ahead)."""
    d, last = dt.date.fromisoformat(date), today + dt.timedelta(days=FORECAST_DAYS - 1)
    if d < today:
        return f"{date} is in the past; forecasts cover {today} to {last}"
    if d > last:
        return f"no forecast yet: forecasts reach {last}; {date} becomes available from {d - dt.timedelta(days=FORECAST_DAYS - 1)}"
    return None


def destination_forecast(airport_code, date, **_):
    """Daily weather forecast at an airport's location for one date (local day), today to 15 days ahead."""
    a = airport(airport_code)
    if why := forecast_window(date, dt.datetime.now(dt.timezone.utc).date()):
        return {"airport": a["iata"], "date": date, "available": False, "reason": why}
    d = _get(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": a["lat"],
            "longitude": a["lon"],
            "timezone": "auto",
            "start_date": date,
            "end_date": date,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max",
        },
    )["daily"]
    code = d["weather_code"][0]
    return {
        "airport": a["iata"],
        "city": a["city"],
        "date": date,
        "available": True,
        "summary": WMO.get(code, f"WMO code {code}"),
        "temp_max_c": d["temperature_2m_max"][0],
        "temp_min_c": d["temperature_2m_min"][0],
        "precipitation_mm": d["precipitation_sum"][0],
        "precipitation_probability_pct": d["precipitation_probability_max"][0],
        "wind_max_kmh": d["wind_speed_10m_max"][0],
        "gust_max_kmh": d["wind_gusts_10m_max"][0],
    }


def public_holidays(airport_code, start_date, end_date, **_):
    """Public holidays in the airport's country between two dates (busy travel days)."""
    a = airport(airport_code)
    start, end = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    if (end - start).days > 400 or end < start:
        raise ValueError("date range must be 0 to 400 days")
    out = []
    for year in range(start.year, end.year + 1):
        req = urllib.request.Request(
            f"https://date.nager.at/api/v3/PublicHolidays/{year}/{a['country']}", headers={"User-Agent": UA}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            out += [h for h in json.load(r) if start <= dt.date.fromisoformat(h["date"]) <= end]
    return {
        "country": a["country"],
        "from": start_date,
        "to": end_date,
        "holidays": [{"date": h["date"], "name": h["name"], "nationwide": h.get("global", True)} for h in out],
    }


def convert(amount, rate):
    return str((Decimal(str(amount)) * Decimal(str(rate))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fx_convert(amount, from_currency, to_currency, **_):
    """Convert an amount with the latest ECB reference rate (indicative, not what a card issuer charges)."""
    f, t = from_currency.upper(), to_currency.upper()
    if f == t:
        return {"amount": str(amount), "from": f, "to": t, "rate": "1", "converted": str(amount)}
    d = _get("https://api.frankfurter.dev/v1/latest", {"base": f, "symbols": t})
    rate = d["rates"][t]
    return {"amount": str(amount), "from": f, "to": t, "rate": str(rate), "converted": convert(amount, rate),
            "rate_date": d["date"], "source": "ECB reference rate via Frankfurter (indicative)"}  # fmt: skip


TOOLS = {f.__name__: f for f in (airport_weather, airport_forecast, destination_forecast, public_holidays, fx_convert)}


def handler(event, context):
    """Gateway passes the tool arguments as the event and the tool as 'TripIntel___<tool>' in client context."""
    name = context.client_context.custom["bedrockAgentCoreToolName"].split("___", 1)[-1]
    try:
        return TOOLS[name](**event)
    except (duffel.DuffelError, ValueError, KeyError, OSError) as e:
        # Bad input or an upstream API down: return it as data so the agent says so instead of guessing.
        return {"error": str(e) if not isinstance(e, KeyError) else f"unknown tool or missing field: {e}"}


if __name__ == "__main__":
    assert _vis_sm("6+") == 6 and _vis_sm("1/2") == 0.5 and _vis_sm("1 1/2") == 1.5 and _vis_sm(4.35) == 4.35
    assert _ceiling_ft([{"cover": "FEW", "base": 400}, {"cover": "BKN", "base": 1200}]) == 1200
    assert _ceiling_ft([{"cover": "SCT", "base": 300}]) is None and _ceiling_ft([], vert_vis=200) == 200
    assert [flight_category(c, v) for c, v in [(None, 10), (2500, 10), (800, 10), (None, 2), (300, 10)]] == [
        "VFR", "MVFR", "IFR", "IFR", "LIFR",
    ]  # fmt: skip
    assert signals("VFR", 8, None, None) == ("low", [])
    assert signals("VFR", 20, 28, "-RA")[0] == "medium"
    assert signals("IFR", 10, None, "TSRA")[0] == "high" and signals("LIFR", 5, None, "FG")[0] == "high"
    today = dt.date(2026, 10, 18)
    assert forecast_window("2026-10-18", today) is None and forecast_window("2026-11-02", today) is None
    assert "no forecast yet" in forecast_window("2026-11-03", today)
    assert "from 2027-01-12" in forecast_window("2027-01-27", today), "says when the date becomes available"
    assert "past" in forecast_window("2026-10-17", today)
    assert convert("402.26", 1.1253) == "452.66" and convert(100, "0.84385") == "84.39"
    _airports["XXX"] = {"iata": "XXX", "icao": "KXXX", "name": "Test", "city": "T", "country": "US"}
    _wx_cache[("metar", "KXXX")] = (time.time(), [{"fltCat": "IFR", "wspd": 30, "wgst": 41, "wxString": "TSRA",
                                                    "visib": 2, "clouds": [{"cover": "OVC", "base": 900}]}])  # fmt: skip
    w = airport_weather("xxx")
    assert w["risk"] == "high" and w["ceiling_ft"] == 900 and len(w["signals"]) == 3, w

    class Ctx:
        class client_context:
            custom = {"bedrockAgentCoreToolName": "TripIntel___nope"}

    assert "unknown tool" in handler({}, Ctx)["error"]
    print("trip_intel: ok", sorted(TOOLS))
