#!/usr/bin/env python3
"""Unit tests for gather_recent_raw_events (no live Splunk required)."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gather_recent_raw_events.py"


def load_module():
    spec = importlib.util.spec_from_file_location("gather_recent_raw_events", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_module()


class ParseBucketNameTests(unittest.TestCase):
    def test_cluster_originating(self):
        parsed = mod.parse_bucket_name(
            "db_1700003600_1700000000_12_ABCD-EFGH"
        )
        self.assertEqual(parsed, ("db", 1700003600, 1700000000))

    def test_cluster_replicated(self):
        parsed = mod.parse_bucket_name("rb_1700003600_1700000000_12_ABCD-EFGH")
        self.assertEqual(parsed, ("rb", 1700003600, 1700000000))

    def test_non_clustered(self):
        parsed = mod.parse_bucket_name("db_1700003600_1700000000_99")
        self.assertEqual(parsed, ("db", 1700003600, 1700000000))

    def test_hot_not_parsed(self):
        self.assertIsNone(mod.parse_bucket_name("hot_v1_1234"))


class WindowOverlapTests(unittest.TestCase):
    def test_overlap_partial(self):
        # Bucket spans before and into the window.
        self.assertTrue(mod.bucket_overlaps_window(100, 50, window_start=80, window_end=200))

    def test_fully_before_window(self):
        self.assertFalse(mod.bucket_overlaps_window(70, 50, window_start=80, window_end=200))

    def test_fully_after_window(self):
        self.assertFalse(mod.bucket_overlaps_window(300, 250, window_start=80, window_end=200))


class CollectBucketsTests(unittest.TestCase):
    def _make_bucket(self, root: Path, index: str, state: str, name: str) -> Path:
        bucket = root / index / state / name
        (bucket / "rawdata").mkdir(parents=True)
        (bucket / "rawdata" / "journal.gz").write_bytes(b"fake")
        return bucket

    def test_selects_recent_originating_skips_replica_and_old(self):
        now = 1_700_050_000
        week = 7 * 86400
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)
            recent_db = self._make_bucket(
                db,
                "main",
                "db",
                f"db_{now}_{now - 3600}_1_GUIDA",
            )
            self._make_bucket(
                db,
                "main",
                "db",
                f"rb_{now}_{now - 3600}_1_GUIDA",
            )
            self._make_bucket(
                db,
                "main",
                "colddb",
                f"db_{now - week - 1000}_{now - week - 5000}_2_GUIDA",
            )
            (db / "main" / "db" / "hot_v1_9").mkdir(parents=True)

            buckets = mod.collect_buckets(
                db,
                ["main"],
                max_age_days=7,
                now_epoch=now,
            )
            self.assertEqual([b.path for b in buckets], [recent_db])
            self.assertTrue(buckets[0].is_originating)

    def test_include_replicated(self):
        now = 1_700_050_000
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)
            self._make_bucket(db, "sec", "db", f"db_{now}_{now - 10}_1_G")
            rb = self._make_bucket(db, "sec", "db", f"rb_{now}_{now - 10}_1_G")
            buckets = mod.collect_buckets(
                db,
                ["sec"],
                max_age_days=7,
                now_epoch=now,
                include_replicated=True,
            )
            paths = {b.path for b in buckets}
            self.assertIn(rb, paths)


class ExportCmdTests(unittest.TestCase):
    def test_build_exporttool_cmd(self):
        bucket = mod.BucketInfo(
            path=Path("/opt/splunk/var/lib/splunk/main/db/db_1_0_1"),
            index_name="main",
            name="db_1_0_1",
            prefix="db",
            newest_epoch=1,
            oldest_epoch=0,
            state_dir="db",
        )
        cmd = mod.build_exporttool_cmd(
            Path("/opt/splunk"),
            bucket,
            Path("/tmp/out.csv"),
            earliest_epoch=100,
            latest_epoch=200,
        )
        self.assertEqual(
            cmd,
            [
                "/opt/splunk/bin/splunk",
                "cmd",
                "exporttool",
                "/opt/splunk/var/lib/splunk/main/db/db_1_0_1",
                "/tmp/out.csv",
                "-csv",
                "-et",
                "100",
                "-lt",
                "200",
            ],
        )


if __name__ == "__main__":
    unittest.main()
