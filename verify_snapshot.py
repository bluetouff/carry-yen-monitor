#!/usr/bin/env python3
"""Valide les contrats public, operationnel et marche de YCT."""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

from yct_quality import (
    CFTC_CONTRACT_CODE,
    PRIMARY_SOURCE_URLS,
    QualityError,
    boj_config_state,
    boj_rate_at,
    content_sha256,
    load_boj_policy,
    load_calendar,
    methodology_contract,
    parse_timestamp,
    snapshot_fingerprint,
    validate_fx_rows,
    validate_position_rows,
)

REQUIRED_SOURCES = ("cot", "tff", "fx", "fed", "boj")


def require(condition, message, errors):
    if not condition:
        errors.append(message)


def _load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("racine JSON non objet")
    return data


def validate(
    path,
    max_generated_age_hours=120,
    allow_degraded=False,
    now=None,
    calendar_path=None,
    boj_policy_path=None,
):
    now = now or datetime.now(timezone.utc)
    errors = []
    try:
        data = _load_json(path)
    except Exception as exc:  # noqa: BLE001
        return ["JSON illisible : %s" % exc], None

    require(data.get("schema_version") == 3, "schema_version doit valoir 3", errors)
    try:
        generated = parse_timestamp(data.get("generated"))
        published = parse_timestamp(data.get("published_at"))
        require(generated == published, "generated doit etre l'alias de published_at", errors)
        generated_age = (now - generated).total_seconds() / 3600.0
        require(0 <= generated_age <= max_generated_age_hours,
                "snapshot publie il y a %.1f h" % generated_age, errors)
    except Exception:  # noqa: BLE001
        errors.append("generated ou published_at absent ou invalide")

    try:
        calendar = load_calendar(calendar_path)
    except Exception as exc:  # noqa: BLE001
        errors.append("calendrier officiel invalide : %s" % getattr(exc, "code", "erreur"))
        calendar = None
    try:
        boj_policy = load_boj_policy(boj_policy_path)
    except Exception as exc:  # noqa: BLE001
        errors.append("politique BoJ invalide : %s" % getattr(exc, "code", "erreur"))
        boj_policy = None

    health = data.get("health", {})
    if not isinstance(health, dict):
        errors.append("health doit etre un objet")
        health = {}
    if not allow_degraded:
        require(health.get("status") == "ok", "health.status n'est pas ok", errors)
    checks = health.get("checks", {}) if isinstance(health.get("checks"), dict) else {}
    for key in REQUIRED_SOURCES:
        require(checks.get(key) is True, "health.checks.%s n'est pas vrai" % key, errors)

    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        errors.append("sources doit etre un objet")
        sources = {}
    for key in REQUIRED_SOURCES:
        meta = sources.get(key, {})
        if not isinstance(meta, dict) or not meta:
            errors.append("metadonnees source absentes ou invalides : %s" % key)
            continue
        expected_status = "verified-config" if key == "boj" else "fresh"
        if not allow_degraded:
            require(meta.get("status") == expected_status,
                    "source %s en statut %s" % (key, meta.get("status")), errors)
        require(bool(meta.get("data_as_of")), "source %s sans data_as_of" % key, errors)
        require(str(meta.get("source_url", "")).startswith("https://"),
                "source %s sans URL HTTPS" % key, errors)
        require(len(str(meta.get("content_sha256", ""))) == 64,
                "source %s sans empreinte SHA-256" % key, errors)
    expected_providers = {
        "cot": "CFTC", "tff": "CFTC", "fx": "BCE",
        "fed": "Federal Reserve Bank of St. Louis", "boj": "Bank of Japan",
    }
    for key, provider in expected_providers.items():
        require(sources.get(key, {}).get("provider") == provider,
                "fournisseur %s inattendu" % key, errors)
    for key in ("cot", "tff", "fx"):
        require(sources.get(key, {}).get("source_url") == PRIMARY_SOURCE_URLS[key],
                "URL primaire %s inattendue" % key, errors)
    for key in ("cot", "tff"):
        require(CFTC_CONTRACT_CODE in str(sources.get(key, {}).get("dataset", "")),
                "contrat CFTC absent de la provenance %s" % key, errors)

    cot = data.get("cot")
    tff = data.get("tff")
    fx = data.get("fx")
    if calendar:
        for key, rows, label in (("cot", cot, "CFTC Legacy"), ("tff", tff, "CFTC TFF")):
            try:
                validate_position_rows(rows, now, calendar, label=label)
            except QualityError as exc:
                errors.append("%s : %s" % (key, exc))
        try:
            validate_fx_rows(fx, now)
        except QualityError as exc:
            errors.append("fx : %s" % exc)
    else:
        require(isinstance(cot, list), "cot doit etre une liste", errors)
        require(isinstance(tff, list), "tff doit etre une liste", errors)
        require(isinstance(fx, list), "fx doit etre une liste", errors)

    if isinstance(cot, list) and cot and isinstance(tff, list) and tff:
        require(cot[-1].get("d") == tff[-1].get("d"),
                "Legacy et TFF ne portent pas sur la meme semaine", errors)
    if isinstance(fx, list) and fx:
        try:
            require(abs(float(data.get("spot")) - float(fx[-1]["v"])) < 0.00001,
                    "spot different de la derniere reference BCE", errors)
        except Exception:  # noqa: BLE001
            errors.append("spot ou derniere reference BCE invalide")

    rates = data.get("rates", {})
    if not isinstance(rates, dict):
        errors.append("rates doit etre un objet")
        rates = {}
    try:
        fed = float(rates["fed"])
        lower = float(rates["fed_lower"])
        upper = float(rates["fed_upper"])
        require(all(math.isfinite(value) for value in (fed, lower, upper)), "taux Fed non fini", errors)
        require(lower <= upper and upper - lower <= 1, "fourchette Fed incoherente", errors)
        require(abs(fed - (lower + upper) / 2.0) < 0.0006, "milieu Fed incoherent", errors)
        fed_source = rates.get("fed_source")
        expected_fed_url = {
            "fred-api": PRIMARY_SOURCE_URLS["fed_api"],
            "fred-csv": PRIMARY_SOURCE_URLS["fed_csv"],
        }.get(fed_source)
        require(bool(expected_fed_url), "mode source Fed inattendu", errors)
        require(sources.get("fed", {}).get("source_url") == expected_fed_url,
                "URL primaire Fed inattendue", errors)
    except Exception:  # noqa: BLE001
        errors.append("taux Fed absent ou invalide")
    try:
        boj = float(rates["boj"])
        require(math.isfinite(boj) and -1 <= boj <= 15, "taux BoJ hors plage", errors)
        if boj_policy:
            require(boj == boj_rate_at(boj_policy, now), "taux BoJ different du contrat versionne a cette date", errors)
            if boj_policy.get("effective_from"):
                require(rates.get("boj_announced") == boj_policy["rate"],
                        "taux BoJ annonce incoherent", errors)
                require(rates.get("boj_effective_from") == boj_policy["effective_from"],
                        "date d'effet BoJ incoherente", errors)
    except Exception:  # noqa: BLE001
        errors.append("taux BoJ absent ou invalide")

    data_as_of = data.get("data_as_of", {})
    if not isinstance(data_as_of, dict):
        errors.append("data_as_of doit etre un objet")
        data_as_of = {}
    for key in REQUIRED_SOURCES:
        require(data_as_of.get(key) == sources.get(key, {}).get("data_as_of"),
                "data_as_of.%s incoherent" % key, errors)
    if isinstance(cot, list) and cot:
        require(data_as_of.get("cot") == cot[-1].get("d"), "date CFTC incoherente", errors)
    if isinstance(tff, list) and tff:
        require(data_as_of.get("tff") == tff[-1].get("d"), "date TFF incoherente", errors)
    if isinstance(fx, list) and fx:
        require(data_as_of.get("fx") == fx[-1].get("d"), "date BCE incoherente", errors)

    if calendar and sources.get("boj"):
        boj_status, next_review = boj_config_state(sources["boj"].get("data_as_of"), now, calendar)
        require(boj_status == "verified-config", "configuration BoJ a reverifier", errors)
        require(sources["boj"].get("next_review_due") == next_review,
                "prochaine revue BoJ incoherente", errors)
    if boj_policy and sources.get("boj"):
        require(data_as_of.get("boj") == boj_policy["data_as_of"],
                "date BoJ differente du contrat versionne", errors)
        require(sources["boj"].get("source_url") == boj_policy["source_url"],
                "URL BoJ differente du contrat versionne", errors)
        require(sources["boj"].get("document_sha256") == boj_policy["source_sha256"],
                "empreinte du document BoJ differente du contrat versionne", errors)
        require(sources["boj"].get("schedule_url") == PRIMARY_SOURCE_URLS["boj_schedule"],
                "URL du calendrier BoJ inattendue", errors)
        require(str(sources["boj"].get("policy_index_url", "")).startswith(
            "https://www.boj.or.jp/en/mopo/mpmdeci/state_"
        ), "URL de l'index BoJ inattendue", errors)

    methodology = data.get("methodology", {})
    if not isinstance(methodology, dict):
        errors.append("methodology doit etre un objet")
        methodology = {}
    require(methodology == methodology_contract(),
            "contrat methodologique inattendu", errors)

    expected_fingerprint = snapshot_fingerprint(data)
    require(data.get("data_fingerprint") == expected_fingerprint,
            "empreinte du contenu du snapshot incoherente", errors)
    if isinstance(cot, list):
        require(sources.get("cot", {}).get("content_sha256") == content_sha256(cot),
                "empreinte CFTC incoherente", errors)
    if isinstance(tff, list):
        require(sources.get("tff", {}).get("content_sha256") == content_sha256(tff),
                "empreinte TFF incoherente", errors)
    if isinstance(fx, list):
        require(sources.get("fx", {}).get("content_sha256") == content_sha256(fx),
                "empreinte BCE incoherente", errors)
    if all(key in rates for key in ("fed", "fed_lower", "fed_upper", "fed_source")):
        fed_payload = (
            float(rates["fed"]), float(rates["fed_lower"]), float(rates["fed_upper"]),
            data_as_of.get("fed"), rates["fed_source"],
        )
        require(sources.get("fed", {}).get("content_sha256") == content_sha256(fed_payload),
                "empreinte Fed incoherente", errors)
    if "boj" in rates and boj_policy:
        require(sources.get("boj", {}).get("content_sha256") == content_sha256(boj_policy),
                "empreinte BoJ incoherente", errors)

    return errors, data


def validate_status(path, snapshot, max_checked_age_hours=8, now=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    try:
        status = _load_json(path)
    except Exception as exc:  # noqa: BLE001
        return ["status JSON illisible : %s" % exc], None
    require(status.get("schema_version") == 1, "status.schema_version doit valoir 1", errors)
    try:
        checked_age = (now - parse_timestamp(status.get("checked_at"))).total_seconds() / 3600.0
        require(0 <= checked_age <= max_checked_age_hours,
                "dernier controle il y a %.1f h" % checked_age, errors)
    except Exception:  # noqa: BLE001
        errors.append("status.checked_at absent ou invalide")
    require(status.get("status") == "ok", "dernier controle de sources degrade", errors)
    expected_publication = snapshot.get("published_at") or snapshot.get("generated")
    require(status.get("last_successful_publication_at") == expected_publication,
            "status et snapshot ne designent pas la meme publication", errors)
    source_status = status.get("sources", {}) if isinstance(status.get("sources"), dict) else {}
    for key in REQUIRED_SOURCES:
        expected = "verified-config" if key == "boj" else "fresh"
        require(source_status.get(key, {}).get("status") == expected,
                "dernier controle %s en statut %s" % (key, source_status.get(key, {}).get("status")), errors)
    return errors, status


def validate_market(path, max_age_hours=96, now=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    try:
        market = _load_json(path)
    except Exception as exc:  # noqa: BLE001
        return ["market JSON illisible : %s" % exc], None
    require(market.get("schema_version") == 1, "market.schema_version doit valoir 1", errors)
    require(market.get("provider") == "Massive Market Data", "fournisseur market inattendu", errors)
    require(market.get("pair") == "USD/JPY", "paire market inattendue", errors)
    try:
        bid, ask, mid = (float(market[key]) for key in ("bid", "ask", "mid"))
        require(0 < bid <= mid <= ask, "quote market incoherente", errors)
        require(40 <= mid <= 300 and ask - bid <= 1, "quote market hors plage", errors)
        age = (now - parse_timestamp(market.get("data_as_of"))).total_seconds() / 3600.0
        require(-1 <= age <= max_age_hours, "quote market agee de %.1f h" % age, errors)
    except Exception:  # noqa: BLE001
        errors.append("quote market absente ou invalide")
    require(str(market.get("source_url", "")).startswith("https://massive.com/"),
            "URL source Massive invalide", errors)
    return errors, market


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--status-path")
    parser.add_argument("--market-path")
    parser.add_argument("--max-generated-age-hours", type=float, default=120)
    parser.add_argument("--max-checked-age-hours", type=float, default=8)
    parser.add_argument("--allow-degraded", action="store_true")
    parser.add_argument("--calendar-path")
    parser.add_argument("--boj-policy-path")
    args = parser.parse_args()
    errors, data = validate(
        args.path,
        max_generated_age_hours=args.max_generated_age_hours,
        allow_degraded=args.allow_degraded,
        calendar_path=args.calendar_path,
        boj_policy_path=args.boj_policy_path,
    )
    status = None
    if not errors and args.status_path:
        status_errors, status = validate_status(
            args.status_path, data, max_checked_age_hours=args.max_checked_age_hours
        )
        errors.extend(status_errors)
    if args.market_path and os.path.exists(args.market_path):
        market_errors, _ = validate_market(args.market_path)
        errors.extend(market_errors)
    if errors:
        for error in errors:
            print("[yct-verify] ECHEC : %s" % error, file=sys.stderr)
        return 1
    print(
        "[yct-verify] OK : published=%s checked=%s cot=%d (%s) tff=%d (%s) "
        "fx=%d (%s) fed=%.3f boj=%.3f"
        % (
            data["published_at"], status.get("checked_at") if status else "non-verifie",
            len(data["cot"]), data["cot"][-1]["d"],
            len(data["tff"]), data["tff"][-1]["d"],
            len(data["fx"]), data["fx"][-1]["d"],
            float(data["rates"]["fed"]), float(data["rates"]["boj"]),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
