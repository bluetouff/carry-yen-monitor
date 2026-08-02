#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_sample.py - genere un echantillon de demonstration web/data.json.

But : permettre d'ouvrir l'application sans serveur ni cle d'API (clone puis
ouverture de web/index.html, ou GitHub Pages). Les donnees sont SYNTHETIQUES,
ancrees sur des reperes historiques reels pour raconter la meme histoire que
la version live, mais elles ne sont pas a jour. En production, c'est
build_snapshot.py qui ecrit le vrai data.json a partir des sources primaires.

Usage :
  python3 gen_sample.py          # ecrit web/data.json (echantillon)
  Pour des donnees reelles, utiliser build_snapshot.py (voir RUNBOOK.md).
"""

import json
import math
import os
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "web", "data.json")


def interp(anchors, x):
    """Interpolation lineaire entre des points (position 0..1, valeur)."""
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= x <= x1:
            t = 0 if x1 == x0 else (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return anchors[-1][1]


def jitter(seed, amp):
    """Bruit deterministe pseudo-aleatoire, sans dependance externe."""
    s = math.sin(seed * 12.9898) * 43758.5453
    return (s - math.floor(s) - 0.5) * 2 * amp


def gen_fx(n=260):
    # USD/JPY : ~143 il y a un an, montee vers ~160, consolidation sous 160.
    anchors = [(0.0, 143.0), (0.35, 148.0), (0.6, 154.0), (0.8, 159.0), (1.0, 159.9)]
    today = datetime.now(timezone.utc).date()
    rows = []
    d = today - timedelta(days=int(n * 1.45))
    idx = 0
    while len(rows) < n and d <= today:
        if d.weekday() < 5:  # jours ouvres
            x = idx / max(1, n - 1)
            v = interp(anchors, min(1.0, x)) + jitter(idx + 1, 0.9)
            rows.append({"d": d.isoformat(), "v": round(v, 2)})
            idx += 1
        d += timedelta(days=1)
    return rows


def gen_cot(weeks=170):
    # Net non-commercial : short profond mi-2024 (-184k), bascule longue debut
    # 2025 (+179k), retour short mi-2026 (-130k). Reperes reels approximes.
    anchors = [(0.0, -120000), (0.18, -184000), (0.30, 66000), (0.52, 179000),
               (0.78, 0), (0.92, -90000), (1.0, -130000)]
    oi_anchors = [(0.0, 305000), (0.3, 340000), (0.6, 360000), (1.0, 500000)]
    today = datetime.now(timezone.utc).date()
    # dernier mardi
    last_tue = today - timedelta(days=(today.weekday() - 1) % 7)
    rows = []
    for i in range(weeks):
        x = i / (weeks - 1)
        net = interp(anchors, x) + jitter(i + 100, 6000)
        oi = interp(oi_anchors, x) + jitter(i + 200, 8000)
        d = last_tue - timedelta(weeks=(weeks - 1 - i))
        rows.append({"d": d.isoformat(), "net": int(round(net)), "oi": int(round(oi))})
    return rows


def main():
    fx = gen_fx()
    cot = gen_cot()
    out = {
        "sample": True,
        "schema_version": 2,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "health": {"status": "degraded", "checks": {"cot": False, "fx": False, "fed": False, "boj": False}},
        "rates": {"boj": 1.0, "fed": 3.625, "fed_source": "sample", "boj_source": "sample"},
        "fx": fx,
        "cot": cot,
        "spot": fx[-1]["v"],
        "sources": {
            "cot": {"status": "sample", "data_as_of": cot[-1]["d"]},
            "fx": {"status": "sample", "data_as_of": fx[-1]["d"]},
            "fed": {"status": "sample", "data_as_of": datetime.now(timezone.utc).date().isoformat()},
            "boj": {"status": "sample", "data_as_of": datetime.now(timezone.utc).date().isoformat()},
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("echantillon ecrit : %s (%d points FX, %d semaines CFTC, spot %.2f)"
          % (OUT, len(fx), len(cot), out["spot"]))


if __name__ == "__main__":
    main()
