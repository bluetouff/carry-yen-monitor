#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_snapshot.py - regenere /var/lib/yct/data.json pour le moniteur Carry Yen.

Sources primaires, recuperees cote serveur :
  - CFTC Commitments of Traders, Legacy Futures Only (Socrata 6dca-aqww)
  - BCE, fichier historique officiel des taux de reference EUR (USD/JPY derive)
  - FRED, Federal Reserve Bank of St. Louis (DFEDTARU / DFEDTARL)
  - BoJ, valeur configuree et datee d'apres la derniere decision officielle

Conception :
  - stdlib uniquement (urllib), aucun paquet a installer
  - ecriture atomique (tmp + os.replace)
  - validation du grain, de la profondeur historique et de la fraicheur
  - metadonnees par source : un bloc conserve ne peut pas paraitre fraichement charge

Variables d'environnement :
  OUT_PATH             chemin du data.json (defaut /var/lib/yct/data.json)
  BOJ_RATE             cible BoJ en % (defaut 1.00)
  BOJ_RATE_AS_OF       date de la decision BoJ, YYYY-MM-DD (defaut 2026-07-31)
  BOJ_SOURCE_URL       URL HTTPS de la decision BoJ verifiee
  FED_RATE             milieu de cible Fed si FRED echoue (defaut 3.625)
  FED_RATE_AS_OF       date de la decision Fed de repli (defaut 2026-07-29)
  FED_RATE_SOURCE_URL  URL HTTPS de la decision Fed de repli
  FRED_API_KEY         optionnel ; l'export CSV officiel sans cle reste disponible
  SOCRATA_APP_TOKEN    jeton CFTC optionnel (quotas plus larges)
"""

import csv
import io
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

OUT_PATH = os.environ.get("OUT_PATH", "/var/lib/yct/data.json")
BOJ_RATE = float(os.environ.get("BOJ_RATE", "1.00"))
BOJ_RATE_AS_OF = os.environ.get("BOJ_RATE_AS_OF", "2026-07-31").strip()
BOJ_SOURCE_URL = os.environ.get(
    "BOJ_SOURCE_URL", "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260731a.pdf"
).strip()
FED_RATE_FALLBACK = float(os.environ.get("FED_RATE", "3.625"))
FED_RATE_AS_OF = os.environ.get("FED_RATE_AS_OF", "2026-07-29").strip()
FED_RATE_SOURCE_URL = os.environ.get(
    "FED_RATE_SOURCE_URL",
    "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm",
).strip()
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "").strip()

UA = "yct-snapshot/2.0 (+https://yct.l0g.fr)"
CONTRACT_YEN = 12_500_000
CONTRACT_CODE = "097741"
CFTC_LIMIT = 170
CFTC_MIN_WEEKS = 156
MAX_BYTES = 8 * 1024 * 1024

CFTC_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
ECB_API_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD+JPY.EUR.SP00.A"
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
def log(msg):
    print("[yct] %s" % msg, file=sys.stderr, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_date(value):
    return date.fromisoformat(str(value)[:10])


def age_days(value, today=None):
    today = today or utc_now().date()
    return (today - parse_iso_date(value)).days


def http_get(url, headers=None, timeout=25, attempts=3):
    """Retourne les octets d'une URL HTTPS sans jamais journaliser la query."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("schema non https refuse : %s" % parsed.scheme)
    host = parsed.netloc
    last = None
    request_headers = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.geturl().split("://", 1)[0] != "https":
                    raise ValueError("redirection hors https refusee")
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("reponse trop volumineuse (> %d octets)" % MAX_BYTES)
                return raw
        except Exception as exc:  # noqa: BLE001
            last = exc
            log("retry %d/%d sur %s : %s" % (i + 1, attempts, host, exc))
    raise last


def http_get_json(url, headers=None, timeout=25, attempts=3):
    return json.loads(http_get(url, headers, timeout, attempts).decode("utf-8"))


def http_get_text(url, headers=None, timeout=25, attempts=3):
    return http_get(url, headers, timeout, attempts).decode("utf-8-sig")


def validate_cftc(rows, today=None):
    if len(rows) < CFTC_MIN_WEEKS:
        raise RuntimeError(
            "CFTC : historique incomplet (%d semaines, minimum %d)"
            % (len(rows), CFTC_MIN_WEEKS)
        )
    dates = [row["d"] for row in rows]
    if len(dates) != len(set(dates)):
        raise RuntimeError("CFTC : dates dupliquees au grain hebdomadaire")
    span = (parse_iso_date(dates[-1]) - parse_iso_date(dates[0])).days
    if span < 3 * 365 - 14:
        raise RuntimeError("CFTC : profondeur inferieure a trois ans (%d jours)" % span)
    lag = age_days(dates[-1], today=today)
    if lag < 0 or lag > 12:
        raise RuntimeError("CFTC : derniere observation trop ancienne (%d jours)" % lag)


def fetch_cftc():
    """Net Legacy non-commercial du seul futur yen CME 097741, sur 170 semaines."""
    params = {
        "$where": "cftc_contract_market_code = '%s'" % CONTRACT_CODE,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(CFTC_LIMIT),
        "$select": (
            "report_date_as_yyyy_mm_dd,cftc_contract_market_code,"
            "noncomm_positions_long_all,noncomm_positions_short_all,open_interest_all"
        ),
    }
    url = CFTC_URL + "?" + urllib.parse.urlencode(params)
    headers = {"X-App-Token": SOCRATA_APP_TOKEN} if SOCRATA_APP_TOKEN else None
    raw = http_get_json(url, headers=headers)
    rows = []
    for item in raw:
        try:
            code = str(item.get("cftc_contract_market_code") or "").strip()
            if code != CONTRACT_CODE:
                raise RuntimeError("CFTC : contrat inattendu %r" % code)
            report_date = (item.get("report_date_as_yyyy_mm_dd") or "")[:10]
            long_count = float(item.get("noncomm_positions_long_all"))
            short_count = float(item.get("noncomm_positions_short_all"))
            open_interest = float(item.get("open_interest_all") or 0)
            if not report_date:
                continue
            rows.append(
                {
                    "d": report_date,
                    "net": int(long_count - short_count),
                    "oi": int(open_interest),
                }
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda row: row["d"])
    validate_cftc(rows)
    log(
        "CFTC ok : contrat %s, %d semaines, dernier %s net=%d oi=%d"
        % (CONTRACT_CODE, len(rows), rows[-1]["d"], rows[-1]["net"], rows[-1]["oi"])
    )
    return rows


def validate_fx(rows, today=None):
    if len(rows) < 240:
        raise RuntimeError("BCE : historique incomplet (%d observations)" % len(rows))
    dates = [row["d"] for row in rows]
    if len(dates) != len(set(dates)):
        raise RuntimeError("BCE : dates dupliquees")
    span = (parse_iso_date(dates[-1]) - parse_iso_date(dates[0])).days
    if span < 350:
        raise RuntimeError("BCE : profondeur inferieure a douze mois (%d jours)" % span)
    lag = age_days(dates[-1], today=today)
    if lag < 0 or lag > 7:
        raise RuntimeError("BCE : derniere reference trop ancienne (%d jours)" % lag)


def fetch_fx():
    """USD/JPY derive directement des references EUR/USD et EUR/JPY de la BCE."""
    end = utc_now().date()
    start = end - timedelta(days=372)
    url = ECB_API_URL + "?" + urllib.parse.urlencode(
        {"startPeriod": start.isoformat(), "endPeriod": end.isoformat(), "format": "csvdata"}
    )
    reader = csv.DictReader(io.StringIO(http_get_text(url)))
    by_date = {}
    for item in reader:
        try:
            currency = str(item.get("CURRENCY") or "").strip()
            report_date = str(item.get("TIME_PERIOD") or "")[:10]
            value = float(item.get("OBS_VALUE"))
            if currency not in ("USD", "JPY") or not report_date or value <= 0:
                continue
            by_date.setdefault(report_date, {})[currency] = value
        except (TypeError, ValueError):
            continue
    rows = []
    for report_date, rates in by_date.items():
        if "USD" in rates and "JPY" in rates:
            rows.append({"d": report_date, "v": round(rates["JPY"] / rates["USD"], 4)})
    rows.sort(key=lambda row: row["d"])
    validate_fx(rows)
    log(
        "BCE ok : %d references, USD/JPY %s = %.4f"
        % (len(rows), rows[-1]["d"], rows[-1]["v"])
    )
    return rows


def fred_api_observations(series_id):
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "14",
    }
    payload = http_get_json(FRED_API_URL + "?" + urllib.parse.urlencode(params))
    values = {}
    for item in payload.get("observations", []):
        value = item.get("value")
        if value not in (".", None, ""):
            values[str(item.get("date"))[:10]] = float(value)
    return values


def fred_csv_observations(series_id):
    url = FRED_CSV_URL + "?" + urllib.parse.urlencode({"id": series_id})
    reader = csv.DictReader(io.StringIO(http_get_text(url)))
    values = {}
    for item in reader:
        value = item.get(series_id)
        report_date = item.get("observation_date") or item.get("DATE")
        if value not in (".", None, "") and report_date:
            values[str(report_date)[:10]] = float(value)
    return values


def latest_common_rate(lower_values, upper_values):
    common = sorted(set(lower_values).intersection(upper_values))
    if not common:
        raise RuntimeError("FRED : aucune date commune pour la cible Fed")
    report_date = common[-1]
    lower = lower_values[report_date]
    upper = upper_values[report_date]
    if lower > upper or upper - lower > 3:
        raise RuntimeError("FRED : fourchette Fed incoherente [%.3f ; %.3f]" % (lower, upper))
    lag = age_days(report_date)
    if lag < 0 or lag > 10:
        raise RuntimeError("FRED : derniere cible trop ancienne (%d jours)" % lag)
    return round((lower + upper) / 2.0, 3), lower, upper, report_date


def fetch_fed_rate():
    """Milieu de la cible Fed, API FRED avec cle puis export CSV officiel sans cle."""
    if FRED_API_KEY:
        try:
            result = latest_common_rate(
                fred_api_observations("DFEDTARL"),
                fred_api_observations("DFEDTARU"),
            )
            log("FRED API ok : cible [%.3f ; %.3f], milieu %.3f au %s" % (result[1], result[2], result[0], result[3]))
            return result + ("fred-api",)
        except Exception as exc:  # noqa: BLE001
            log("FRED API indisponible (%s), essai de l'export CSV officiel" % exc)
    result = latest_common_rate(
        fred_csv_observations("DFEDTARL"),
        fred_csv_observations("DFEDTARU"),
    )
    log("FRED CSV ok : cible [%.3f ; %.3f], milieu %.3f au %s" % (result[1], result[2], result[0], result[3]))
    return result + ("fred-csv",)


def source_meta(status, provider, dataset, data_as_of, checked_at, source_url):
    return {
        "status": status,
        "provider": provider,
        "dataset": dataset,
        "data_as_of": data_as_of,
        "last_checked_at": checked_at,
        "last_success_at": checked_at if status == "fresh" else None,
        "source_url": source_url,
    }


def cached_meta(prev, key, provider, dataset, data_as_of, checked_at, source_url):
    previous = prev.get("sources", {}).get(key, {})
    meta = dict(previous) if isinstance(previous, dict) else {}
    meta.update(
        {
            "status": "cached",
            "provider": meta.get("provider", provider),
            "dataset": meta.get("dataset", dataset),
            "data_as_of": meta.get("data_as_of") or data_as_of,
            "last_checked_at": checked_at,
            "last_success_at": meta.get("last_success_at") or prev.get("generated"),
            "source_url": meta.get("source_url", source_url),
        }
    )
    return meta


def dated_config_meta(provider, dataset, data_as_of, checked_at, source_url, max_age_days):
    try:
        lag = age_days(data_as_of)
        status = "verified-config" if 0 <= lag <= max_age_days else "stale-config"
    except (TypeError, ValueError):
        status = "stale-config"
    return {
        "status": status,
        "provider": provider,
        "dataset": dataset,
        "data_as_of": data_as_of or None,
        "last_checked_at": checked_at,
        "last_success_at": None,
        "source_url": source_url,
    }


def load_existing():
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as snapshot:
            return json.load(snapshot)
    except Exception:  # noqa: BLE001
        return {}


def atomic_write(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".data.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as snapshot:
            json.dump(data, snapshot, ensure_ascii=False, separators=(",", ":"))
            snapshot.flush()
            os.fsync(snapshot.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    checked_at = iso_now()
    prev = load_existing()
    previous_rates = prev.get("rates", {}) if isinstance(prev.get("rates"), dict) else {}
    out = {
        "schema_version": 2,
        "generated": checked_at,
        "rates": dict(previous_rates),
        "fx": prev.get("fx", []),
        "cot": prev.get("cot", []),
        "spot": prev.get("spot"),
        "sources": {},
    }
    ok = {"cot": False, "fx": False, "fed": False, "boj": False}

    try:
        out["cot"] = fetch_cftc()
        ok["cot"] = True
        out["sources"]["cot"] = source_meta(
            "fresh", "CFTC", "Legacy Futures Only 6dca-aqww / 097741",
            out["cot"][-1]["d"], checked_at, CFTC_URL,
        )
    except Exception as exc:  # noqa: BLE001
        log("CFTC ECHEC, conservation de la section precedente : %s" % exc)
        previous_as_of = out["cot"][-1].get("d") if out["cot"] else None
        out["sources"]["cot"] = cached_meta(
            prev, "cot", "CFTC", "Legacy Futures Only 6dca-aqww / 097741",
            previous_as_of, checked_at, CFTC_URL,
        )

    try:
        out["fx"] = fetch_fx()
        out["spot"] = out["fx"][-1]["v"]
        ok["fx"] = True
        out["sources"]["fx"] = source_meta(
            "fresh", "BCE", "Euro foreign exchange reference rates",
            out["fx"][-1]["d"], checked_at, ECB_API_URL,
        )
    except Exception as exc:  # noqa: BLE001
        log("BCE ECHEC, conservation de la section precedente : %s" % exc)
        previous_as_of = out["fx"][-1].get("d") if out["fx"] else None
        out["sources"]["fx"] = cached_meta(
            prev, "fx", "BCE", "Euro foreign exchange reference rates",
            previous_as_of, checked_at, ECB_API_URL,
        )

    try:
        fed, lower, upper, fed_as_of, fed_source = fetch_fed_rate()
        out["rates"].update(
            {"fed": fed, "fed_lower": lower, "fed_upper": upper, "fed_source": fed_source}
        )
        ok["fed"] = True
        out["sources"]["fed"] = source_meta(
            "fresh", "Federal Reserve Bank of St. Louis", "DFEDTARL / DFEDTARU",
            fed_as_of, checked_at, FRED_API_URL if fed_source == "fred-api" else FRED_CSV_URL,
        )
    except Exception as exc:  # noqa: BLE001
        log("FRED ECHEC, conservation ou repli sur la configuration : %s" % exc)
        if previous_rates.get("fed") is not None:
            out["rates"]["fed"] = previous_rates["fed"]
            out["rates"]["fed_source"] = "cached"
            out["sources"]["fed"] = cached_meta(
                prev, "fed", "Federal Reserve", "Federal funds target range",
                FED_RATE_AS_OF, checked_at, FED_RATE_SOURCE_URL,
            )
        else:
            out["rates"].update({"fed": FED_RATE_FALLBACK, "fed_source": "fallback-config"})
            out["sources"]["fed"] = source_meta(
                "fallback", "Federal Reserve", "Federal funds target range",
                FED_RATE_AS_OF, checked_at, FED_RATE_SOURCE_URL,
            )

    out["rates"]["boj"] = BOJ_RATE
    out["rates"]["boj_source"] = "verified-config"
    out["sources"]["boj"] = dated_config_meta(
        "Bank of Japan", "Uncollateralized overnight call rate target",
        BOJ_RATE_AS_OF, checked_at, BOJ_SOURCE_URL, max_age_days=70,
    )
    ok["boj"] = out["sources"]["boj"]["status"] == "verified-config"

    if not out["cot"] or not out["fx"]:
        log("Snapshot essentiel incomplet et aucun bloc anterieur exploitable. Abandon.")
        return 2

    out["health"] = {"status": "ok" if all(ok.values()) else "degraded", "checks": ok}
    atomic_write(OUT_PATH, out)
    log(
        "ecrit %s (health=%s, cot=%s, fx=%s, fed=%s, boj=%s)"
        % (OUT_PATH, out["health"]["status"], ok["cot"], ok["fx"], ok["fed"], ok["boj"])
    )
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
