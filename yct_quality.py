#!/usr/bin/env python3
"""Contrats de qualite et calendriers officiels partages par YCT."""

import hashlib
import json
import math
import os
from copy import deepcopy
from datetime import date, datetime, time as clock_time, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CALENDAR_PATH = os.path.join(ROOT, "config", "source-calendars.json")
DEFAULT_BOJ_POLICY_PATH = os.path.join(ROOT, "config", "boj-policy.json")
CFTC_CONTRACT_CODE = "097741"
CONTRACT_NOTIONAL_YEN = 12_500_000
POSITION_WEEKS = 170
POSITION_MIN_SPAN_DAYS = 3 * 365 - 14
FX_MIN_ROWS = 240
FX_MIN_SPAN_DAYS = 350
RISK_METHODOLOGY = {
    "risk_formula_version": "1.0.0",
    "weights": {
        "legacy_crowding": 0.45,
        "yen_appreciation_4w": 0.35,
        "rate_compression": 0.20,
    },
    "rate_differential_anchor": 5.25,
    "contract_notional_yen": CONTRACT_NOTIONAL_YEN,
    "calibration": "heuristic-not-backtested",
}
PRIMARY_SOURCE_URLS = {
    "cot": "https://publicreporting.cftc.gov/resource/6dca-aqww.json",
    "tff": "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
    "fx": "https://data-api.ecb.europa.eu/service/data/EXR/D.USD+JPY.EUR.SP00.A",
    "fed_api": "https://api.stlouisfed.org/fred/series/observations",
    "fed_csv": "https://fred.stlouisfed.org/graph/fredgraph.csv",
    "boj_schedule": "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm",
}


class QualityError(RuntimeError):
    """Erreur stable et classifiable, publiable par son code uniquement."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_date(value):
    return date.fromisoformat(str(value)[:10])


def parse_timestamp(value):
    return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def iso_timestamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_calendar(path=None):
    path = path or os.environ.get("SOURCE_CALENDAR_PATH") or DEFAULT_CALENDAR_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            calendar = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        raise QualityError("calendar-unreadable", "calendrier officiel illisible") from exc
    if calendar.get("schema_version") != 1:
        raise QualityError("calendar-schema", "schema calendrier invalide")
    try:
        for name, date_key in (("cftc", "release_dates"), ("boj", "meeting_end_dates")):
            config = calendar[name]
            values = [parse_date(value) for value in config[date_key]]
            valid_through = parse_date(config["valid_through"])
            if not values or values != sorted(values) or len(values) != len(set(values)):
                raise ValueError("dates non triees, vides ou dupliquees")
            if values[-1] > valid_through:
                raise ValueError("date au-dela de valid_through")
            if not str(config["source_url"]).startswith("https://"):
                raise ValueError("URL source non HTTPS")
            ZoneInfo(config["timezone"])
        release_hour, release_minute = (
            int(part) for part in calendar["cftc"]["release_time"].split(":")
        )
        decision_hour, decision_minute = (
            int(part) for part in calendar["boj"]["decision_time"].split(":")
        )
        clock_time(release_hour, release_minute)
        clock_time(decision_hour, decision_minute)
        if int(calendar["cftc"]["grace_minutes"]) < 0:
            raise ValueError("grace CFTC negative")
        if int(calendar["boj"]["review_grace_hours"]) < 0:
            raise ValueError("grace BoJ negative")
        release_dates = set(calendar["cftc"]["release_dates"])
        for release_value, report_value in calendar["cftc"].get("report_date_overrides", {}).items():
            release_date = parse_date(release_value)
            report_date = parse_date(report_value)
            if release_value not in release_dates or report_date.weekday() not in (0, 1):
                raise ValueError("override CFTC invalide")
            if not 1 <= (release_date - report_date).days <= 10:
                raise ValueError("ecart override CFTC invalide")
    except Exception as exc:  # noqa: BLE001
        raise QualityError("calendar-contract", "contrat calendrier invalide") from exc
    return calendar


def load_boj_policy(path=None):
    path = path or os.environ.get("BOJ_POLICY_PATH") or DEFAULT_BOJ_POLICY_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            policy = json.load(handle)
        if policy.get("schema_version") not in (1, 2):
            raise ValueError("schema")
        rate = float(policy["rate"])
        data_as_of = date.fromisoformat(policy["data_as_of"]).isoformat()
        source_url = str(policy["source_url"])
        source_sha256 = str(policy["source_sha256"])
        if not math.isfinite(rate) or not -1 <= rate <= 15:
            raise ValueError("rate")
        source = urlsplit(source_url)
        if source.scheme != "https" or source.netloc != "www.boj.or.jp" or source.query or source.fragment:
            raise ValueError("source_url")
        if len(source_sha256) != 64 or any(character not in "0123456789abcdef" for character in source_sha256):
            raise ValueError("source_sha256")
        transition = {}
        if policy["schema_version"] == 2:
            effective_from = date.fromisoformat(policy["effective_from"]).isoformat()
            previous_rate = float(policy["previous_rate"])
            if effective_from < data_as_of or not math.isfinite(previous_rate) or not -1 <= previous_rate <= 15:
                raise ValueError("effective_from/previous_rate")
            transition = {"effective_from": effective_from, "previous_rate": previous_rate}
    except Exception as exc:  # noqa: BLE001
        raise QualityError("boj-policy-contract", "configuration de politique BoJ invalide") from exc
    return {
        "schema_version": policy["schema_version"],
        "rate": rate,
        "data_as_of": data_as_of,
        "source_url": source_url,
        "source_sha256": source_sha256,
        **transition,
    }


def boj_rate_at(policy, now):
    """Apply a reviewed decision on its effective date in Japan, never on announcement."""
    local_date = now.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    if local_date < policy["data_as_of"]:
        raise QualityError("boj-future-decision", "decision BoJ encore future")
    if policy.get("effective_from") and local_date < policy["effective_from"]:
        return policy["previous_rate"]
    return policy["rate"]


def methodology_contract():
    """Retourne une copie pour eviter qu'un consommateur modifie le contrat global."""

    return deepcopy(RISK_METHODOLOGY)


def _previous_tuesday(value):
    days_since_tuesday = (value.weekday() - 1) % 7
    return value - timedelta(days=days_since_tuesday or 7)


def expected_cftc_report_date(now, calendar, *, with_grace=True):
    cfg = calendar.get("cftc", {})
    if now.date() > parse_date(cfg.get("valid_through")):
        raise QualityError("cftc-calendar-expired", "calendrier CFTC arrive a expiration")
    zone = ZoneInfo(cfg.get("timezone", "America/New_York"))
    hour, minute = (int(part) for part in cfg.get("release_time", "15:30").split(":"))
    grace = timedelta(minutes=int(cfg.get("grace_minutes", 90)) if with_grace else 0)
    eligible = []
    for raw_date in cfg.get("release_dates", []):
        release_date = parse_date(raw_date)
        release_at = datetime.combine(release_date, clock_time(hour, minute), zone)
        if now >= release_at.astimezone(timezone.utc) + grace:
            eligible.append(release_date)
    if not eligible:
        raise QualityError("cftc-calendar-empty", "aucune publication CFTC applicable")
    release_date = max(eligible)
    overrides = cfg.get("report_date_overrides", {})
    return parse_date(overrides[release_date.isoformat()]) if release_date.isoformat() in overrides \
        else _previous_tuesday(release_date)


def _easter_sunday(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def is_target_business_day(value):
    if value.weekday() >= 5:
        return False
    easter = _easter_sunday(value.year)
    closed = {
        date(value.year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(value.year, 5, 1),
        date(value.year, 12, 25),
        date(value.year, 12, 26),
    }
    return value not in closed


def expected_ecb_reference_date(now):
    local = now.astimezone(ZoneInfo("Europe/Paris"))
    candidate = local.date()
    if not (is_target_business_day(candidate) and local.time() >= clock_time(17, 0)):
        candidate -= timedelta(days=1)
    while not is_target_business_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def boj_config_state(data_as_of, now, calendar):
    cfg = calendar.get("boj", {})
    as_of = parse_date(data_as_of)
    meetings = sorted(parse_date(value) for value in cfg.get("meeting_end_dates", []))
    valid_through = parse_date(cfg.get("valid_through"))
    if as_of > now.date() or as_of > valid_through:
        return "stale-config", None
    next_meeting = next((value for value in meetings if value > as_of), None)
    if next_meeting is None:
        if now.date() > valid_through:
            return "stale-config", None
        return "verified-config", None
    zone = ZoneInfo(cfg.get("timezone", "Asia/Tokyo"))
    hour, minute = (int(part) for part in cfg.get("decision_time", "15:00").split(":"))
    deadline = datetime.combine(next_meeting, clock_time(hour, minute), zone)
    deadline += timedelta(hours=int(cfg.get("review_grace_hours", 24)))
    status = "verified-config" if now <= deadline.astimezone(timezone.utc) else "stale-config"
    return status, next_meeting.isoformat()


def content_sha256(value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_position_rows(rows, now, calendar, label="CFTC"):
    if not isinstance(rows, list) or len(rows) != POSITION_WEEKS:
        raise QualityError("position-row-count", "%s : %d lignes, %d attendues" % (
            label, len(rows) if isinstance(rows, list) else 0, POSITION_WEEKS
        ))
    dates = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise QualityError("position-row-shape", "%s : ligne %d invalide" % (label, index))
        try:
            row_date = parse_date(row["d"])
            long_count = int(row["long"])
            short_count = int(row["short"])
            net = int(row["net"])
            oi = int(row["oi"])
        except Exception as exc:  # noqa: BLE001
            raise QualityError("position-row-shape", "%s : ligne %d incomplete" % (label, index)) from exc
        if min(long_count, short_count, oi) < 0:
            raise QualityError("position-negative", "%s : position negative" % label)
        if net != long_count - short_count:
            raise QualityError("position-net", "%s : net incoherent" % label)
        if max(long_count, short_count, abs(net)) > oi:
            raise QualityError("position-open-interest", "%s : position superieure a l'open interest" % label)
        dates.append(row_date)
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise QualityError("position-dates", "%s : dates non triees ou dupliquees" % label)
    # La CFTC peut avancer le relevé au lundi lorsqu'un jour férié tombe le
    # mardi (par exemple le 3 juillet 2023 et le 10 novembre 2025).
    if any(value.weekday() not in (0, 1) for value in dates):
        raise QualityError("position-report-day", "%s : date de rapport hors lundi/mardi" % label)
    if any((current - previous).days not in (6, 7, 8) for previous, current in zip(dates, dates[1:])):
        raise QualityError("position-week-gap", "%s : semaine absente ou irreguliere" % label)
    if (dates[-1] - dates[0]).days < POSITION_MIN_SPAN_DAYS:
        raise QualityError("position-history", "%s : historique inferieur a trois ans" % label)
    expected = expected_cftc_report_date(now, calendar)
    released = expected_cftc_report_date(now, calendar, with_grace=False)
    # Grace tolerates the previous release while the API catches up. It must
    # never reject the new report once its official release time has passed.
    if dates[-1] not in {expected, released}:
        raise QualityError(
            "position-stale", "%s : derniere observation %s, %s attendue" % (label, dates[-1], expected)
        )


def validate_fx_rows(rows, now):
    if not isinstance(rows, list) or len(rows) < FX_MIN_ROWS:
        raise QualityError("fx-row-count", "BCE : historique incomplet")
    dates = []
    previous = None
    max_change = 0.0
    for index, row in enumerate(rows):
        try:
            row_date = parse_date(row["d"])
            value = float(row["v"])
        except Exception as exc:  # noqa: BLE001
            raise QualityError("fx-row-shape", "BCE : ligne %d invalide" % index) from exc
        if not math.isfinite(value) or not 40 <= value <= 300:
            raise QualityError("fx-range", "BCE : USD/JPY hors plage")
        if previous:
            max_change = max(max_change, abs(value / previous - 1.0) * 100.0)
        previous = value
        dates.append(row_date)
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise QualityError("fx-dates", "BCE : dates non triees ou dupliquees")
    if (dates[-1] - dates[0]).days < FX_MIN_SPAN_DAYS:
        raise QualityError("fx-history", "BCE : historique inferieur a douze mois")
    expected = expected_ecb_reference_date(now)
    if dates[-1] != expected:
        raise QualityError("fx-stale", "BCE : derniere reference %s, %s attendue" % (dates[-1], expected))
    if max_change > 15:
        raise QualityError("fx-jump", "BCE : variation quotidienne aberrante")
    return round(max_change, 4)


def detect_revisions(previous, current, fields, recent_days):
    if not isinstance(previous, list) or not previous or not current:
        return [], []
    old = {row.get("d"): row for row in previous if isinstance(row, dict) and row.get("d")}
    revised = []
    for row in current:
        prior = old.get(row.get("d"))
        if prior and any(prior.get(field) != row.get(field) for field in fields):
            revised.append(row["d"])
    cutoff = parse_date(current[-1]["d"]) - timedelta(days=recent_days)
    historical = [value for value in revised if parse_date(value) < cutoff]
    return revised, historical


def snapshot_fingerprint(data):
    keys = ("schema_version", "rates", "fx", "cot", "tff", "spot", "methodology", "data_as_of")
    provenance_fields = (
        "status", "provider", "dataset", "data_as_of", "source_url", "schedule_url",
        "policy_index_url", "content_sha256", "revised_dates", "next_review_due",
    )
    sources = data.get("sources", {}) if isinstance(data.get("sources"), dict) else {}
    provenance = {
        key: {field: meta.get(field) for field in provenance_fields if field in meta}
        for key, meta in sources.items() if isinstance(meta, dict)
    }
    payload = {key: data.get(key) for key in keys}
    payload["provenance"] = provenance
    return content_sha256(payload)
