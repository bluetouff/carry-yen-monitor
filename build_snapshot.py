#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construit puis promeut le snapshot YCT uniquement s'il est integralement sain.

Les donnees publiques restent le dernier etat valide. Chaque tentative, y compris
un echec amont, est decrite separement dans status.json. Les flux Massive sont
optionnels et publies dans market.json pour ne jamais changer la nature de la
reference quotidienne BCE.
"""

import csv
import hashlib
import io
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from yct_quality import (
    CFTC_CONTRACT_CODE,
    PRIMARY_SOURCE_URLS,
    QualityError,
    boj_config_state,
    boj_rate_at,
    content_sha256,
    confirmed_boj_policy,
    detect_revisions,
    iso_timestamp,
    load_boj_policy,
    load_calendar,
    methodology_contract,
    parse_timestamp,
    snapshot_fingerprint,
    validate_fx_rows,
    validate_position_rows,
)

OUT_PATH = os.environ.get("OUT_PATH", "/var/lib/yct/data.json")
SOURCE_CALENDAR_PATH = os.environ.get("SOURCE_CALENDAR_PATH", "").strip() or None
BOJ_POLICY_PATH = os.environ.get("BOJ_POLICY_PATH", "").strip() or None
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "").strip()
MASSIVE_API_KEY = os.environ.get("MASSIVE_API_KEY", "").strip()

UA = "yct-snapshot/3.0 (+https://yct.l0g.fr)"
CONTRACT_CODE = CFTC_CONTRACT_CODE
POSITION_LIMIT = 170
MAX_BYTES = 8 * 1024 * 1024
RUN_BUDGET_SECONDS = 90
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_ATTEMPTS = 3

CFTC_URL = PRIMARY_SOURCE_URLS["cot"]
CFTC_TFF_URL = PRIMARY_SOURCE_URLS["tff"]
ECB_API_URL = PRIMARY_SOURCE_URLS["fx"]
FRED_API_URL = PRIMARY_SOURCE_URLS["fed_api"]
FRED_CSV_URL = PRIMARY_SOURCE_URLS["fed_csv"]
FED_SERIES = ("DFEDTARL", "DFEDTARU")
MASSIVE_QUOTE_URL = "https://api.massive.com/v1/last_quote/currencies/USD/JPY"
MASSIVE_DOC_URL = "https://massive.com/docs/rest/forex/quotes/last-quote"
BOJ_SCHEDULE_URL = PRIMARY_SOURCE_URLS["boj_schedule"]
BOJ_STATEMENTS_URL_TEMPLATE = "https://www.boj.or.jp/en/mopo/mpmdeci/state_%d/index.htm"


class SourceError(RuntimeError):
    """Erreur reseau ou schema avec un code public sans secret."""

    def __init__(self, code, message, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def log(message):
    print("[yct] %s" % message, file=sys.stderr, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def _runtime_path(env_name, filename):
    configured = os.environ.get(env_name, "").strip()
    return configured or os.path.join(os.path.dirname(OUT_PATH) or ".", filename)


def _error_code(exc):
    return getattr(exc, "code", None) or exc.__class__.__name__.lower()


class HttpClient:
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, deadline=None, sleep=time.sleep, rng=None):
        self.deadline = deadline or (time.monotonic() + RUN_BUDGET_SECONDS)
        self.sleep = sleep
        self.rng = rng or random.SystemRandom()
        self.attempts = 0
        self.bytes_received = 0
        self.last_http_status = None

    def _remaining(self):
        return self.deadline - time.monotonic()

    def _pause(self, attempt, retry_after=None):
        if retry_after:
            try:
                delay = min(10.0, max(0.0, float(retry_after)))
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(retry_after))
                    delay = min(10.0, max(0.0, (retry_at - utc_now()).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    delay = 0.0
        else:
            delay = min(8.0, (2 ** (attempt - 1)) + self.rng.random())
        remaining = self._remaining()
        if remaining <= 0:
            raise SourceError("deadline", "budget global epuise")
        self.sleep(min(delay, max(0.0, remaining)))

    def get(self, url, headers=None, timeout=REQUEST_TIMEOUT_SECONDS, attempts=REQUEST_ATTEMPTS):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise SourceError("non-https", "schema non HTTPS refuse")
        host = parsed.netloc
        request_headers = {"User-Agent": UA, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)
        for attempt in range(1, attempts + 1):
            remaining = self._remaining()
            if remaining <= 0:
                raise SourceError("deadline", "%s : budget global epuise" % host)
            self.attempts += 1
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(request, timeout=min(timeout, remaining)) as response:
                    final_url = urllib.parse.urlparse(response.geturl())
                    if final_url.scheme != "https":
                        raise SourceError("redirect-non-https", "%s : redirection refusee" % host)
                    self.last_http_status = getattr(response, "status", 200)
                    raw = response.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise SourceError("response-too-large", "%s : reponse trop volumineuse" % host)
                    self.bytes_received += len(raw)
                    return raw
            except urllib.error.HTTPError as exc:
                self.last_http_status = exc.code
                code = "http-%d" % exc.code
                if exc.code not in self.RETRYABLE_STATUS or attempt == attempts:
                    raise SourceError(
                        code, "%s : HTTP %d" % (host, exc.code),
                        retry_after=exc.headers.get("Retry-After") if exc.headers else None,
                    ) from exc
                log("retry %d/%d sur %s apres HTTP %d" % (attempt, attempts, host, exc.code))
                self._pause(attempt, exc.headers.get("Retry-After") if exc.headers else None)
            except SourceError:
                raise
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                if attempt == attempts:
                    raise SourceError("network", "%s : erreur reseau" % host) from exc
                log("retry %d/%d sur %s apres erreur reseau" % (attempt, attempts, host))
                self._pause(attempt)
        raise SourceError("network", "%s : requete impossible" % host)


def http_get(url, headers=None, timeout=REQUEST_TIMEOUT_SECONDS, attempts=REQUEST_ATTEMPTS, client=None):
    client = client or HttpClient()
    return client.get(url, headers=headers, timeout=timeout, attempts=attempts)


def http_get_json(url, headers=None, client=None):
    try:
        return json.loads(http_get(url, headers=headers, client=client).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceError("json", "reponse JSON invalide") from exc


def http_get_text(url, headers=None, client=None, attempts=REQUEST_ATTEMPTS):
    try:
        return http_get(url, headers=headers, client=client, attempts=attempts).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("encoding", "encodage source invalide") from exc


def _position_value(item, field, label):
    value = item.get(field)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QualityError("position-missing-field", "%s : champ %s invalide" % (label, field)) from exc
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise QualityError("position-value", "%s : champ %s incoherent" % (label, field))
    return int(number)


def _fetch_positions(url, long_field, short_field, label, client, now, calendar):
    params = {
        "$where": "cftc_contract_market_code = '%s'" % CONTRACT_CODE,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(POSITION_LIMIT),
        "$select": (
            "report_date_as_yyyy_mm_dd,cftc_contract_market_code,%s,%s,open_interest_all"
            % (long_field, short_field)
        ),
    }
    endpoint = url + "?" + urllib.parse.urlencode(params)
    headers = {"X-App-Token": SOCRATA_APP_TOKEN} if SOCRATA_APP_TOKEN else None
    raw = http_get_json(endpoint, headers=headers, client=client)
    if not isinstance(raw, list) or len(raw) != POSITION_LIMIT:
        raise QualityError("position-raw-count", "%s : reponse incomplete" % label)
    rows = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise QualityError("position-row-shape", "%s : ligne %d invalide" % (label, index))
        code = str(item.get("cftc_contract_market_code") or "").strip()
        if code != CONTRACT_CODE:
            raise QualityError("position-contract", "%s : contrat inattendu %r" % (label, code))
        report_date = str(item.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not report_date:
            raise QualityError("position-date", "%s : date absente" % label)
        long_count = _position_value(item, long_field, label)
        short_count = _position_value(item, short_field, label)
        open_interest = _position_value(item, "open_interest_all", label)
        rows.append({
            "d": report_date,
            "long": long_count,
            "short": short_count,
            "net": long_count - short_count,
            "oi": open_interest,
        })
    rows.sort(key=lambda row: row["d"])
    validate_position_rows(rows, now, calendar, label=label)
    log("%s ok : contrat %s, %d semaines, dernier %s net=%d" % (
        label, CONTRACT_CODE, len(rows), rows[-1]["d"], rows[-1]["net"]
    ))
    return rows


def fetch_cftc(client=None, now=None, calendar=None):
    return _fetch_positions(
        CFTC_URL,
        "noncomm_positions_long_all",
        "noncomm_positions_short_all",
        "CFTC Legacy",
        client or HttpClient(),
        now or utc_now(),
        calendar or load_calendar(SOURCE_CALENDAR_PATH),
    )


def fetch_tff(client=None, now=None, calendar=None):
    return _fetch_positions(
        CFTC_TFF_URL,
        "lev_money_positions_long",
        "lev_money_positions_short",
        "CFTC TFF",
        client or HttpClient(),
        now or utc_now(),
        calendar or load_calendar(SOURCE_CALENDAR_PATH),
    )


def fetch_fx(client=None, now=None, calendar=None):  # calendar garde une signature commune
    del calendar
    now = now or utc_now()
    end = now.date()
    start = end - timedelta(days=372)
    url = ECB_API_URL + "?" + urllib.parse.urlencode({
        "startPeriod": start.isoformat(), "endPeriod": end.isoformat(), "format": "csvdata"
    })
    reader = csv.DictReader(io.StringIO(http_get_text(url, client=client)))
    by_date = {}
    relevant_rows = 0
    for index, item in enumerate(reader):
        currency = str(item.get("CURRENCY") or "").strip()
        if currency not in ("USD", "JPY"):
            continue
        relevant_rows += 1
        report_date = str(item.get("TIME_PERIOD") or "")[:10]
        try:
            value = float(item.get("OBS_VALUE"))
        except (TypeError, ValueError) as exc:
            raise QualityError("fx-value", "BCE : valeur invalide ligne %d" % index) from exc
        if not report_date or not math.isfinite(value) or value <= 0:
            raise QualityError("fx-row-shape", "BCE : ligne %d invalide" % index)
        if currency in by_date.setdefault(report_date, {}):
            raise QualityError("fx-duplicate-currency", "BCE : devise dupliquee au %s" % report_date)
        by_date[report_date][currency] = value
    if relevant_rows == 0:
        raise QualityError("fx-empty", "BCE : aucune observation USD ou JPY")
    rows = []
    for report_date, rates in by_date.items():
        if set(rates) != {"USD", "JPY"}:
            raise QualityError("fx-unpaired", "BCE : paire incomplete au %s" % report_date)
        rows.append({"d": report_date, "v": round(rates["JPY"] / rates["USD"], 4)})
    rows.sort(key=lambda row: row["d"])
    max_change = validate_fx_rows(rows, now)
    log("BCE ok : %d references, USD/JPY %s = %.4f" % (
        len(rows), rows[-1]["d"], rows[-1]["v"]
    ))
    return rows, max_change


def _fred_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise QualityError("fred-date", "FRED : date non ISO")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise QualityError("fred-date", "FRED : date invalide") from exc
    return value


def _fred_number(value):
    if isinstance(value, bool):
        raise QualityError("fred-value", "FRED : valeur booleenne refusee")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QualityError("fred-value", "FRED : valeur invalide") from exc
    if not math.isfinite(number):
        raise QualityError("fred-value", "FRED : valeur non finie")
    return number


def fred_api_observations(series_id, client):
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "14",
    }
    payload = http_get_json(FRED_API_URL + "?" + urllib.parse.urlencode(params), client=client)
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        raise QualityError("fred-schema", "FRED : observations absentes")
    values = {}
    seen = set()
    for item in observations:
        if not isinstance(item, dict):
            raise QualityError("fred-schema", "FRED : observation non objet")
        value = item.get("value")
        report_date = _fred_date(item.get("date"))
        if report_date in seen:
            raise QualityError("fred-duplicate-date", "FRED : date dupliquee")
        seen.add(report_date)
        if value not in (".", None, ""):
            values[report_date] = _fred_number(value)
    if not values:
        raise QualityError("fred-empty", "FRED : aucune observation exploitable")
    return values


def parse_fred_csv(document, now):
    try:
        reader = csv.DictReader(io.StringIO(document), strict=True)
        fields = reader.fieldnames or []
        date_field = "observation_date" if "observation_date" in fields else "DATE"
        if len(fields) != 3 or set(fields) != {date_field, *FED_SERIES}:
            raise QualityError("fred-schema", "FRED : colonnes CSV inattendues")
        values = {series_id: {} for series_id in FED_SERIES}
        seen = set()
        for item in reader:
            if None in item or any(value is None for value in item.values()):
                raise QualityError("fred-schema", "FRED : ligne CSV incomplete")
            report_date = _fred_date(item[date_field])
            if report_date in seen:
                raise QualityError("fred-duplicate-date", "FRED : date dupliquee")
            seen.add(report_date)
            for series_id in FED_SERIES:
                value = item[series_id]
                if value not in (".", ""):
                    values[series_id][report_date] = _fred_number(value)
    except csv.Error as exc:
        raise QualityError("fred-schema", "FRED : CSV mal forme ou tronque") from exc
    return latest_common_rate(*(values[series_id] for series_id in FED_SERIES), now=now)


def fred_csv_rate(client, now):
    # Une seule reponse fournit les deux bornes. Les erreurs HTTP et de schema
    # partagent trois essais, sans multiplier les retries ni le budget global.
    url = FRED_CSV_URL + "?" + urllib.parse.urlencode({"id": ",".join(FED_SERIES)})
    retryable = {"network", "fred-schema", "fred-empty"} | {
        "http-%d" % status for status in HttpClient.RETRYABLE_STATUS
    }
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            document = http_get_text(url, client=client, attempts=1)
            return parse_fred_csv(document, now)
        except (SourceError, QualityError) as exc:
            if _error_code(exc) not in retryable or attempt == REQUEST_ATTEMPTS:
                raise
            log("FRED CSV retry %d/%d apres %s" % (attempt, REQUEST_ATTEMPTS, _error_code(exc)))
            client._pause(attempt, retry_after=getattr(exc, "retry_after", None))
    raise SourceError("fred-empty", "FRED : export CSV indisponible")


def latest_common_rate(lower_values, upper_values, now=None):
    now = now or utc_now()
    if not lower_values or not upper_values:
        raise QualityError("fred-empty", "FRED : aucune observation exploitable")
    if max(lower_values) != max(upper_values):
        raise QualityError("fred-unpaired", "FRED : dernieres bornes non synchronisees")
    common = sorted(set(lower_values).intersection(upper_values))
    if not common:
        raise QualityError("fred-common-date", "FRED : aucune date commune")
    report_date = common[-1]
    _fred_date(report_date)
    lower = _fred_number(lower_values[report_date])
    upper = _fred_number(upper_values[report_date])
    width = upper - lower
    if not all(math.isfinite(value) for value in (lower, upper)) or lower > upper or width > 1:
        raise QualityError("fred-range", "FRED : fourchette incoherente")
    lag = (now.date() - datetime.fromisoformat(report_date).date()).days
    if lag < 0 or lag > 4:
        raise QualityError("fred-stale", "FRED : cible trop ancienne")
    return round((lower + upper) / 2.0, 3), lower, upper, report_date


def fetch_fed_rate(client=None, now=None, calendar=None):
    del calendar
    client = client or HttpClient()
    now = now or utc_now()
    if FRED_API_KEY:
        try:
            result = latest_common_rate(
                fred_api_observations("DFEDTARL", client),
                fred_api_observations("DFEDTARU", client),
                now=now,
            )
            log("FRED API ok : cible [%.3f ; %.3f], milieu %.3f au %s" % (
                result[1], result[2], result[0], result[3]
            ))
            return result + ("fred-api",)
        except Exception as exc:  # noqa: BLE001
            log("FRED API indisponible (%s), essai CSV" % _error_code(exc))
    result = fred_csv_rate(client, now)
    log("FRED CSV ok : cible [%.3f ; %.3f], milieu %.3f au %s" % (
        result[1], result[2], result[0], result[3]
    ))
    return result + ("fred-csv",)


def fetch_massive_quote(client=None, now=None, calendar=None):
    del calendar
    if not MASSIVE_API_KEY:
        raise SourceError("not-configured", "Massive non configure")
    client = client or HttpClient()
    now = now or utc_now()
    url = MASSIVE_QUOTE_URL + "?" + urllib.parse.urlencode({"apiKey": MASSIVE_API_KEY})
    payload = http_get_json(url, client=client)
    quote = payload.get("last") if isinstance(payload, dict) else None
    if not isinstance(quote, dict):
        raise QualityError("massive-schema", "Massive : last absent")
    try:
        bid = float(quote["bid"])
        ask = float(quote["ask"])
        timestamp_ms = int(quote["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise QualityError("massive-schema", "Massive : quote incomplete") from exc
    mid = (bid + ask) / 2.0
    if not all(math.isfinite(value) for value in (bid, ask)) or bid <= 0 or ask < bid:
        raise QualityError("massive-quote", "Massive : bid/ask incoherent")
    if not 40 <= mid <= 300 or ask - bid > 1:
        raise QualityError("massive-range", "Massive : USD/JPY hors plage")
    quote_at = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    age_hours = (now - quote_at).total_seconds() / 3600.0
    if age_hours < -1 or age_hours > 96:
        raise QualityError("massive-stale", "Massive : quote trop ancienne")
    return {
        "schema_version": 1,
        "generated": iso_timestamp(now),
        "provider": "Massive Market Data",
        "pair": "USD/JPY",
        "bid": round(bid, 5),
        "ask": round(ask, 5),
        "mid": round(mid, 5),
        "data_as_of": iso_timestamp(quote_at),
        "source_url": MASSIVE_DOC_URL,
    }


class _BojHTMLParser(HTMLParser):
    """Read links and scoped policy/news evidence without interpreting HTML as code."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.policies = []
        self.news_items = []
        self.stack = []

    def _ancestor(self, key):
        return next((frame[key] for frame in reversed(self.stack) if key in frame), None)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"a", "section", "time"} and len(attributes) != len(attrs):
            raise QualityError("boj-index-schema", "BoJ : attribut HTML duplique")
        frame = {"tag": tag}
        if tag == "section" and "policy" in (attributes.get("class") or "").split():
            frame["policy"] = {"headings": [], "links": [], "closed": False}
            self.policies.append(frame["policy"])
        if tag == "li":
            frame["news"] = {"dates": [], "links": [], "closed": False}
            self.news_items.append(frame["news"])
        if tag == "time":
            news = self._ancestor("news")
            if news is not None:
                news["dates"].append(attributes.get("datetime"))
        if tag == "h3":
            policy_section = self._ancestor("policy")
            if policy_section is not None:
                frame["heading"] = {"parts": [], "closed": False}
                policy_section["headings"].append(frame["heading"])
        if tag == "a":
            if self._ancestor("anchor") is not None:
                raise QualityError("boj-index-schema", "BoJ : liens HTML imbriques")
            frame["anchor"] = {"href": attributes.get("href"), "parts": [], "closed": False}
            self.links.append(frame["anchor"])
            for key in ("policy", "news"):
                ancestor = self._ancestor(key)
                if ancestor is not None:
                    ancestor["links"].append(frame["anchor"])
        if tag not in self.VOID_TAGS:
            self.stack.append(frame)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                for key in ("anchor", "heading", "policy", "news"):
                    if key in self.stack[index]:
                        self.stack[index][key]["closed"] = True
                del self.stack[index:]
                break

    def handle_data(self, data):
        if any(frame["tag"] in {"script", "style"} for frame in self.stack):
            return
        for key in ("anchor", "heading"):
            node = self._ancestor(key)
            if node is not None:
                node["parts"].append(data)


def _boj_text(node):
    return " ".join("".join(node["parts"]).split())


def _boj_statement_title(title):
    return bool(re.fullmatch(
        r"(?:Statement on Monetary Policy|Change in the Guideline for Money Market Operations)"
        r"(?:\s*\[PDF(?:\s+[^\]]+)?\])?", title,
    ))


def _boj_statement_link(href, base_url):
    if not isinstance(href, str) or not href:
        raise QualityError("boj-source-url", "BoJ : lien de decision absent")
    url = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlsplit(url)
    match = re.fullmatch(
        r"/en/mopo/mpmdeci/(?:mpr|state)_(\d{4})/k(\d{6})[a-z]?\.(?:pdf|htm)", parsed.path
    )
    if parsed.scheme != "https" or parsed.netloc != "www.boj.or.jp" or parsed.query or parsed.fragment or not match:
        raise QualityError("boj-source-url", "BoJ : URL de decision non officielle ou inattendue")
    try:
        report_date = datetime.strptime(match[2], "%y%m%d").date()
    except ValueError as exc:
        raise QualityError("boj-date", "BoJ : date de decision invalide") from exc
    if report_date.year != int(match[1]):
        raise QualityError("boj-date", "BoJ : annee de decision incoherente")
    return url, report_date.isoformat()


def _boj_parse_html(document):
    parser = _BojHTMLParser()
    parser.feed(document)
    parser.close()
    return parser


def _boj_confirm_current_guideline(document, latest, policy, now, index_url):
    """Confirm an unchanged current target, never claim to have reviewed a new PDF."""
    parser = _boj_parse_html(document)
    if len(parser.policies) != 1:
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : bloc Guideline ambigu ou absent")
    section = parser.policies[0]
    if not section["closed"] or len(section["headings"]) != 1 or len(section["links"]) != 1:
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : structure Guideline inattendue")
    heading, link = section["headings"][0], section["links"][0]
    if not heading["closed"] or _boj_text(heading) != "Guideline" or not link["closed"]:
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : Guideline non confirmee")
    if urllib.parse.urljoin("https://www.boj.or.jp/en/index.htm", link["href"] or "") != index_url:
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : index Guideline incoherent")
    match = re.fullmatch(
        r"The Bank will encourage the uncollateralized overnight call rate to remain at around "
        r"(-?(?:0|[1-9]\d*)(?:\.\d+)?) percent\.", _boj_text(link),
    )
    if not match:
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : cible Guideline ambigue")
    rate = float(match[1])
    if not math.isfinite(rate) or rate != policy["rate"] or rate != boj_rate_at(policy, now):
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : variation de taux a examiner")
    if policy.get("effective_from") and now.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat() < policy["effective_from"]:
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : transition de taux encore en attente")
    dated_links = []
    for item in parser.news_items:
        if any(urllib.parse.urljoin("https://www.boj.or.jp/en/index.htm", entry["href"] or "") == latest["url"]
               for entry in item["links"]):
            if not item["closed"] or item["dates"] != [latest["d"]] or any(not entry["closed"] for entry in item["links"]):
                raise QualityError("boj-new-decision", "nouvelle decision BoJ : date de publication non confirmee")
            dated_links.append(item)
    if not dated_links:
        raise QualityError("boj-new-decision", "nouvelle decision BoJ : page Guideline anterieure a la publication")
    return rate


def fetch_boj_index(client=None, now=None, calendar=None, policy=None):
    del calendar
    client = client or HttpClient()
    now = now or utc_now()
    policy = policy or load_boj_policy(BOJ_POLICY_PATH)
    policy_as_of = policy["data_as_of"]
    policy_source_url = policy["source_url"]
    try:
        year = int(policy_as_of[:4])
    except (TypeError, ValueError) as exc:
        raise QualityError("boj-date", "date de configuration BoJ invalide") from exc
    local_today = now.astimezone(ZoneInfo("Asia/Tokyo")).date()
    if policy_as_of > local_today.isoformat():
        raise QualityError("boj-future-decision", "decision BoJ encore future")
    configured_url, configured_date = _boj_statement_link(policy_source_url, policy_source_url)
    if configured_date != policy_as_of:
        raise QualityError("boj-date", "BoJ : date de configuration et URL incoherentes")
    statements = []
    index_urls = [BOJ_STATEMENTS_URL_TEMPLATE % current_year for current_year in sorted({year, local_today.year})]
    for index_url in index_urls:
        parser = _boj_parse_html(http_get_text(index_url, client=client))
        for link in parser.links:
            title = _boj_text(link)
            if _boj_statement_title(title):
                if not link["closed"]:
                    raise QualityError("boj-index-schema", "BoJ : lien de decision incomplet")
                url, report_date = _boj_statement_link(link["href"], index_url)
                if report_date > local_today.isoformat():
                    raise QualityError("boj-future-decision", "decision BoJ encore future")
                statements.append({"d": report_date, "url": url, "title": title})
    if not statements:
        raise QualityError("boj-index-schema", "index des decisions BoJ illisible")
    statements.sort(key=lambda item: (item["d"], item["url"]))
    latest_date = statements[-1]["d"]
    configured_urls = {item["url"] for item in statements if item["d"] == policy_as_of}
    if configured_url not in configured_urls:
        raise QualityError("boj-source-url", "URL de decision BoJ non retrouvee dans l'index")
    latest_urls = {item["url"] for item in statements if item["d"] == latest_date}
    if len(latest_urls) != 1:
        raise QualityError("boj-index-schema", "BoJ : plusieurs decisions concurrentes")
    latest = statements[-1]
    confirmation_rate = None
    if latest_date != policy_as_of:
        canonical_url = "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_%s/k%sa.pdf" % (
            latest_date[:4], latest_date[2:].replace("-", ""),
        )
        if latest_date < policy_as_of or latest["url"] != canonical_url:
            raise QualityError("boj-new-decision", "nouvelle decision BoJ a examiner")
        confirmation_rate = _boj_confirm_current_guideline(
            http_get_text("https://www.boj.or.jp/en/index.htm", client=client), latest, policy, now,
            BOJ_STATEMENTS_URL_TEMPLATE % int(latest_date[:4]),
        )
    statement = http_get(latest["url"], client=client)
    raw_statement_sha256 = hashlib.sha256(statement).hexdigest()
    if not statement.startswith(b"%PDF-"):
        raise QualityError("boj-source-format", "decision BoJ non PDF")
    if confirmation_rate is None and raw_statement_sha256 != policy["source_sha256"]:
        raise QualityError("boj-source-hash", "empreinte de la decision BoJ inattendue")
    result = {
        "latest_statement_date": latest_date,
        "index_url": index_urls[-1],
        "index_urls": index_urls,
        "statement_url": latest["url"],
        "statement_sha256": raw_statement_sha256,
    }
    if confirmation_rate is not None:
        result["confirmation"] = {
            "mode": "official-guideline-confirmed", "rate": confirmation_rate,
            "data_as_of": latest_date, "source_url": latest["url"],
            "source_sha256": raw_statement_sha256,
            "guideline_url": "https://www.boj.or.jp/en/index.htm",
        }
    return result


def _source_result(name, fetcher, now, calendar, deadline):
    client = HttpClient(deadline=deadline)
    started = time.monotonic()
    try:
        value = fetcher(client=client, now=now, calendar=calendar)
        return name, value, {
            "status": "fresh",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "attempts": client.attempts,
            "bytes": client.bytes_received,
            "http_status": client.last_http_status,
        }, None
    except Exception as exc:  # noqa: BLE001
        log("%s ECHEC : %s" % (name, exc))
        return name, None, {
            "status": "failed",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "attempts": client.attempts,
            "bytes": client.bytes_received,
            "http_status": client.last_http_status,
            "error_code": _error_code(exc),
            **(exc.date_details if isinstance(exc, QualityError) else {}),
        }, exc


def load_existing(path=None):
    try:
        with open(path or OUT_PATH, "r", encoding="utf-8") as snapshot:
            data = json.load(snapshot)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def atomic_write(path, data, mode=0o644):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".yct.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def atomic_promote(candidate_path, output_path):
    os.replace(candidate_path, output_path)
    os.chmod(output_path, 0o644)
    directory = os.path.dirname(output_path) or "."
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _public_source_meta(provider, dataset, data_as_of, checked_at, source_url, metrics, rows, revisions=None):
    return {
        "status": "fresh",
        "provider": provider,
        "dataset": dataset,
        "data_as_of": data_as_of,
        "last_checked_at": checked_at,
        "last_success_at": checked_at,
        "source_url": source_url,
        "observations": rows,
        "content_sha256": metrics.get("content_sha256"),
        "revised_dates": revisions or [],
    }


def _status_base(checked_at, previous):
    return {
        "schema_version": 1,
        "checked_at": checked_at,
        "status": "degraded",
        "published": False,
        "last_successful_publication_at": previous.get("published_at") or previous.get("generated"),
        "sources": {},
    }


def main(now=None):
    now = now or utc_now()
    checked_at = iso_timestamp(now)
    status_path = _runtime_path("STATUS_PATH", "status.json")
    candidate_path = _runtime_path("CANDIDATE_PATH", "candidate.json")
    market_path = _runtime_path("MARKET_PATH", "market.json")
    previous = load_existing()
    status = _status_base(checked_at, previous)

    try:
        calendar = load_calendar(SOURCE_CALENDAR_PATH)
        boj_policy = load_boj_policy(BOJ_POLICY_PATH)
        reviewed_boj_as_of = boj_policy["data_as_of"]
        boj_rate = boj_rate_at(boj_policy, now)
    except Exception as exc:  # noqa: BLE001
        status["error_code"] = _error_code(exc)
        atomic_write(status_path, status)
        log("calendrier ECHEC : %s" % exc)
        return 1 if previous else 2

    deadline = time.monotonic() + RUN_BUDGET_SECONDS
    fetchers = {
        "cot": fetch_cftc,
        "tff": fetch_tff,
        "fx": fetch_fx,
        "fed": fetch_fed_rate,
        "boj_index": lambda **kwargs: fetch_boj_index(policy=boj_policy, **kwargs),
    }
    if MASSIVE_API_KEY:
        fetchers["massive"] = fetch_massive_quote
    else:
        status["sources"]["massive"] = {"status": "not-configured", "error_code": "not-configured"}

    values = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
        futures = {
            executor.submit(_source_result, name, fetcher, now, calendar, deadline): name
            for name, fetcher in fetchers.items()
        }
        for future in as_completed(futures):
            name, value, metrics, error = future.result()
            values[name] = value
            status["sources"][name] = metrics
            if error:
                errors[name] = error

    boj_index_metrics = status["sources"].pop("boj_index", {"status": "failed", "error_code": "boj-index-missing"})
    boj_index_error = errors.pop("boj_index", None)
    boj_confirmation = (values.get("boj_index") or {}).get("confirmation")
    if boj_confirmation is not None and not boj_index_error:
        try:
            boj_policy = confirmed_boj_policy(boj_policy, boj_confirmation, now)
            boj_rate = boj_rate_at(boj_policy, now)
        except QualityError as exc:
            boj_index_error = exc
    boj_status, next_review = boj_config_state(boj_policy["data_as_of"], now, calendar)
    status["sources"]["boj"] = dict(boj_index_metrics)
    status["sources"]["boj"].update({
        "status": boj_status,
        "data_as_of": boj_policy["data_as_of"],
        "next_review_due": next_review,
        "verification_mode": "official-guideline-confirmed" if boj_confirmation else "reviewed-policy",
        "reviewed_policy_as_of": reviewed_boj_as_of,
    })
    if boj_index_error:
        errors["boj"] = boj_index_error
        status["sources"]["boj"]["status"] = "failed"
        status["sources"]["boj"]["error_code"] = _error_code(boj_index_error)
    elif boj_status != "verified-config":
        errors["boj"] = QualityError("boj-stale-config", "configuration BoJ a reverifier")
        status["sources"]["boj"]["error_code"] = "boj-stale-config"
    else:
        status["sources"]["boj"]["content_sha256"] = content_sha256(values["boj_index"])

    if values.get("massive"):
        atomic_write(market_path, values["massive"])
        status["sources"]["massive"].update({
            "data_as_of": values["massive"]["data_as_of"],
            "content_sha256": content_sha256(values["massive"]),
        })

    previous_sources = previous.get("sources", {}) if isinstance(previous.get("sources"), dict) else {}
    for name, metrics in status["sources"].items():
        prior = previous_sources.get(name, {}) if isinstance(previous_sources.get(name), dict) else {}
        if metrics.get("status") in ("fresh", "verified-config"):
            metrics["last_success_at"] = checked_at
        elif prior.get("last_success_at"):
            metrics["last_success_at"] = prior["last_success_at"]
        if not metrics.get("data_as_of") and prior.get("data_as_of"):
            metrics["data_as_of"] = prior["data_as_of"]
    if values.get("cot"):
        status["sources"]["cot"].update({
            "data_as_of": values["cot"][-1]["d"], "rows": len(values["cot"]),
            "content_sha256": content_sha256(values["cot"]),
        })
    if values.get("tff"):
        status["sources"]["tff"].update({
            "data_as_of": values["tff"][-1]["d"], "rows": len(values["tff"]),
            "content_sha256": content_sha256(values["tff"]),
        })
    if values.get("fx"):
        status["sources"]["fx"].update({
            "data_as_of": values["fx"][0][-1]["d"], "rows": len(values["fx"][0]),
            "max_daily_change_pct": values["fx"][1],
            "content_sha256": content_sha256(values["fx"][0]),
        })
    if values.get("fed"):
        status["sources"]["fed"].update({
            "data_as_of": values["fed"][3], "rows": 2,
            "content_sha256": content_sha256(values["fed"]),
        })

    required = ("cot", "tff", "fx", "fed", "boj")
    if any(name in errors for name in required):
        status["error_code"] = "required-source-failed"
        atomic_write(status_path, status)
        log("publication bloquee : sources requises en echec (%s)" % ", ".join(
            name for name in required if name in errors
        ))
        return 1 if previous else 2

    cot = values["cot"]
    tff = values["tff"]
    fx, max_fx_change = values["fx"]
    fed, lower, upper, fed_as_of, fed_source = values["fed"]

    position_revision_fields = ("long", "short", "net", "oi") \
        if previous.get("schema_version") == 3 else ("net", "oi")
    if previous and previous.get("schema_version") != 3:
        log("migration du snapshot schema %s : comparaison historique limitee a net/oi" % (
            previous.get("schema_version", "inconnu")
        ))
    revision_specs = {
        "cot": (previous.get("cot"), cot, position_revision_fields, 14),
        "tff": (previous.get("tff"), tff, position_revision_fields, 14),
        "fx": (previous.get("fx"), fx, ("v",), 10),
    }
    revisions = {}
    for name, spec in revision_specs.items():
        revised, historical = detect_revisions(*spec)
        revisions[name] = revised
        status["sources"][name]["revised_dates"] = revised
        if historical:
            log("%s : %d revision(s) historique(s), promotion bloquee" % (name, len(historical)))
            status["sources"][name].update({
                "status": "failed",
                "error_code": "historical-revision",
                "historical_revisions": historical,
            })
            errors[name] = QualityError("historical-revision", "%s : revision historique" % name)
    if errors:
        status["error_code"] = "historical-revision"
        atomic_write(status_path, status)
        return 1 if previous else 2

    for name, value in (("cot", cot), ("tff", tff), ("fx", fx), ("fed", values["fed"])):
        status["sources"][name]["content_sha256"] = content_sha256(value)
    status["sources"]["cot"].update({"data_as_of": cot[-1]["d"], "rows": len(cot)})
    status["sources"]["tff"].update({"data_as_of": tff[-1]["d"], "rows": len(tff)})
    status["sources"]["fx"].update({
        "data_as_of": fx[-1]["d"], "rows": len(fx), "max_daily_change_pct": max_fx_change
    })
    status["sources"]["fed"].update({"data_as_of": fed_as_of, "rows": 2})

    out = {
        "schema_version": 3,
        "generated": checked_at,
        "published_at": checked_at,
        "data_as_of": {
            "cot": cot[-1]["d"],
            "tff": tff[-1]["d"],
            "fx": fx[-1]["d"],
            "fed": fed_as_of,
            "boj": boj_policy["data_as_of"],
        },
        "health": {"status": "ok", "checks": {name: True for name in required}},
        "rates": {
            "fed": fed,
            "fed_lower": lower,
            "fed_upper": upper,
            "fed_source": fed_source,
            "boj": boj_rate,
            "boj_source": "official-guideline-confirmed" if boj_confirmation else "verified-config",
            **({"boj_announced": boj_policy["rate"], "boj_effective_from": boj_policy["effective_from"]}
               if boj_policy.get("effective_from") else {}),
        },
        "fx": fx,
        "cot": cot,
        "tff": tff,
        "spot": fx[-1]["v"],
        "methodology": methodology_contract(),
        "sources": {
            "cot": _public_source_meta(
                "CFTC", "Legacy Futures Only 6dca-aqww / 097741", cot[-1]["d"], checked_at,
                CFTC_URL, status["sources"]["cot"], len(cot), revisions["cot"],
            ),
            "tff": _public_source_meta(
                "CFTC", "TFF Futures Only gpe5-46if / Leveraged Money / 097741", tff[-1]["d"],
                checked_at, CFTC_TFF_URL, status["sources"]["tff"], len(tff), revisions["tff"],
            ),
            "fx": _public_source_meta(
                "BCE", "Euro foreign exchange reference rates", fx[-1]["d"], checked_at,
                ECB_API_URL, status["sources"]["fx"], len(fx), revisions["fx"],
            ),
            "fed": _public_source_meta(
                "Federal Reserve Bank of St. Louis", "DFEDTARL / DFEDTARU", fed_as_of, checked_at,
                FRED_API_URL if fed_source == "fred-api" else FRED_CSV_URL,
                status["sources"]["fed"], 2,
            ),
            "boj": {
                "status": "verified-config",
                "provider": "Bank of Japan",
                "dataset": "Uncollateralized overnight call rate target",
                "data_as_of": boj_policy["data_as_of"],
                "last_checked_at": checked_at,
                "last_success_at": checked_at,
                "source_url": boj_policy["source_url"],
                "policy_index_url": values["boj_index"]["index_url"],
                "schedule_url": BOJ_SCHEDULE_URL,
                "next_review_due": next_review,
                "document_sha256": boj_policy["source_sha256"],
                "content_sha256": content_sha256(boj_policy),
                "verification_mode": "official-guideline-confirmed" if boj_confirmation else "reviewed-policy",
                "reviewed_policy_as_of": reviewed_boj_as_of,
                **({"policy_confirmation": boj_confirmation} if boj_confirmation else {}),
            },
        },
    }
    out["data_fingerprint"] = snapshot_fingerprint(out)

    if previous.get("schema_version") == 3 and previous.get("data_fingerprint") == out["data_fingerprint"]:
        status.update({
            "status": "ok",
            "published": False,
            "reason": "unchanged",
            "last_successful_publication_at": previous.get("published_at") or previous.get("generated"),
        })
        atomic_write(status_path, status)
        log("aucun changement de donnees, data.json conserve octet pour octet")
        return 0

    atomic_write(candidate_path, out)
    import verify_snapshot  # import tardif pour garder un seul contrat de validation

    validation_errors, _ = verify_snapshot.validate(candidate_path, now=now)
    if validation_errors:
        for error in validation_errors:
            log("candidat invalide : %s" % error)
        status.update({"error_code": "candidate-invalid", "validation_error_count": len(validation_errors)})
        atomic_write(status_path, status)
        return 1 if previous else 2

    atomic_promote(candidate_path, OUT_PATH)
    status.update({
        "status": "ok",
        "published": True,
        "reason": "data-changed",
        "last_successful_publication_at": checked_at,
        "published_fingerprint": out["data_fingerprint"],
    })
    atomic_write(status_path, status)
    log("promotion %s : cot=%s tff=%s fx=%s fed=%s boj=%s" % (
        OUT_PATH, cot[-1]["d"], tff[-1]["d"], fx[-1]["d"], fed_as_of,
        boj_policy["data_as_of"]
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
