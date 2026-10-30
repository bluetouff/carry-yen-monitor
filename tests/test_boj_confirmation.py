"""Unchanged targets may be confirmed; new rates always require review."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest import mock

from test_yct import BOJ_POLICY, FIXED_NOW, run_paths, run_successfully
import build_snapshot
import verify_snapshot
import yct_quality


def confirmation():
    return {
        "mode": "official-guideline-confirmed", "rate": BOJ_POLICY["rate"],
        "data_as_of": "2026-08-01",
        "source_url": "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260801a.pdf",
        "source_sha256": "a" * 64,
        "guideline_url": "https://www.boj.or.jp/en/index.htm",
    }


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_never_creates_an_effective_date_or_a_new_rate(self):
        resolved = yct_quality.confirmed_boj_policy(BOJ_POLICY, confirmation(), FIXED_NOW)
        self.assertEqual(resolved["rate"], BOJ_POLICY["rate"])
        self.assertEqual(resolved["data_as_of"], "2026-08-01")
        self.assertNotIn("effective_from", resolved)

    def test_invalid_or_changed_confirmations_are_rejected(self):
        for changes in (
            {"rate": 1.25}, {"rate": True}, {"rate": "1.0"}, {"rate": float("nan")},
            {"data_as_of": "2026-07-31"}, {"data_as_of": "2026-08-03"},
            {"source_url": "https://www.boj.or.jp.evil.test/k260801a.pdf"},
            {"source_url": "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260801b.pdf"},
            {"source_sha256": "x" * 64}, {"guideline_url": "https://evil.test"},
            {"mode": "reviewed-policy"}, {"extra": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(yct_quality.QualityError):
                yct_quality.confirmed_boj_policy(BOJ_POLICY, dict(confirmation(), **changes), FIXED_NOW)

    def test_pending_reviewed_hike_cannot_be_confirmed_early(self):
        baseline = dict(BOJ_POLICY, schema_version=2, previous_rate=0.75, effective_from="2026-08-03")
        with self.assertRaises(yct_quality.QualityError):
            yct_quality.confirmed_boj_policy(baseline, confirmation(), FIXED_NOW)

    def test_transaction_and_validator_agree_on_confirmed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths, boj_confirmation=confirmation()), 0)
            errors, data = verify_snapshot.validate(paths["OUT_PATH"], now=FIXED_NOW,
                                                   boj_policy_path=paths["BOJ_POLICY_PATH"])
            self.assertEqual(errors, [])
            self.assertEqual(data["rates"]["boj_source"], "official-guideline-confirmed")
            self.assertEqual(data["sources"]["boj"]["reviewed_policy_as_of"], "2026-07-31")
            self.assertEqual(data["sources"]["boj"]["data_as_of"], "2026-08-01")
            original = Path(paths["OUT_PATH"]).read_bytes()
            self.assertEqual(run_successfully(paths, boj_confirmation=confirmation()), 0)
            self.assertEqual(Path(paths["OUT_PATH"]).read_bytes(), original)
            for mutate in ("rate", "mode", "missing"):
                forged = copy.deepcopy(data)
                if mutate == "rate":
                    forged["sources"]["boj"]["policy_confirmation"]["rate"] = 1.25
                elif mutate == "mode":
                    forged["sources"]["boj"]["verification_mode"] = "reviewed-policy"
                else:
                    del forged["sources"]["boj"]["policy_confirmation"]
                forged["data_fingerprint"] = yct_quality.snapshot_fingerprint(forged)
                Path(paths["OUT_PATH"]).write_text(json.dumps(forged))
                errors, _ = verify_snapshot.validate(paths["OUT_PATH"], now=FIXED_NOW,
                                                    boj_policy_path=paths["BOJ_POLICY_PATH"])
                self.assertTrue(errors, mutate)

    def test_changed_confirmation_preserves_last_good_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = run_paths(directory)
            self.assertEqual(run_successfully(paths), 0)
            original = Path(paths["OUT_PATH"]).read_bytes()
            self.assertEqual(run_successfully(paths, boj_confirmation=dict(confirmation(), rate=1.25)), 1)
            self.assertEqual(Path(paths["OUT_PATH"]).read_bytes(), original)
            status = json.loads(Path(paths["STATUS_PATH"]).read_text())
            self.assertEqual(status["sources"]["boj"]["error_code"], "boj-confirmation")


class CollectorTests(unittest.TestCase):
    PDF = b"%PDF-1.7 official statement test fixture"
    INDEX = """<a href='/en/mopo/mpmdeci/mpr_2026/k260731a.pdf'>Statement on Monetary Policy</a>
    <a href='/en/mopo/mpmdeci/mpr_2026/k260801a.pdf'>Statement on Monetary Policy [PDF]</a>"""
    HOME = """<li><time datetime='2026-08-01'>Aug. 1</time>
    <a href='/en/mopo/mpmdeci/mpr_2026/k260801a.pdf'>Statement on Monetary Policy</a></li>
    <section class='policy'><h3>Guideline</h3><p>
    <a href='/en/mopo/mpmdeci/state_2026/index.htm'>The Bank will encourage the
    uncollateralized overnight call rate to remain at around 1.0 percent.</a></p></section>"""

    def collect(self, index=None, home=None, now=FIXED_NOW, policy=None):
        with mock.patch.object(build_snapshot, "http_get_text", side_effect=[
            self.INDEX if index is None else index, self.HOME if home is None else home,
        ]), mock.patch.object(build_snapshot, "http_get", return_value=self.PDF):
            return build_snapshot.fetch_boj_index(policy=policy or BOJ_POLICY, now=now)

    def test_official_guideline_confirms_only_unchanged_target(self):
        result = self.collect()
        self.assertEqual(result["confirmation"]["mode"], "official-guideline-confirmed")
        self.assertEqual(result["confirmation"]["rate"], 1.0)
        resolved = yct_quality.confirmed_boj_policy(BOJ_POLICY, result["confirmation"], FIXED_NOW)
        self.assertEqual(resolved["data_as_of"], "2026-08-01")

    def test_changed_ambiguous_missing_or_stale_guideline_fails_closed(self):
        cases = (
            self.HOME.replace("1.0 percent", "1.25 percent"),
            self.HOME + self.HOME,
            self.HOME.replace("class='policy'", "class='other'"),
            self.HOME.replace("datetime='2026-08-01'", "datetime='2026-07-31'"),
            self.HOME.replace("k260801a.pdf", "k260731a.pdf"),
            self.HOME.replace("/en/mopo/mpmdeci/state_2026/index.htm", "https://evil.test/"),
            self.HOME.replace("The Bank will encourage", "The Bank would encourage"),
        )
        for home in cases:
            with self.subTest(home=home), self.assertRaises(yct_quality.QualityError):
                self.collect(home=home)

    def test_future_or_foreign_statement_and_pending_hike_fail_closed(self):
        cases = (
            {"now": datetime(2026, 7, 31, 3, tzinfo=timezone.utc)},
            {"index": self.INDEX.replace("/en/mopo/mpmdeci/mpr_2026/k260801a.pdf", "https://evil.test/k260801a.pdf")},
            {"policy": dict(BOJ_POLICY, schema_version=2, previous_rate=0.75, effective_from="2026-08-03")},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(yct_quality.QualityError):
                self.collect(**changes)


if __name__ == "__main__":
    unittest.main()
