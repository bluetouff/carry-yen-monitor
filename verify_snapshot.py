#!/usr/bin/env python3
"""Valide le contrat public d'un snapshot YCT avant promotion ou apres generation."""

import argparse
import json
import sys
from datetime import date, datetime, timezone


def parse_date(value):
    return date.fromisoformat(str(value)[:10])


def parse_timestamp(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def require(condition, message, errors):
    if not condition:
        errors.append(message)


def validate(path, max_generated_age_hours=8, allow_degraded=False, now=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        return ["JSON illisible : %s" % exc], None

    require(data.get("schema_version") == 2, "schema_version doit valoir 2", errors)
    try:
        generated_age = (now - parse_timestamp(data.get("generated"))).total_seconds() / 3600
        require(0 <= generated_age <= max_generated_age_hours,
                "snapshot genere il y a %.1f h" % generated_age, errors)
    except Exception:  # noqa: BLE001
        errors.append("generated absent ou invalide")

    health = data.get("health", {})
    if not isinstance(health, dict):
        errors.append("health doit etre un objet")
        health = {}
    if not allow_degraded:
        require(health.get("status") == "ok", "health.status n'est pas ok", errors)

    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        errors.append("sources doit etre un objet")
        sources = {}
    for key in ("cot", "fx", "fed", "boj"):
        meta = sources.get(key, {})
        if not meta:
            errors.append("metadonnees source absentes : %s" % key)
            continue
        if not isinstance(meta, dict):
            errors.append("metadonnees source invalides : %s" % key)
            continue
        if not allow_degraded:
            require(meta.get("status") in ("fresh", "verified-config"),
                    "source %s en statut %s" % (key, meta.get("status")), errors)
        require(bool(meta.get("data_as_of")), "source %s sans data_as_of" % key, errors)
        require(str(meta.get("source_url", "")).startswith("https://"),
                "source %s sans URL HTTPS" % key, errors)

    cot = data.get("cot", [])
    if not isinstance(cot, list):
        errors.append("cot doit etre une liste")
        cot = []
    require(len(cot) >= 156, "historique CFTC inferieur a 156 semaines", errors)
    if cot:
        cot_dates = [row.get("d") if isinstance(row, dict) else None for row in cot]
        valid_cot_dates = all(isinstance(value, str) and value for value in cot_dates)
        require(valid_cot_dates, "dates CFTC absentes ou invalides", errors)
        if valid_cot_dates:
            require(cot_dates == sorted(cot_dates), "historique CFTC non trie", errors)
            require(len(cot_dates) == len(set(cot_dates)), "dates CFTC dupliquees", errors)
        try:
            span = (parse_date(cot_dates[-1]) - parse_date(cot_dates[0])).days
            lag = (now.date() - parse_date(cot_dates[-1])).days
            require(span >= 3 * 365 - 14, "historique CFTC inferieur a trois ans", errors)
            require(0 <= lag <= 12, "observation CFTC trop ancienne (%d jours)" % lag, errors)
        except Exception:  # noqa: BLE001
            errors.append("dates CFTC invalides")

    fx = data.get("fx", [])
    if not isinstance(fx, list):
        errors.append("fx doit etre une liste")
        fx = []
    require(len(fx) >= 240, "historique BCE inferieur a 240 observations", errors)
    if fx:
        fx_dates = [row.get("d") if isinstance(row, dict) else None for row in fx]
        valid_fx_dates = all(isinstance(value, str) and value for value in fx_dates)
        require(valid_fx_dates, "dates BCE absentes ou invalides", errors)
        if valid_fx_dates:
            require(fx_dates == sorted(fx_dates), "historique BCE non trie", errors)
            require(len(fx_dates) == len(set(fx_dates)), "dates BCE dupliquees", errors)
        try:
            span = (parse_date(fx_dates[-1]) - parse_date(fx_dates[0])).days
            lag = (now.date() - parse_date(fx_dates[-1])).days
            require(span >= 350, "historique BCE inferieur a douze mois", errors)
            require(0 <= lag <= 7, "reference BCE trop ancienne (%d jours)" % lag, errors)
        except Exception:  # noqa: BLE001
            errors.append("dates BCE invalides")
        try:
            require(abs(float(data.get("spot")) - float(fx[-1]["v"])) < 0.00001,
                    "spot different de la derniere reference BCE", errors)
        except Exception:  # noqa: BLE001
            errors.append("spot ou derniere reference BCE invalide")

    rates = data.get("rates", {})
    if not isinstance(rates, dict):
        errors.append("rates doit etre un objet")
        rates = {}
    for key in ("fed", "boj"):
        try:
            value = float(rates[key])
            require(-1 <= value <= 15, "taux %s hors plage" % key, errors)
        except Exception:  # noqa: BLE001
            errors.append("taux %s absent ou invalide" % key)

    return errors, data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--max-generated-age-hours", type=float, default=8)
    parser.add_argument("--allow-degraded", action="store_true")
    args = parser.parse_args()
    errors, data = validate(
        args.path,
        max_generated_age_hours=args.max_generated_age_hours,
        allow_degraded=args.allow_degraded,
    )
    if errors:
        for error in errors:
            print("[yct-verify] ECHEC : %s" % error, file=sys.stderr)
        return 1
    print(
        "[yct-verify] OK : generated=%s cot=%d (%s) fx=%d (%s) fed=%.3f boj=%.3f"
        % (
            data["generated"], len(data["cot"]), data["cot"][-1]["d"],
            len(data["fx"]), data["fx"][-1]["d"],
            float(data["rates"]["fed"]), float(data["rates"]["boj"]),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
