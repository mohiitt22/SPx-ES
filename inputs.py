"""Read the two feed schemas and resolve series identifiers and settlement clocks."""

import csv
import io
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

NY = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
UTC = timezone.utc
SPX_FIELDS = {"contract", "time", "expiry", "strike", "call_price", "put_price"}
ES_FIELDS = {"option_contract", "futures_contract", "strike", "option_type", "bid", "ask"}
EARLY_SPX = {date(2026, 11, 27), date(2026, 12, 24)}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def timestamp(value, default=None):
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip())
        if parsed.tzinfo is None and default is not None:
            parsed = parsed.replace(tzinfo=default)
        return parsed
    except ValueError:
        return None


def day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def expiry_time(value, date_text, time_text, zone):
    if not (date_text or time_text):
        return value
    base = value.astimezone(zone) if value else None
    new_date = day(date_text) if date_text else base.date() if base else None
    new_time = time.fromisoformat(time_text) if time_text else base.time() if base else None
    if new_date is None or new_time is None or new_time.tzinfo:
        raise ValueError("Enter a local expiry date and time.")
    return datetime.combine(new_date, new_time, zone).astimezone(UTC)


def iso_ct(value):
    return value.astimezone(CT).isoformat(timespec="seconds") if value else None


def csv_rows(source, required, label):
    if isinstance(source, Path):
        raw = source.read_bytes()
    else:
        raw = source.read()
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{label} is missing columns: {', '.join(sorted(missing))}.")
        return list(reader), set(reader.fieldnames)
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f"{label} needs to be a valid UTF-8 CSV.") from exc


def read_spx(source):
    try:
        parquet = pq.ParquetFile(source)
        names = set(parquet.schema_arrow.names)
        missing = SPX_FIELDS - names
        if missing:
            raise ValueError(f"SPX Parquet is missing columns: {', '.join(sorted(missing))}.")
        columns = SPX_FIELDS | {"dte", "underlying", "rate", "call_iv", "put_iv"}
        rows = parquet.read(columns=sorted(columns & names)).to_pylist()
    except (OSError, TypeError) as exc:
        raise ValueError("SPX upload needs to be a readable Parquet file.") from exc

    series = defaultdict(lambda: defaultdict(dict))
    vendor_dtes = defaultdict(set)
    duplicates = defaultdict(int)
    ignored = 0
    for row in rows:
        name = str(row["contract"] or "").strip()
        raw_expiry = timestamp(row["expiry"])
        ts = timestamp(row["time"])
        strike = number(row["strike"])
        if not name or not raw_expiry or not ts or strike is None or strike <= 0:
            ignored += 1
            continue
        key = name, raw_expiry.isoformat()
        keys = series[key][ts]
        values = {field: number(row.get(field)) for field in
                  ("underlying", "rate", "call_price", "put_price", "call_iv", "put_iv")}
        if strike in keys and keys[strike] != values:
            raise ValueError(f"Conflicting SPX rows for {name} at {ts.isoformat()} and strike {strike:g}.")
        duplicates[key] += int(strike in keys)
        keys[strike] = values
        if (dte := number(row.get("dte"))) is not None:
            vendor_dtes[key].add(dte)
    if not series:
        raise ValueError("SPX Parquet has no usable contract snapshots.")
    return dict(series), vendor_dtes, duplicates, len(rows), ignored


def read_es(source):
    rows, names = csv_rows(source, ES_FIELDS, "ES CSV")
    if not {"scheduled_at", "captured_at"} & names:
        raise ValueError("ES CSV needs scheduled_at or captured_at.")
    series = defaultdict(lambda: defaultdict(dict))
    duplicates = defaultdict(int)
    ignored = 0
    for row in rows:
        name = (row["option_contract"] or "").strip().upper()
        future = (row["futures_contract"] or "").strip().upper()
        raw_ts = timestamp(row.get("scheduled_at") or row.get("captured_at"))
        ts = raw_ts if not raw_ts or raw_ts.tzinfo else raw_ts.replace(tzinfo=CT)
        captured = timestamp(row.get("captured_at"), CT)
        source_ts = timestamp(row.get("source_quote_timestamp"), CT)
        strike = number(row["strike"])
        side = {"CALL": 1, "C": 1, "PUT": -1, "P": -1}.get((row["option_type"] or "").strip().upper())
        if not name or not future or not ts or strike is None or strike <= 0 or not side:
            ignored += 1
            continue
        ts = ts.astimezone(UTC)
        month_match = re.search(r"XCME:O:([A-Z0-9]+):(\d{6}):", row.get("source_option_contract") or "")
        source_root, option_month = month_match.groups() if month_match else ("", "")
        explicit = (row.get("expiry") or "").strip()
        if explicit and day(explicit) is None:
            raise ValueError(f"ES CSV contains an invalid expiry for {name}.")
        key = name, future, explicit, source_root, option_month
        values = {field: number(row.get(field)) for field in ("forward", "bid", "ask", "iv")}
        values.update(captured=captured, source_ts=source_ts, status=(row.get("quote_status") or "").strip(),
                      captured_slot=not bool(row.get("scheduled_at")), assumed_zone=raw_ts.tzinfo is None)
        keys = series[key][ts]
        quote_key = (strike, side)
        if quote_key in keys and keys[quote_key] != values:
            raise ValueError(f"Conflicting ES rows for {name} at {ts.isoformat()} and strike {strike:g}.")
        duplicates[key] += int(quote_key in keys)
        keys[quote_key] = values
    if not series:
        raise ValueError("ES CSV has no usable contract snapshots.")
    return dict(series), duplicates, len(rows), ignored


def read_futures(source):
    rows, _ = csv_rows(source, {"timestamp", "futures_contract", "price"}, "Futures CSV")
    quotes = {}
    for i, row in enumerate(rows, 2):
        ts = timestamp(row["timestamp"])
        future = (row["futures_contract"] or "").strip().upper()
        price = number(row["price"])
        if not ts or ts.tzinfo is None or not future or price is None or price <= 0:
            raise ValueError(f"Futures CSV row {i} needs an offset-aware timestamp, contract and positive price.")
        key = (future, ts.astimezone(UTC))
        if key in quotes and quotes[key] != price:
            raise ValueError(f"Futures CSV has conflicting prices on row {i}.")
        quotes[key] = price
    if not quotes:
        raise ValueError("Futures CSV has no prices.")
    return quotes


def spx_settlement(key, marker_mode="vendor"):
    symbol, text = key
    raw = timestamp(text)
    if marker_mode == "exact":
        return (raw if raw.tzinfo else raw.replace(tzinfo=NY)).astimezone(UTC), "Raw expiry timestamp"
    if not symbol.startswith(("OWSNP", "ODSNP", "OMSNP", "OMAMSNP")):
        return (raw if raw.tzinfo else raw.replace(tzinfo=NY)).astimezone(UTC), "Raw expiry timestamp"
    actual = raw.date() + timedelta(days=1)
    settle = time(9, 30) if symbol.startswith("OMAMSNP") else time(13 if actual in EARLY_SPX else 16)
    value = datetime.combine(actual, settle, NY).astimezone(UTC)
    return value, "Vendor marker + 1 calendar day, with SPX settlement clock"


def es_settlement(key):
    name, _, explicit, source_root, option_month = key
    symbol = re.fullmatch(r"(EW[1-4]|ES)([FGHJKMNQUVXZ])(\d{1,2})", name)
    root = source_root or (symbol.group(1) if symbol else name)
    style = "american" if root == "ES" else "european" if (
        re.fullmatch(r"EW[1-4]?|E[1-5][ABCD]", root)) else None
    settle = time(8, 30) if style == "american" else time(15)
    if explicit:
        expiry_date = day(explicit)
        if style == "european" and expiry_date in EARLY_SPX:
            settle = time(12)
        parsed = timestamp(explicit, CT) if len(explicit) > 10 else datetime.combine(expiry_date, settle, CT)
        if parsed is None:
            raise ValueError(f"ES CSV contains an invalid expiry timestamp for {name}.")
        return parsed.astimezone(UTC), style, "ES CSV expiry"
    if not option_month and symbol:
        month = "FGHJKMNQUVXZ".index(symbol[2]) + 1
        option_month = f"{2020 + int(symbol[3]) if len(symbol[3]) == 1 else 2000 + int(symbol[3]):04d}{month:02d}"
        provenance = "Nominal CME weekday from contract code, verify year and holiday schedule"
    else:
        provenance = "Nominal CME weekday from source contract month, verify holiday schedule"
    if root in {"EW1", "EW2", "EW3", "EW4", "ES"} and re.fullmatch(r"\d{6}", option_month):
        first = day(f"{option_month[:4]}-{option_month[4:]}-01")
        if first:
            week = int(root[-1]) if root.startswith("EW") else 3
            expiry = first + timedelta(days=(4 - first.weekday()) % 7 + 7 * (week - 1))
            if (root, option_month) == ("EW3", "202701"):
                provenance = "CME Week 3 rule and January 2027 source contract month"
            if style == "european" and expiry in EARLY_SPX:
                settle = time(12)
            return datetime.combine(expiry, settle, CT).astimezone(UTC), style, provenance
    return None, style, "ES expiry date and time required"


@dataclass
class Inputs:
    spx_file: str
    es_file: str
    futures_file: str | None
    spx: dict
    es: dict
    futures: dict
    vendor_dtes: dict
    duplicates: int
    total_spx: int
    total_es: int
    ignored_spx: int
    ignored_es: int

    @classmethod
    def load(cls, spx, es, futures=None):
        spx_series, vendor_dtes, spx_duplicates, total_spx, ignored_spx = read_spx(spx)
        es_series, es_duplicates, total_es, ignored_es = read_es(es)
        name = lambda source: Path(source.filename).name if hasattr(source, "filename") else source.name
        return cls(name(spx), name(es), name(futures) if futures else None, spx_series, es_series,
                   read_futures(futures) if futures else {}, vendor_dtes,
                   sum(spx_duplicates.values()) + sum(es_duplicates.values()),
                   total_spx, total_es, ignored_spx, ignored_es)
