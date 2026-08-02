import csv
import io
import json
import hashlib
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_snapshot  # noqa: E402
import verify_snapshot  # noqa: E402


def last_tuesday(today):
    return today - timedelta(days=(today.weekday() - 1) % 7)


def cftc_fixture(count=170, code="097741"):
    latest = last_tuesday(datetime.now(timezone.utc).date())
    rows = []
    for index in range(count):
        report_date = latest - timedelta(weeks=index)
        rows.append(
            {
                "report_date_as_yyyy_mm_dd": report_date.isoformat() + "T00:00:00.000",
                "cftc_contract_market_code": code,
                "noncomm_positions_long_all": "101271",
                "noncomm_positions_short_all": str(264683 - index),
                "open_interest_all": "432366",
            }
        )
    return rows


class BuilderTests(unittest.TestCase):
    def test_cftc_query_targets_only_contract_097741_and_keeps_170_weeks(self):
        captured = {}

        def fake_get(url, headers=None):
            captured["url"] = url
            return cftc_fixture()

        with mock.patch.object(build_snapshot, "http_get_json", side_effect=fake_get):
            rows = build_snapshot.fetch_cftc()

        query = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
        self.assertEqual(query["$where"], ["cftc_contract_market_code = '097741'"])
        self.assertNotIn("market_and_exchange_names", captured["url"])
        self.assertEqual(len(rows), 170)
        self.assertEqual(rows[-1]["net"], -163412)
        self.assertEqual(len({row["d"] for row in rows}), 170)

    def test_cftc_rejects_an_unexpected_contract_code(self):
        with mock.patch.object(
            build_snapshot, "http_get_json", return_value=cftc_fixture(code="097742")
        ):
            with self.assertRaisesRegex(RuntimeError, "contrat inattendu"):
                build_snapshot.fetch_cftc()

    def test_ecb_cross_is_derived_from_the_primary_csv(self):
        today = datetime.now(timezone.utc).date()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["KEY", "CURRENCY", "TIME_PERIOD", "OBS_VALUE"])
        for offset in range(373):
            current = today - timedelta(days=offset)
            writer.writerow(["EXR.D.USD.EUR.SP00.A", "USD", current.isoformat(), "1.1485"])
            writer.writerow(["EXR.D.JPY.EUR.SP00.A", "JPY", current.isoformat(), "184.03"])

        with mock.patch.object(build_snapshot, "http_get_text", return_value=output.getvalue()):
            rows = build_snapshot.fetch_fx()

        self.assertGreaterEqual(len(rows), 372)
        self.assertEqual(rows[-1]["d"], today.isoformat())
        self.assertAlmostEqual(rows[-1]["v"], round(184.03 / 1.1485, 4))
        self.assertIn("data-api.ecb.europa.eu", build_snapshot.ECB_API_URL)

    def test_fed_rate_uses_latest_common_fred_date(self):
        lower = {"2026-07-30": 3.5, "2026-07-31": 3.5}
        upper = {"2026-07-30": 3.75, "2026-07-31": 3.75}
        with mock.patch.object(
            build_snapshot, "fred_csv_observations", side_effect=[lower, upper]
        ):
            rate, low, high, as_of, source = build_snapshot.fetch_fed_rate()
        self.assertEqual((rate, low, high), (3.625, 3.5, 3.75))
        self.assertEqual(as_of, "2026-07-31")
        self.assertEqual(source, "fred-csv")

    def test_failed_source_preserves_its_last_success_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "data.json")
            previous = {
                "generated": "2026-08-01T00:00:00Z",
                "rates": {"fed": 3.625, "boj": 1.0},
                "cot": [{"d": "2026-07-28", "net": -163412, "oi": 432366}],
                "fx": [{"d": "2026-07-31", "v": 160.2351}],
                "spot": 160.2351,
                "sources": {
                    "cot": {
                        "status": "fresh",
                        "data_as_of": "2026-07-28",
                        "last_success_at": "2026-08-01T00:00:00Z",
                        "source_url": build_snapshot.CFTC_URL,
                    }
                },
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(previous, handle)

            with mock.patch.object(build_snapshot, "OUT_PATH", path), mock.patch.object(
                build_snapshot, "fetch_cftc", side_effect=RuntimeError("source down")
            ), mock.patch.object(
                build_snapshot, "fetch_fx", return_value=[{"d": "2026-07-31", "v": 160.2351}]
            ), mock.patch.object(
                build_snapshot,
                "fetch_fed_rate",
                return_value=(3.625, 3.5, 3.75, "2026-07-31", "fred-csv"),
            ):
                result = build_snapshot.main()

            with open(path, "r", encoding="utf-8") as handle:
                updated = json.load(handle)
            self.assertEqual(result, 1)
            self.assertEqual(updated["sources"]["cot"]["status"], "cached")
            self.assertEqual(
                updated["sources"]["cot"]["last_success_at"], "2026-08-01T00:00:00Z"
            )
            self.assertNotEqual(updated["generated"], updated["sources"]["cot"]["last_success_at"])
            self.assertEqual(updated["health"]["status"], "degraded")

    def test_first_run_does_not_publish_a_partial_critical_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "data.json")
            with mock.patch.object(build_snapshot, "OUT_PATH", path), mock.patch.object(
                build_snapshot, "fetch_cftc", side_effect=RuntimeError("source down")
            ), mock.patch.object(
                build_snapshot, "fetch_fx", return_value=[{"d": "2026-07-31", "v": 160.2351}]
            ), mock.patch.object(
                build_snapshot,
                "fetch_fed_rate",
                return_value=(3.625, 3.5, 3.75, "2026-07-31", "fred-csv"),
            ):
                result = build_snapshot.main()
            self.assertEqual(result, 2)
            self.assertFalse(os.path.exists(path))


class ContractTests(unittest.TestCase):
    def test_frontend_uses_accurate_labels_and_current_boj_default(self):
        web = ROOT / "web" if (ROOT / "web").exists() else ROOT
        html = (web / "index.html").read_text(encoding="utf-8")
        javascript = (web / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("fonds à effet de levier, CTA, macro", html)
        self.assertIn("value=\"1.00\"", html)
        self.assertIn("contrat CME 097741", html)
        self.assertIn("pas taille mondiale du carry", html)
        self.assertIn('fmt(diff,3)+" pts"', javascript)
        self.assertIn('fmt(diff*100,1)+" pb', javascript)
        self.assertIn("de notionnel net", javascript)

    def test_frontend_assets_are_cache_busted_by_their_content_hash(self):
        web = ROOT / "web" if (ROOT / "web").exists() else ROOT
        html = (web / "index.html").read_text(encoding="utf-8")
        for name in ("app.css", "app.js"):
            digest = hashlib.sha256((web / name).read_bytes()).hexdigest()[:12]
            match = re.search(r'%s\?v=([0-9a-f]{12})' % re.escape(name), html)
            self.assertIsNotNone(match, "%s sans version de contenu" % name)
            self.assertEqual(match.group(1), digest)

    def test_apache_revalidates_html_and_assets(self):
        config = (ROOT / "deploy" / "yct.l0g.fr.conf").read_text(encoding="utf-8")
        activation = (ROOT / "deploy" / "activate-release.sh").read_text(encoding="utf-8")
        self.assertIn('Header always set Cache-Control "no-cache, max-age=0, must-revalidate"', config)
        self.assertIn("apache2ctl configtest", activation)

    def test_builder_no_longer_depends_on_frankfurter(self):
        source = (ROOT / "build_snapshot.py").read_text(encoding="utf-8")
        self.assertNotIn("frankfurter", source.lower())
        self.assertIn("cftc_contract_market_code", source)

    def test_verifier_rejects_cached_sources(self):
        now = datetime.now(timezone.utc)
        latest_tue = last_tuesday(now.date())
        cot = [
            {"d": (latest_tue - timedelta(weeks=169 - index)).isoformat(), "net": -1, "oi": 1}
            for index in range(170)
        ]
        fx = [
            {"d": (now.date() - timedelta(days=371 - index)).isoformat(), "v": 160.0}
            for index in range(372)
        ]
        source = {
            "status": "fresh",
            "data_as_of": now.date().isoformat(),
            "source_url": "https://example.test/source",
        }
        snapshot = {
            "schema_version": 2,
            "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "health": {"status": "degraded"},
            "sources": {key: dict(source) for key in ("cot", "fx", "fed", "boj")},
            "cot": cot,
            "fx": fx,
            "spot": 160.0,
            "rates": {"fed": 3.625, "boj": 1.0},
        }
        snapshot["sources"]["cot"]["status"] = "cached"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(snapshot, handle)
            handle.flush()
            errors, _ = verify_snapshot.validate(handle.name, now=now)
        self.assertTrue(any("health.status" in error for error in errors))
        self.assertTrue(any("source cot" in error for error in errors))

    def test_verifier_fails_cleanly_on_malformed_source_metadata(self):
        now = datetime.now(timezone.utc)
        snapshot = {
            "schema_version": 2,
            "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "health": [],
            "sources": {"cot": []},
            "cot": "invalid",
            "fx": "invalid",
            "rates": [],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(snapshot, handle)
            handle.flush()
            errors, _ = verify_snapshot.validate(handle.name, now=now)
        self.assertTrue(any("health doit etre un objet" in error for error in errors))
        self.assertTrue(any("cot doit etre une liste" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
