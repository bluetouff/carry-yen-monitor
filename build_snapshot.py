#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_snapshot.py - regenere /var/lib/yct/data.json pour le moniteur Carry Yen.

Sources (toutes publiques, recuperees cote serveur) :
  - CFTC Commitments of Traders, Legacy Futures Only (Socrata 6dca-aqww)
  - BCE via Frankfurter (taux de reference quotidiens, USD/JPY)
  - FRED (optionnel) : DFEDTARU / DFEDTARL pour la mediane Fed, si FRED_API_KEY est defini

Conception :
  - stdlib uniquement (urllib), aucun paquet a installer
  - ecriture atomique (tmp + os.replace)
  - degradation gracieuse : si une source echoue, on conserve la section
    precedente du data.json existant plutot que de vider la page

Variables d'environnement (toutes optionnelles) :
  OUT_PATH           chemin du data.json          (defaut /var/lib/yct/data.json)
  BOJ_RATE           taux directeur BoJ en %      (defaut 0.75)
  FED_RATE           mediane Fed en % si pas FRED (defaut 3.625)
  FRED_API_KEY       active la recup Fed via FRED (sinon FED_RATE)
  SOCRATA_APP_TOKEN  jeton CFTC optionnel (quotas plus larges)
"""

import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

OUT_PATH = os.environ.get("OUT_PATH", "/var/lib/yct/data.json")
BOJ_RATE = float(os.environ.get("BOJ_RATE", "0.75"))
FED_RATE_FALLBACK = float(os.environ.get("FED_RATE", "3.625"))
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "").strip()

UA = "yct-snapshot/1.0 (+https://yct.l0g.fr)"
CONTRACT_YEN = 12_500_000


MAX_BYTES = 8 * 1024 * 1024  # garde-fou memoire : 8 Mio max par reponse


def http_get_json(url, headers=None, timeout=25, attempts=3):
    # https obligatoire : pas de bascule en clair, meme via une redirection
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("schema non https refuse : %s" % parsed.scheme)
    host = parsed.netloc
    last = None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.geturl().split("://", 1)[0] != "https":
                    raise ValueError("redirection hors https refusee")
                raw = r.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("reponse trop volumineuse (> %d octets)" % MAX_BYTES)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            # on ne journalise que le host, jamais le chemin ni la query (cles)
            log("retry %d/%d sur %s : %s" % (i + 1, attempts, host, e))
    raise last


def log(msg):
    print("[yct] %s" % msg, file=sys.stderr, flush=True)


def fetch_cftc():
    """Net non-commercial sur le futur yen CME, ~3 ans d'historique hebdo."""
    where = "market_and_exchange_names like '%JAPANESE YEN%'"
    params = {
        "$where": where,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "170",
        "$select": ("report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
                    "noncomm_positions_short_all,open_interest_all"),
    }
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json?" + urllib.parse.urlencode(params)
    headers = {"X-App-Token": SOCRATA_APP_TOKEN} if SOCRATA_APP_TOKEN else None
    raw = http_get_json(url, headers=headers)
    rows = []
    for x in raw:
        try:
            d = (x.get("report_date_as_yyyy_mm_dd") or "")[:10]
            L = float(x.get("noncomm_positions_long_all"))
            S = float(x.get("noncomm_positions_short_all"))
            oi = float(x.get("open_interest_all") or 0)
            if not d:
                continue
            rows.append({"d": d, "net": int(L - S), "oi": int(oi)})
        except (TypeError, ValueError):
            continue
    # Le filtre large peut ramener plusieurs contrats "JAPANESE YEN" (standard + micro).
    # On ne garde, par date, que la ligne au plus gros open interest : le contrat
    # standard CME, seul reference pertinente du positionnement carry.
    by_date = {}
    for r in rows:
        cur = by_date.get(r["d"])
        if cur is None or r["oi"] > cur["oi"]:
            by_date[r["d"]] = r
    rows = sorted(by_date.values(), key=lambda r: r["d"])
    if not rows:
        raise RuntimeError("CFTC : aucune ligne exploitable")
    log("CFTC ok : %d semaines, dernier %s net=%d oi=%d"
        % (len(rows), rows[-1]["d"], rows[-1]["net"], rows[-1]["oi"]))
    return rows


def fetch_fx():
    """USD/JPY quotidien BCE sur ~1 an, ascendant."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=372)
    url = "https://api.frankfurter.dev/v1/%s..%s?base=USD&symbols=JPY" % (start.isoformat(), end.isoformat())
    j = http_get_json(url)
    rates = j.get("rates", {})
    rows = []
    for d in sorted(rates.keys()):
        v = rates[d].get("JPY")
        if v:
            rows.append({"d": d, "v": round(float(v), 4)})
    if not rows:
        raise RuntimeError("BCE : serie vide")
    log("BCE ok : %d points, spot %s = %s" % (len(rows), rows[-1]["d"], rows[-1]["v"]))
    return rows


def fred_latest(series_id):
    url = ("https://api.stlouisfed.org/fred/series/observations?"
           + urllib.parse.urlencode({
               "series_id": series_id,
               "api_key": FRED_API_KEY,
               "file_type": "json",
               "sort_order": "desc",
               "limit": "1",
           }))
    j = http_get_json(url)
    obs = j.get("observations", [])
    if obs and obs[0].get("value") not in (".", None, ""):
        return float(obs[0]["value"])
    raise RuntimeError("FRED %s : pas d'observation" % series_id)


def fetch_fed_rate():
    """Mediane Fed depuis FRED si une cle existe, sinon FED_RATE_FALLBACK."""
    if not FRED_API_KEY:
        return FED_RATE_FALLBACK, "config"
    try:
        upper = fred_latest("DFEDTARU")
        lower = fred_latest("DFEDTARL")
        med = round((upper + lower) / 2.0, 3)
        log("FRED ok : Fed cible [%.2f ; %.2f] mediane %.3f" % (lower, upper, med))
        return med, "fred"
    except Exception as e:  # noqa: BLE001
        log("FRED indisponible (%s), repli sur FED_RATE=%s" % (e, FED_RATE_FALLBACK))
        return FED_RATE_FALLBACK, "fallback"


def load_existing():
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def atomic_write(path, data):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".data.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    prev = load_existing()
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rates": dict(prev.get("rates", {})),
        "fx": prev.get("fx", []),
        "cot": prev.get("cot", []),
        "spot": prev.get("spot"),
    }
    ok = {"cot": False, "fx": False}

    try:
        out["cot"] = fetch_cftc()
        ok["cot"] = True
    except Exception as e:  # noqa: BLE001
        log("CFTC ECHEC, conservation de la section precedente : %s" % e)

    try:
        out["fx"] = fetch_fx()
        out["spot"] = out["fx"][-1]["v"]
        ok["fx"] = True
    except Exception as e:  # noqa: BLE001
        log("BCE ECHEC, conservation de la section precedente : %s" % e)

    fed, fed_src = fetch_fed_rate()
    out["rates"] = {"boj": BOJ_RATE, "fed": fed, "fed_source": fed_src}

    # Si on n'a jamais rien eu (premiere execution totalement vide), echec dur.
    if not out["cot"] and not out["fx"]:
        log("Aucune donnee disponible et aucun snapshot anterieur. Abandon.")
        return 2

    atomic_write(OUT_PATH, out)
    log("ecrit %s (cot=%s, fx=%s, fed=%s)" % (OUT_PATH, ok["cot"], ok["fx"], fed_src))
    # Code 0 si tout est frais, 1 si une source a manque (visible dans systemctl status sans bloquer).
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
