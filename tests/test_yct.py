import csv
import copy
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_snapshot  # noqa: E402
import verify_snapshot  # noqa: E402
import yct_quality  # noqa: E402

FIXED_NOW = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
CALENDAR = yct_quality.load_calendar(str(ROOT / "config" / "source-calendars.json"))
CURRENT_BOJ_POLICY = yct_quality.load_boj_policy(str(ROOT / "config" / "boj-policy.json"))
# Historical fixture: contract tests must not depend on the next policy decision.
BOJ_POLICY = {
    "schema_version": 1, "rate": 1.0, "data_as_of": "2026-07-31",
    "source_url": "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260731a.pdf",
    "source_sha256": "f862844f0b476f44bcd76044475755f2bae5c22fea72fd3425bc946ce32e6470",
}


def position_api_fixture(kind="legacy", count=170, code="097741"):
    latest = date(2026, 7, 28)
    rows = []
    for index in range(count):
        report_date = latest - timedelta(weeks=index)
        if kind == "legacy":
            long_count, short_count = 101271, 264683 - index
            fields = {
                "noncomm_positions_long_all": str(long_count),
                "noncomm_positions_short_all": str(short_count),
            }
        else:
            long_count, short_count = 76752, 178742 - index
            fields = {
                "lev_money_positions_long": str(long_count),
                "lev_money_positions_short": str(short_count),
            }
        rows.append({
            "report_date_as_yyyy_mm_dd": report_date.isoformat() + "T00:00:00.000",
            "cftc_contract_market_code": code,
            "open_interest_all": "432366",
            **fields,
        })
    return rows


def position_rows(kind="legacy"):
    raw = position_api_fixture(kind=kind)
    long_field = "noncomm_positions_long_all" if kind == "legacy" else "lev_money_positions_long"
    short_field = "noncomm_positions_short_all" if kind == "legacy" else "lev_money_positions_short"
    rows = [{
        "d": item["report_date_as_yyyy_mm_dd"][:10],
        "long": int(item[long_field]),
        "short": int(item[short_field]),
        "net": int(item[long_field]) - int(item[short_field]),
        "oi": int(item["open_interest_all"]),
    } for item in raw]
    return sorted(rows, key=lambda row: row["d"])


def fx_rows():
    rows = []
    current = date(2025, 7, 25)
    end = date(2026, 7, 31)
    index = 0
    while current <= end:
        if current.weekday() < 5:
            rows.append({"d": current.isoformat(), "v": round(156 + index * 0.016, 4)})
            index += 1
        current += timedelta(days=1)
    rows[-1]["v"] = 160.2351
    return rows


def fx_csv_fixture():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["KEY", "CURRENCY", "TIME_PERIOD", "OBS_VALUE"])
    for row in fx_rows():
        writer.writerow(["EXR.D.USD.EUR.SP00.A", "USD", row["d"], "1.1485"])
        writer.writerow(["EXR.D.JPY.EUR.SP00.A", "JPY", row["d"], str(row["v"] * 1.1485)])
    return output.getvalue()


def run_paths(directory):
    policy_path = os.path.join(directory, "boj-policy.json")
    Path(policy_path).write_text(json.dumps(BOJ_POLICY), encoding="utf-8")
    return {
        "BOJ_POLICY_PATH": policy_path,
        "OUT_PATH": os.path.join(directory, "data.json"),
        "STATUS_PATH": os.path.join(directory, "status.json"),
        "CANDIDATE_PATH": os.path.join(directory, "candidate.json"),
        "MARKET_PATH": os.path.join(directory, "market.json"),
    }


def run_successfully(paths, now=FIXED_NOW, cot=None, boj_error=None):
    cot = cot or position_rows("legacy")
    with mock.patch.object(build_snapshot, "OUT_PATH", paths["OUT_PATH"]), mock.patch.dict(
        os.environ, paths, clear=False
    ), mock.patch.object(build_snapshot, "fetch_cftc", return_value=cot), mock.patch.object(
        build_snapshot, "fetch_tff", return_value=position_rows("tff")
    ), mock.patch.object(
        build_snapshot, "fetch_fx", return_value=(fx_rows(), 0.2)
    ), mock.patch.object(
        build_snapshot, "fetch_fed_rate", return_value=(3.625, 3.5, 3.75, "2026-07-31", "fred-csv")
    ), mock.patch.object(
        build_snapshot, "fetch_boj_index", side_effect=boj_error, return_value={
            "latest_statement_date": "2026-07-31",
            "index_url": "https://www.boj.or.jp/en/mopo/mpmdeci/state_2026/index.htm",
            "statement_url": BOJ_POLICY["source_url"],
        }
    ):
        return build_snapshot.main(now=now)


class CalendarTests(unittest.TestCase):
    def test_cftc_calendar_includes_holiday_delays_and_expected_tuesday(self):
        self.assertIn("2026-07-06", CALENDAR["cftc"]["release_dates"])
        self.assertEqual(yct_quality.expected_cftc_report_date(FIXED_NOW, CALENDAR), date(2026, 7, 28))
        with self.assertRaisesRegex(yct_quality.QualityError, "expiration"):
            yct_quality.expected_cftc_report_date(
                datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc), CALENDAR
            )

    def test_cftc_calendar_can_version_a_holiday_report_date_override(self):
        calendar = copy.deepcopy(CALENDAR)
        calendar["cftc"]["report_date_overrides"]["2026-07-31"] = "2026-07-27"
        self.assertEqual(yct_quality.expected_cftc_report_date(FIXED_NOW, calendar), date(2026, 7, 27))

    def test_ecb_expected_date_uses_target_business_days(self):
        self.assertEqual(yct_quality.expected_ecb_reference_date(FIXED_NOW), date(2026, 7, 31))
        christmas = datetime(2026, 12, 26, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(yct_quality.expected_ecb_reference_date(christmas), date(2026, 12, 24))

    def test_cftc_release_grace_accepts_new_report_only_after_release(self):
        release = datetime(2026, 9, 18, 19, 30, tzinfo=timezone.utc)
        old = position_rows()
        new = position_rows()
        for rows, latest in ((old, date(2026, 9, 8)), (new, date(2026, 9, 15))):
            delta = latest - date.fromisoformat(rows[-1]["d"])
            for row in rows:
                row["d"] = (date.fromisoformat(row["d"]) + delta).isoformat()
        for now, old_ok, new_ok in (
            (release - timedelta(seconds=1), True, False),
            (release, True, True),
            (release + timedelta(minutes=89, seconds=59), True, True),
            (release + timedelta(minutes=90), False, True),
        ):
            for rows, accepted in ((old, old_ok), (new, new_ok)):
                with self.subTest(now=now, latest=rows[-1]["d"]):
                    if accepted:
                        yct_quality.validate_position_rows(rows, now, CALENDAR)
                    else:
                        with self.assertRaises(yct_quality.QualityError):
                            yct_quality.validate_position_rows(rows, now, CALENDAR)

    def test_boj_configuration_expires_after_next_meeting_grace(self):
        before = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)
        after = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(yct_quality.boj_config_state("2026-07-31", before, CALENDAR),
                         ("verified-config", "2026-09-18"))
        self.assertEqual(yct_quality.boj_config_state("2026-07-31", after, CALENDAR)[0],
                         "stale-config")

    def test_boj_configuration_accepts_a_reviewed_unscheduled_decision(self):
        now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            yct_quality.boj_config_state("2026-08-15", now, CALENDAR),
            ("verified-config", "2026-09-18"),
        )

    def test_versioned_boj_policy_is_the_single_public_contract(self):
        self.assertEqual(CURRENT_BOJ_POLICY["rate"], 1.25)
        self.assertEqual(CURRENT_BOJ_POLICY["data_as_of"], "2026-09-18")
        self.assertEqual(CURRENT_BOJ_POLICY["effective_from"], "2026-09-24")
        self.assertEqual(CURRENT_BOJ_POLICY["previous_rate"], 1.0)
        self.assertTrue(CURRENT_BOJ_POLICY["source_url"].startswith("https://www.boj.or.jp/"))
        self.assertEqual(len(CURRENT_BOJ_POLICY["source_sha256"]), 64)

    def test_announced_boj_hike_applies_only_on_its_effective_date_in_japan(self):
        before = datetime(2026, 9, 23, 14, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=1)
        self.assertEqual(yct_quality.boj_rate_at(CURRENT_BOJ_POLICY, before), 1.0)
        self.assertEqual(yct_quality.boj_rate_at(CURRENT_BOJ_POLICY, after), 1.25)
        with self.assertRaises(yct_quality.QualityError):
            yct_quality.boj_rate_at(CURRENT_BOJ_POLICY, FIXED_NOW)

    def test_invalid_boj_transitions_fail_closed(self):
        for changes in ({"previous_rate": None}, {"previous_rate": float("nan")},
                        {"effective_from": "2026-09-17"}, {"effective_from": "2026-09-24garbage"},
                        {"source_url": "https://www.boj.or.jp@evil.test/decision.pdf"}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "policy.json"
                path.write_text(json.dumps(dict(CURRENT_BOJ_POLICY, **changes)))
                with self.assertRaises(yct_quality.QualityError):
                    yct_quality.load_boj_policy(str(path))


class SourceTests(unittest.TestCase):
    def test_legacy_query_targets_only_097741_and_keeps_exactly_170_weeks(self):
        captured = {}

        def fake_get(url, headers=None, client=None):
            captured["url"] = url
            return position_api_fixture("legacy")

        with mock.patch.object(build_snapshot, "http_get_json", side_effect=fake_get):
            rows = build_snapshot.fetch_cftc(now=FIXED_NOW, calendar=CALENDAR)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
        self.assertEqual(query["$where"], ["cftc_contract_market_code = '097741'"])
        self.assertEqual(len(rows), 170)
        self.assertEqual(rows[-1]["net"], -163412)

    def test_tff_is_a_distinct_official_series_for_leveraged_money(self):
        captured = {}

        def fake_get(url, headers=None, client=None):
            captured["url"] = url
            return position_api_fixture("tff")

        with mock.patch.object(build_snapshot, "http_get_json", side_effect=fake_get):
            rows = build_snapshot.fetch_tff(now=FIXED_NOW, calendar=CALENDAR)
        self.assertIn("gpe5-46if", captured["url"])
        self.assertIn("lev_money_positions_long", captured["url"])
        self.assertEqual(rows[-1]["net"], -101990)

    def test_position_parser_rejects_any_malformed_required_row(self):
        fixture = position_api_fixture("legacy")
        fixture[15]["noncomm_positions_long_all"] = "n/a"
        with mock.patch.object(build_snapshot, "http_get_json", return_value=fixture):
            with self.assertRaisesRegex(yct_quality.QualityError, "champ"):
                build_snapshot.fetch_cftc(now=FIXED_NOW, calendar=CALENDAR)

    def test_ecb_cross_is_derived_from_paired_primary_rows(self):
        with mock.patch.object(build_snapshot, "http_get_text", return_value=fx_csv_fixture()):
            rows, max_change = build_snapshot.fetch_fx(now=FIXED_NOW)
        self.assertGreaterEqual(len(rows), 240)
        self.assertEqual(rows[-1]["d"], "2026-07-31")
        self.assertAlmostEqual(rows[-1]["v"], 160.2351, places=3)
        self.assertLess(max_change, 1)

    def test_fed_range_must_be_recent_and_narrow(self):
        lower = {"2026-07-31": 3.5}
        upper = {"2026-07-31": 3.75}
        self.assertEqual(build_snapshot.latest_common_rate(lower, upper, now=FIXED_NOW),
                         (3.625, 3.5, 3.75, "2026-07-31"))
        with self.assertRaisesRegex(yct_quality.QualityError, "fourchette"):
            build_snapshot.latest_common_rate(lower, {"2026-07-31": 5.0}, now=FIXED_NOW)

    def test_massive_quote_is_separate_and_never_contains_the_key(self):
        payload = {"last": {"bid": 160.20, "ask": 160.22, "timestamp": int(FIXED_NOW.timestamp() * 1000)}}
        captured = {}

        def fake_get(url, headers=None, client=None):
            captured["url"] = url
            return payload

        with mock.patch.object(build_snapshot, "MASSIVE_API_KEY", "secret-test-value"), mock.patch.object(
            build_snapshot, "http_get_json", side_effect=fake_get
        ):
            market = build_snapshot.fetch_massive_quote(now=FIXED_NOW)
        self.assertEqual(market["provider"], "Massive Market Data")
        self.assertAlmostEqual(market["mid"], 160.21)
        self.assertNotIn("secret-test-value", json.dumps(market))
        self.assertIn("secret-test-value", captured["url"])

    def test_boj_index_detects_the_configured_primary_statement(self):
        document = '''
        <a href="/en/mopo/mpmdeci/mpr_2026/k260731a.pdf">Statement on Monetary Policy [PDF]</a>
        <a href="/en/mopo/mpmdeci/mpr_2026/k260616a.pdf">Change in the Guideline for Money Market Operations [PDF]</a>
        '''
        pdf = b"%PDF-1.7 test fixture"
        policy = dict(BOJ_POLICY, source_sha256=hashlib.sha256(pdf).hexdigest())
        with mock.patch.object(build_snapshot, "http_get_text", return_value=document), mock.patch.object(
            build_snapshot, "http_get", return_value=pdf
        ):
            result = build_snapshot.fetch_boj_index(policy=policy)
        self.assertEqual(result["latest_statement_date"], "2026-07-31")
        self.assertEqual(result["statement_url"], BOJ_POLICY["source_url"])

    def test_boj_index_quarantines_a_new_unscheduled_decision(self):
        document = '''
        <a href="/en/mopo/mpmdeci/mpr_2026/k260918a.pdf">Statement on Monetary Policy [PDF]</a>
        <a href="/en/mopo/mpmdeci/mpr_2026/k260731a.pdf">Statement on Monetary Policy [PDF]</a>
        '''
        with mock.patch.object(build_snapshot, "http_get_text", return_value=document):
            with self.assertRaisesRegex(yct_quality.QualityError, "nouvelle decision"):
                build_snapshot.fetch_boj_index(policy=BOJ_POLICY)

    def test_boj_index_rejects_a_changed_primary_document(self):
        document = '''
        <a href="/en/mopo/mpmdeci/mpr_2026/k260731a.pdf">Statement on Monetary Policy [PDF]</a>
        '''
        with mock.patch.object(build_snapshot, "http_get_text", return_value=document), mock.patch.object(
            build_snapshot, "http_get", return_value=b"%PDF-1.7 changed"
        ):
            with self.assertRaisesRegex(yct_quality.QualityError, "empreinte"):
                build_snapshot.fetch_boj_index(policy=BOJ_POLICY)


class HttpClientTests(unittest.TestCase):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return "https://example.test/data"

        def read(self, size):
            return b"ok"

    def test_retry_uses_a_bounded_backoff_for_transient_errors(self):
        client = build_snapshot.HttpClient(
            deadline=time.monotonic() + 10, sleep=lambda delay: None, rng=mock.Mock(random=lambda: 0)
        )
        with mock.patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("down"), self.Response()]):
            self.assertEqual(client.get("https://example.test/data"), b"ok")
        self.assertEqual(client.attempts, 2)

    def test_deterministic_http_404_is_not_retried(self):
        error = urllib.error.HTTPError("https://example.test/data", 404, "not found", {}, io.BytesIO())
        client = build_snapshot.HttpClient(deadline=time.monotonic() + 10, sleep=lambda delay: None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(build_snapshot.SourceError, "HTTP 404"):
                client.get("https://example.test/data")
        error.close()
        self.assertEqual(client.attempts, 1)


class TransactionTests(unittest.TestCase):
    def test_effective_date_promotes_new_rate_even_when_market_data_is_unchanged(self):
        before = datetime(2026, 8, 2, 14, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            policy = dict(BOJ_POLICY, schema_version=2, effective_from="2026-08-03", previous_rate=0.75)
            Path(paths["BOJ_POLICY_PATH"]).write_text(json.dumps(policy))
            self.assertEqual(run_successfully(paths, now=before), 0)
            old = json.loads(Path(paths["OUT_PATH"]).read_text())
            self.assertEqual(old["rates"]["boj"], 0.75)
            self.assertEqual(old["rates"]["boj_announced"], 1.0)
            errors, _ = verify_snapshot.validate(paths["OUT_PATH"], now=after,
                                                  boj_policy_path=paths["BOJ_POLICY_PATH"])
            self.assertIn("taux BoJ different du contrat versionne a cette date", errors)
            self.assertEqual(run_successfully(paths, now=after), 0)
            new = json.loads(Path(paths["OUT_PATH"]).read_text())
            self.assertEqual(new["rates"]["boj"], 1.0)
            self.assertNotEqual(old["data_fingerprint"], new["data_fingerprint"])
            self.assertNotEqual(old["generated"], new["generated"])
            for key in ("cot", "tff", "fx"):
                self.assertEqual(old[key], new[key])
            body = Path(paths["OUT_PATH"]).read_bytes()
            self.assertEqual(run_successfully(paths, now=after + timedelta(minutes=1)), 0)
            self.assertEqual(body, Path(paths["OUT_PATH"]).read_bytes())

    def test_unreviewed_boj_decision_preserves_data_and_exposes_precise_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            body = Path(paths["OUT_PATH"]).read_bytes()
            error = yct_quality.QualityError("boj-new-decision", "nouvelle decision BoJ a examiner")
            self.assertEqual(run_successfully(paths, boj_error=error), 1)
            self.assertEqual(body, Path(paths["OUT_PATH"]).read_bytes())
            status = json.loads(Path(paths["STATUS_PATH"]).read_text())
            self.assertEqual(status["sources"]["boj"]["error_code"], "boj-new-decision")
            self.assertFalse(status["published"])

    def test_schema_two_migration_compares_only_fields_that_existed(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            legacy_snapshot = {
                "schema_version": 2,
                "generated": "2026-08-02T07:00:00Z",
                "cot": [
                    {"d": row["d"], "net": row["net"], "oi": row["oi"]}
                    for row in position_rows("legacy")
                ],
                "fx": fx_rows(),
                "rates": {"fed": 3.625, "boj": 1.0},
                "spot": 160.2351,
                "sources": {},
            }
            Path(paths["OUT_PATH"]).write_text(json.dumps(legacy_snapshot), encoding="utf-8")
            self.assertEqual(run_successfully(paths), 0)
            migrated = json.loads(Path(paths["OUT_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["cot"][-1]["long"], 101271)
        self.assertEqual(migrated["cot"][-1]["short"], 264683)

    def test_first_healthy_run_publishes_schema_three_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            data = json.loads(Path(paths["OUT_PATH"]).read_text(encoding="utf-8"))
            status = json.loads(Path(paths["STATUS_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["cot"][-1]["net"], -163412)
        self.assertEqual(data["tff"][-1]["net"], -101990)
        self.assertEqual(data["data_as_of"]["fx"], "2026-07-31")
        self.assertTrue(status["published"])
        self.assertEqual(status["status"], "ok")

    def test_failed_source_never_changes_last_known_good_data(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            before = Path(paths["OUT_PATH"]).read_bytes()
            later = FIXED_NOW + timedelta(hours=1)
            with mock.patch.object(build_snapshot, "OUT_PATH", paths["OUT_PATH"]), mock.patch.dict(
                os.environ, paths, clear=False
            ), mock.patch.object(build_snapshot, "fetch_cftc", side_effect=RuntimeError("source down")), mock.patch.object(
                build_snapshot, "fetch_tff", return_value=position_rows("tff")
            ), mock.patch.object(
                build_snapshot, "fetch_fx", return_value=(fx_rows(), 0.2)
            ), mock.patch.object(
                build_snapshot, "fetch_fed_rate", return_value=(3.625, 3.5, 3.75, "2026-07-31", "fred-csv")
            ), mock.patch.object(
                build_snapshot, "fetch_boj_index", return_value={
                    "latest_statement_date": "2026-07-31",
                    "index_url": "https://www.boj.or.jp/en/mopo/mpmdeci/state_2026/index.htm",
                    "statement_url": BOJ_POLICY["source_url"],
                }
            ):
                self.assertEqual(build_snapshot.main(now=later), 1)
            after = Path(paths["OUT_PATH"]).read_bytes()
            status = json.loads(Path(paths["STATUS_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual(before, after)
        self.assertEqual(status["status"], "degraded")
        self.assertEqual(status["sources"]["cot"]["status"], "failed")
        self.assertEqual(status["sources"]["tff"]["data_as_of"], "2026-07-28")
        self.assertEqual(len(status["sources"]["tff"]["content_sha256"]), 64)
        self.assertEqual(status["last_successful_publication_at"], "2026-08-02T08:00:00Z")

    def test_unchanged_sources_leave_data_byte_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            before = Path(paths["OUT_PATH"]).read_bytes()
            self.assertEqual(run_successfully(paths, now=FIXED_NOW + timedelta(hours=1)), 0)
            after = Path(paths["OUT_PATH"]).read_bytes()
            status = json.loads(Path(paths["STATUS_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual(before, after)
        self.assertFalse(status["published"])
        self.assertEqual(status["reason"], "unchanged")

    def test_old_historical_revision_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            before = Path(paths["OUT_PATH"]).read_bytes()
            revised = position_rows("legacy")
            revised[0]["long"] += 1
            revised[0]["net"] += 1
            self.assertEqual(run_successfully(paths, now=FIXED_NOW + timedelta(hours=1), cot=revised), 1)
            status = json.loads(Path(paths["STATUS_PATH"]).read_text(encoding="utf-8"))
            after = Path(paths["OUT_PATH"]).read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(status["sources"]["cot"]["error_code"], "historical-revision")

    def test_old_gross_revision_is_quarantined_even_when_net_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            before = Path(paths["OUT_PATH"]).read_bytes()
            revised = position_rows("legacy")
            revised[0]["long"] += 1
            revised[0]["short"] += 1
            self.assertEqual(run_successfully(
                paths, now=FIXED_NOW + timedelta(hours=1), cot=revised
            ), 1)
            after = Path(paths["OUT_PATH"]).read_bytes()
            status = json.loads(Path(paths["STATUS_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual(before, after)
        self.assertEqual(status["sources"]["cot"]["error_code"], "historical-revision")


class ContractTests(unittest.TestCase):
    def test_verifier_accepts_the_complete_snapshot_and_runtime_status(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            errors, data = verify_snapshot.validate(paths["OUT_PATH"], now=FIXED_NOW, boj_policy_path=paths["BOJ_POLICY_PATH"])
            status_errors, _ = verify_snapshot.validate_status(paths["STATUS_PATH"], data, now=FIXED_NOW)
        self.assertEqual(errors, [])
        self.assertEqual(status_errors, [])

    def test_verifier_rejects_tff_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            data = json.loads(Path(paths["OUT_PATH"]).read_text(encoding="utf-8"))
            data["tff"][-1]["net"] += 1
            Path(paths["OUT_PATH"]).write_text(json.dumps(data), encoding="utf-8")
            errors, _ = verify_snapshot.validate(paths["OUT_PATH"], now=FIXED_NOW, boj_policy_path=paths["BOJ_POLICY_PATH"])
        self.assertTrue(any("net incoherent" in error or "empreinte" in error for error in errors))

    def test_verifier_rejects_a_locally_redefined_risk_formula(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            data = json.loads(Path(paths["OUT_PATH"]).read_text(encoding="utf-8"))
            data["methodology"]["weights"] = {
                "legacy_crowding": 0.40,
                "yen_appreciation_4w": 0.40,
                "rate_compression": 0.20,
            }
            data["data_fingerprint"] = yct_quality.snapshot_fingerprint(data)
            Path(paths["OUT_PATH"]).write_text(json.dumps(data), encoding="utf-8")
            errors, _ = verify_snapshot.validate(paths["OUT_PATH"], now=FIXED_NOW, boj_policy_path=paths["BOJ_POLICY_PATH"])
        self.assertIn("contrat methodologique inattendu", errors)

    def test_verifier_rejects_spoofed_primary_source_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            data = json.loads(Path(paths["OUT_PATH"]).read_text(encoding="utf-8"))
            data["sources"]["cot"]["source_url"] = "https://example.test/cot.json"
            data["data_fingerprint"] = yct_quality.snapshot_fingerprint(data)
            Path(paths["OUT_PATH"]).write_text(json.dumps(data), encoding="utf-8")
            errors, _ = verify_snapshot.validate(paths["OUT_PATH"], now=FIXED_NOW, boj_policy_path=paths["BOJ_POLICY_PATH"])
        self.assertIn("URL primaire cot inattendue", errors)

    def test_frontend_exposes_distinct_series_and_correct_yen_sign(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("TFF leveraged funds", html)
        self.assertIn("gpe5-46if", html)
        self.assertIn("status.json", javascript)
        self.assertIn("market.json", javascript)
        self.assertIn("signed(-state.move4w,1)", javascript)
        self.assertIn("risk_formula_version", javascript)

    def test_english_frontend_is_complete_and_shares_the_runtime_contract(self):
        french = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        english = (ROOT / "web" / "en" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('<html lang="fr">', french)
        self.assertIn('<html lang="en">', english)
        self.assertIn('href="https://yct.l0g.fr/en/"', french)
        self.assertIn('href="https://yct.l0g.fr/"', english)
        self.assertIn('hreflang="fr"', english)
        self.assertIn('hreflang="en"', french)
        self.assertIn("Sources and methodology", english)
        self.assertIn("Carry calculator", english)
        self.assertIn("Analysis tool, not investment advice.", english)
        self.assertEqual(
            set(re.findall(r'\bid="([A-Za-z][A-Za-z0-9]*)"', french)),
            set(re.findall(r'\bid="([A-Za-z][A-Za-z0-9]*)"', english)),
        )
        self.assertIn('document.documentElement.lang === "en"', javascript)
        self.assertIn('fetch("/data.json"', javascript)
        self.assertIn('fetch("/status.json"', javascript)
        self.assertIn('fetch("/market.json"', javascript)

    def test_frontend_assets_are_cache_busted_by_their_content_hash(self):
        pages = (
            ROOT / "web" / "index.html",
            ROOT / "web" / "en" / "index.html",
        )
        for page in pages:
            html = page.read_text(encoding="utf-8")
            for name in ("app.css", "app.js"):
                digest = hashlib.sha256((ROOT / "web" / name).read_bytes()).hexdigest()[:12]
                match = re.search(r'%s\?v=([0-9a-f]{12})' % re.escape(name), html)
                self.assertIsNotNone(match, "%s sans version de contenu dans %s" % (name, page))
                self.assertEqual(match.group(1), digest)

    def test_deployment_maps_all_runtime_surfaces(self):
        apache = (ROOT / "deploy" / "yct.l0g.fr.conf").read_text(encoding="utf-8")
        service = (ROOT / "deploy" / "yct-snapshot.service").read_text(encoding="utf-8")
        activation = (ROOT / "deploy" / "activate-release.sh").read_text(encoding="utf-8")
        self.assertIn("Alias /status.json", apache)
        self.assertIn("Alias /market.json", apache)
        self.assertIn("source-calendars.json", service)
        self.assertIn("boj-policy.json", service)
        self.assertIn("--status-path", service)
        self.assertIn("source-calendars.json", activation)
        self.assertIn("boj-policy.json", activation)
        self.assertIn("yct-snapshot.timer", activation)
        self.assertIn("apache2ctl configtest", activation)
        self.assertIn("web/en/index.html", activation)
        self.assertIn("/var/www/html/yct/en", activation)
        self.assertIn("backup_optional /var/www/html/yct/en/index.html", activation)
        self.assertIn("restore_optional en-index.html", activation)

    def test_release_artifact_is_allowlisted_and_secret_scanned(self):
        release_source = (ROOT / "tools" / "release.py").read_text(encoding="utf-8")
        runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("SOURCE_FILES", release_source)
        self.assertIn("SECRET_PATTERNS", release_source)
        self.assertIn('"web/en/index.html"', release_source)
        self.assertNotIn(".env\"", release_source)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1 python3 -m unittest", runbook)
        self.assertGreaterEqual(runbook.count("verify-dir"), 2)

    def test_builder_has_no_deprecated_or_browser_side_source(self):
        source = (ROOT / "build_snapshot.py").read_text(encoding="utf-8")
        self.assertNotIn("frankfurter", source.lower())
        self.assertIn("cftc_contract_market_code", source)
        self.assertNotIn("MASSIVE_API_KEY", (ROOT / "web" / "app.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
