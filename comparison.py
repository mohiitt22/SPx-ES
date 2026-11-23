"""Build auditable SPX-to-ES comparisons one expiry series and snapshot at a time."""

from bisect import bisect_left
from collections import Counter
from math import exp, log, sqrt
from statistics import median
from zoneinfo import ZoneInfo

from inputs import CT, NY, UTC, expiry_time, es_settlement, iso_ct, spx_settlement
from model import american_crr, black76, implied_vol

YEAR = 365 * 86400
BUCKETS = ["Put wing ≤ −5%", "Put shoulder −5% to −2%", "Near ATM ±2%",
           "Call shoulder +2% to +5%", "Call wing ≥ +5%"]


def hours_between(a, b):
    return (a - b).total_seconds() / 3600


def active_index(value, size, label):
    try:
        index = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"Choose a valid {label}.") from None
    if not 0 <= index < size:
        raise ValueError(f"Choose a valid {label}.")
    return index


def es_config(key, options):
    expiry, style, source = es_settlement(key)
    override = options.get("es_expiry") or options.get("es_clock")
    expiry = expiry_time(expiry, options.get("es_expiry"), options.get("es_clock"), CT)
    style = options.get("es_style") or style
    return expiry, style, "Expiry override" if override else source


def calendar_dte(expiry, snapshot, zone=NY):
    return (expiry.astimezone(zone).date() - snapshot.astimezone(zone).date()).days if expiry else None


def catalog(data, options):
    es_keys = sorted(data.es)
    es_id = active_index(options.get("es", 0), len(es_keys), "ES contract")
    es_key = es_keys[es_id]
    asof = min(data.es[es_key])
    es_expiry, es_style, es_source = es_config(es_key, options)
    es_series = []
    for i, key in enumerate(es_keys):
        expiry, style, source = es_settlement(key)
        if i == es_id:
            expiry, style, source = es_expiry, es_style, es_source
        es_series.append(dict(id=i, code=key[0], future=key[1], expiry=iso_ct(expiry),
                              dte=calendar_dte(expiry, min(data.es[key]), CT), style=style,
                              source=source, snapshots=len(data.es[key])))

    spx_series = []
    for i, key in enumerate(sorted(data.spx)):
        expiry, source = spx_settlement(key, options.get("marker_mode", "vendor"))
        if str(i) == str(options.get("spx")) and (options.get("spx_expiry") or options.get("spx_clock")):
            expiry = expiry_time(expiry, options.get("spx_expiry"), options.get("spx_clock"), NY)
            source = "Expiry override"
        gap = hours_between(expiry, es_expiry) if es_expiry else None
        spx_series.append(dict(id=i, code=key[0], raw_expiry=key[1], expiry=iso_ct(expiry),
                               source=source, dte=calendar_dte(expiry, asof),
                               gap_hours=gap, vendor_dte=sorted(data.vendor_dtes[key]),
                               snapshots=len(data.spx[key])))
    spx_series.sort(key=lambda x: (x["dte"] < 0, abs(x["gap_hours"]) if x["gap_hours"] is not None else
                                   abs(x["dte"]), x["code"]))
    return dict(es=es_series, spx=spx_series, default_spx=spx_series[0]["id"],
                effective_es=dict(expiry=iso_ct(es_expiry), style=es_style, source=es_source),
                files=dict(spx=data.spx_file, es=data.es_file, futures=data.futures_file),
                inputs=dict(spx_rows=data.total_spx, es_rows=data.total_es,
                            invalid_spx=data.ignored_spx, invalid_es=data.ignored_es, duplicates=data.duplicates,
                            futures_rows=len(data.futures)))


def one_forward(es_quotes):
    values = [q["forward"] for q in es_quotes.values() if q["forward"] and q["forward"] > 0]
    return values[0] if values and max(values) - min(values) <= 1e-8 else None


def spx_reference(spx_quotes):
    values = [q["underlying"] for q in spx_quotes.values() if q["underlying"] and q["underlying"] > 0]
    return median(values) if values else None


def convert_hours(snapshots, zone):
    converted = {}
    for raw, quotes in snapshots.items():
        local = raw if raw.tzinfo else raw.replace(tzinfo=zone)
        ts = local.astimezone(UTC)
        if ts in converted:
            raise ValueError("More than one SPX snapshot resolves to the same instant.")
        converted[ts] = quotes
    return converted


def timing_evidence(spx_raw, es):
    result = []
    for name in ("America/Chicago", "America/New_York", "UTC"):
        spx = convert_hours(spx_raw, ZoneInfo(name))
        points = []
        for ts in sorted(spx.keys() & es.keys()):
            proxy, ref = one_forward(es[ts]), spx_reference(spx[ts])
            if proxy is not None and ref is not None:
                points.append((ts, proxy, ref))
        changes = [(b[1] - a[1]) - (b[2] - a[2])
                   for a, b in zip(points, points[1:]) if hours_between(b[0], a[0]) == 1]
        rmse = sqrt(sum(x * x for x in changes) / len(changes)) if changes else None
        result.append(dict(zone=name, overlap=len(spx.keys() & es.keys()), changes=len(changes), rmse=rmse))
    return result


def surface(quotes, t, rate):
    if t <= 0:
        return None
    df = exp(-rate * t)
    candidates = {k: k + (q["call_price"] - q["put_price"]) / df for k, q in quotes.items()
                  if q["call_price"] is not None and q["call_price"] > 0
                  and q["put_price"] is not None and q["put_price"] > 0}
    candidates = {k: f for k, f in candidates.items() if f > 0}
    if len(candidates) < 3:
        return None
    provisional = spx_reference(quotes) or median(candidates.values())
    near = sorted(candidates, key=lambda k: (abs(k - provisional), k))[:11]
    provisional = median(candidates[k] for k in near)
    near = sorted(candidates, key=lambda k: (abs(k - provisional), k))[:11]
    forward = median(candidates[k] for k in near)
    mad = median(abs(candidates[k] - forward) for k in near)
    nodes = []
    for strike, q in sorted(quotes.items()):
        side = 1 if strike >= forward else -1
        name = "call" if side == 1 else "put"
        price = q[name + "_price"]
        vol = implied_vol(price, forward, strike, t, rate, side) if price and price > 0 else None
        if vol is not None and vol > 0:
            nodes.append(dict(k=log(strike / forward), strike=strike, iv=vol,
                              vendor_iv=q[name + "_iv"], side=name, premium=price))
    if len(nodes) < 2:
        return None
    return dict(forward=forward, pairs=len(near), parity_mad=mad,
                parity_span=max(candidates[k] for k in near) - min(candidates[k] for k in near),
                discount=df, k_min=nodes[0]["k"], k_max=nodes[-1]["k"], nodes=nodes)


def interpolate(k, nodes, xs, max_gap):
    i = bisect_left(xs, k)
    for j in (i, i - 1):
        if 0 <= j < len(xs) and abs(xs[j] - k) < 1e-12:
            node = nodes[j]
            return node["iv"], node["strike"], node["strike"], 0.0, "Matched"
    if i == 0 or i == len(xs):
        return None, None, None, None, "Outside SPX strike coverage"
    a, b = nodes[i - 1], nodes[i]
    gap = b["k"] - a["k"]
    if gap > max_gap:
        return None, a["strike"], b["strike"], gap, "SPX strike bracket too wide"
    w = (k - a["k"]) / gap
    return sqrt((1 - w) * a["iv"] ** 2 + w * b["iv"] ** 2), a["strike"], b["strike"], gap, "Matched"


def bucket(k):
    if k <= -0.05:
        return BUCKETS[0]
    if k <= -0.02:
        return BUCKETS[1]
    if k < 0.02:
        return BUCKETS[2]
    if k < 0.05:
        return BUCKETS[3]
    return BUCKETS[4]


def summary(rows):
    matched = [q for q in rows if q["gap"] is not None]
    n = len(matched)
    return dict(total=len(rows), matched=n,
                valid_es=sum(q["es_iv"] is not None for q in rows),
                median_gap=median(q["gap"] for q in matched) if n else None,
                mae=sum(abs(q["gap"]) for q in matched) / n if n else None,
                price_mae=sum(abs(q["price_gap"]) for q in matched) / n if n else None,
                in_spread=sum(q["within"] for q in matched) / n if n else None)


def compare(data, options):
    es_key = sorted(data.es)[active_index(options.get("es", 0), len(data.es), "ES contract")]
    es_expiry, style, es_source = es_config(es_key, options)
    if not es_expiry or style not in ("european", "american"):
        raise ValueError("Set the ES expiry date, time and exercise style in model settings.")
    info = catalog(data, options)
    spx_id = active_index(options.get("spx", info["default_spx"]), len(data.spx), "SPX series")
    spx_key = sorted(data.spx)[spx_id]
    spx_expiry, spx_source = spx_settlement(spx_key, options.get("marker_mode", "vendor"))
    if options.get("spx_expiry") or options.get("spx_clock"):
        spx_expiry = expiry_time(spx_expiry, options.get("spx_expiry"), options.get("spx_clock"), NY)
        spx_source = "Expiry override"
    zone = ZoneInfo(options.get("spx_zone", "America/Chicago"))
    annual_rate = float(options.get("rate", 4.29)) / 100
    max_gap = float(options.get("max_gap", 2)) / 100
    if not -0.10 <= annual_rate <= 0.30 or not 0 < max_gap <= 0.10:
        raise ValueError("Enter an annual rate from −10% to 30% and a strike gap above 0% up to 10%.")
    spx_raw = data.spx[spx_key]
    spx = convert_hours(spx_raw, zone)
    es = data.es[es_key]
    pricing = american_crr if style == "american" else black76
    hour_data = []
    all_rows = []
    using_actual = bool(data.futures)
    for ts, quotes in sorted(es.items()):
        spx_t = (spx_expiry - ts).total_seconds() / YEAR
        es_t = (es_expiry - ts).total_seconds() / YEAR
        sf = surface(spx[ts], spx_t, annual_rate) if ts in spx else None
        proxy = one_forward(quotes)
        price = data.futures.get((es_key[1], ts)) if using_actual else proxy
        no_match = ("No SPX snapshot at this time" if ts not in spx else
                    "SPX expiry reached" if spx_t <= 0 else "Insufficient valid SPX prices")
        rows = []
        xs = [p["k"] for p in sf["nodes"]] if sf else []
        for (strike, side), q in sorted(quotes.items()):
            k = log(strike / price) if price is not None else None
            ask, bid = q["ask"], q["bid"]
            mid = (bid + ask) / 2 if bid is not None and ask is not None and 0 <= bid <= ask and ask > 0 else None
            status = "Matched"
            es_iv = bid_iv = ask_iv = mapped = model_price = low = high = span = None
            if price is None:
                status = "Missing actual futures price" if using_actual else "Missing or inconsistent ES forward"
            elif es_t <= 0:
                status = "ES expiry reached"
            elif mid is None:
                status = "Invalid ES bid or ask"
            else:
                es_iv = implied_vol(mid, price, strike, es_t, annual_rate, side, pricing)
                bid_iv = implied_vol(bid, price, strike, es_t, annual_rate, side, pricing)
                ask_iv = implied_vol(ask, price, strike, es_t, annual_rate, side, pricing)
                if es_iv is None:
                    status = "ES mid has no model IV"
                elif sf is None:
                    status = no_match
                else:
                    mapped, low, high, span, status = interpolate(k, sf["nodes"], xs, max_gap)
                    if mapped is not None:
                        model_price = float(pricing(price, strike, es_t, annual_rate, mapped, side))
            gap = es_iv - mapped if mapped is not None else None
            rows.append(dict(time=iso_ct(ts), strike=strike, side="Call" if side == 1 else "Put",
                             k=k, bucket=bucket(k) if k is not None else None,
                             bid=bid, ask=ask, mid=mid, es_iv=es_iv, es_bid_iv=bid_iv, es_ask_iv=ask_iv,
                             es_vendor_iv=q["iv"], mapped_iv=mapped, gap=gap, model_price=model_price,
                             price_gap=mid - model_price if model_price is not None else None,
                             within=bid - 1e-8 <= model_price <= ask + 1e-8 if model_price is not None else None,
                             status=status, spx_equivalent_strike=sf["forward"] * strike / price
                             if sf is not None and price is not None else None,
                             bracket_low=low, bracket_high=high, bracket_gap=span,
                             es_forward=price, spx_forward=sf["forward"] if sf else None,
                             es_dte=es_t * 365, spx_dte=spx_t * 365,
                             source_quote_time=iso_ct(q["source_ts"]), quote_status=q["status"]))
        all_rows.extend(rows)
        total = summary(rows)
        captured = [q["captured"].astimezone(UTC) for q in quotes.values() if q["captured"]]
        hour_data.append(dict(time=iso_ct(ts), spx_snapshot=ts in spx, summary=total, rows=rows, surface=sf,
                              spx_reference=spx_reference(spx[ts]) if ts in spx else None,
                              es_forward=price, es_proxy=proxy, es_dte=es_t * 365, spx_dte=spx_t * 365,
                              capture_lag=max((x - ts).total_seconds() for x in captured) if captured else None,
                              exchange_timestamp_rows=sum(q["source_ts"] is not None for q in quotes.values()),
                              captured_slot=any(q["captured_slot"] for q in quotes.values()),
                              es_naive=any(q["assumed_zone"] for q in quotes.values()),
                              atm_es=min((q for q in rows if q["es_iv"] is not None),
                                         key=lambda q: abs(q["k"]), default=None),
                              atm_spx=interpolate(0, sf["nodes"], xs, max_gap)[0] if sf else None))
    return dict(es=dict(code=es_key[0], future=es_key[1], expiry=iso_ct(es_expiry), source=es_source, style=style),
                spx=dict(id=spx_id, code=spx_key[0], raw_expiry=spx_key[1], expiry=iso_ct(spx_expiry),
                         source=spx_source, naive=any(t.tzinfo is None for t in spx_raw),
                         vendor_dte=sorted(data.vendor_dtes[spx_key]),
                         rates=sorted({q["rate"] for g in spx_raw.values() for q in g.values()
                                       if q["rate"] is not None})),
                expiry_gap_hours=hours_between(spx_expiry, es_expiry),
                rate=annual_rate, max_gap=max_gap, zone=zone.key, using_actual=using_actual,
                hours=hour_data, summary=summary(all_rows), buckets=[
                    dict(name=b, **summary([q for q in all_rows if q["bucket"] == b])) for b in BUCKETS],
                overlap=sum(h["spx_snapshot"] for h in hour_data),
                timing=timing_evidence(spx_raw, es),
                status_counts=dict(Counter(q["status"] for q in all_rows if q["status"] != "Matched")))
