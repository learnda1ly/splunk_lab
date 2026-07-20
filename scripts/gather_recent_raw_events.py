#!/usr/bin/env python3
"""
Gather raw events from a Splunk indexer for specific indexes and recent buckets.

Designed to run on each peer in an indexer cluster. It discovers warm/cold
buckets for the given indexes, keeps only originating copies (db_*), skips
replicated copies (rb_*) and hot buckets, filters to buckets newer than N days,
and extracts raw events via `splunk cmd exporttool`.

Why not coldToFrozenScript?
---------------------------
Splunk's coldToFrozenExample.py (and coldToFrozenScript / coldToFrozenDir) run
when a bucket is about to leave the index (freeze/archive). That is the opposite
lifecycle stage from "buckets newer than 7 days". Those examples are still
useful as documentation of bucket layout (rawdata/journal.gz) and of the rule
that, in a cluster, each peer freezes its own copies — so archival scripts must
avoid double-archiving replicas. For recent searchable buckets, exporttool is
the right tool: it reads a complete hot/warm/cold bucket and emits original
raw events plus metadata. Frozen archives that only keep journal.gz cannot be
exported until thawed/rebuilt.

Tarball packaging / remote move is intentionally left for a later step.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

# Warm/cold originating: db_<newest>_<oldest>_<localid>[_<guid>]
# Warm/cold replicated:  rb_<newest>_<oldest>_<localid>_<guid>
BUCKET_NAME_RE = re.compile(
    r"^(?P<prefix>db|rb)_(?P<newest>\d+)_(?P<oldest>\d+)_(?P<rest>.+)$"
)


@dataclass(frozen=True)
class BucketInfo:
    path: Path
    index_name: str
    name: str
    prefix: str
    newest_epoch: int
    oldest_epoch: int
    state_dir: str  # "db" (hot/warm home) or "colddb"

    @property
    def is_originating(self) -> bool:
        return self.prefix == "db"

    @property
    def is_replicated(self) -> bool:
        return self.prefix == "rb"

    @property
    def is_hot(self) -> bool:
        return self.name.startswith("hot_")


def parse_bucket_name(name: str) -> Optional[tuple[str, int, int]]:
    """Return (prefix, newest_epoch, oldest_epoch) for warm/cold bucket dirs."""
    match = BUCKET_NAME_RE.match(name)
    if not match:
        return None
    return (
        match.group("prefix"),
        int(match.group("newest")),
        int(match.group("oldest")),
    )


def bucket_overlaps_window(
    newest_epoch: int,
    oldest_epoch: int,
    window_start: int,
    window_end: Optional[int] = None,
) -> bool:
    """True if the bucket's event time range overlaps [window_start, window_end]."""
    end = window_end if window_end is not None else int(time.time()) + 1
    if oldest_epoch > newest_epoch:
        return False
    return newest_epoch >= window_start and oldest_epoch <= end


def discover_index_roots(splunk_db: Path, indexes: Sequence[str]) -> List[Path]:
    roots = []
    for index_name in indexes:
        index_root = splunk_db / index_name
        if not index_root.is_dir():
            logging.warning("Index directory not found, skipping: %s", index_root)
            continue
        roots.append(index_root)
    return roots


def iter_bucket_dirs(index_root: Path) -> Iterable[tuple[str, Path]]:
    """Yield (state_dir_name, bucket_path) under homePath/coldPath style dirs."""
    for state_dir in ("db", "colddb"):
        parent = index_root / state_dir
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir()):
            if entry.is_dir():
                yield state_dir, entry


def collect_buckets(
    splunk_db: Path,
    indexes: Sequence[str],
    *,
    max_age_days: float,
    now_epoch: Optional[int] = None,
    include_replicated: bool = False,
    include_hot: bool = False,
) -> List[BucketInfo]:
    """
    Collect buckets for the given indexes that overlap the recent time window.

    "Newer than N days" is implemented as: bucket event-time range overlaps
    [now - N days, now]. That matches gathering recent events without requiring
    the entire bucket to sit inside the window.
    """
    now = int(now_epoch if now_epoch is not None else time.time())
    window_start = now - int(max_age_days * 86400)
    selected: List[BucketInfo] = []

    for index_root in discover_index_roots(splunk_db, indexes):
        index_name = index_root.name
        for state_dir, bucket_path in iter_bucket_dirs(index_root):
            name = bucket_path.name
            if name.startswith("hot_"):
                if not include_hot:
                    logging.debug("Skipping hot bucket: %s", bucket_path)
                    continue
                # Hot dirs do not encode newest/oldest in the directory name.
                # Treat them as fully inside the window when explicitly included.
                selected.append(
                    BucketInfo(
                        path=bucket_path,
                        index_name=index_name,
                        name=name,
                        prefix="hot",
                        newest_epoch=now,
                        oldest_epoch=window_start,
                        state_dir=state_dir,
                    )
                )
                continue

            parsed = parse_bucket_name(name)
            if parsed is None:
                logging.debug("Skipping unrecognized bucket dir: %s", bucket_path)
                continue

            prefix, newest, oldest = parsed
            if prefix == "rb" and not include_replicated:
                logging.debug("Skipping replicated bucket: %s", bucket_path)
                continue
            if "DISABLED" in name:
                logging.debug("Skipping disabled bucket: %s", bucket_path)
                continue
            if not bucket_overlaps_window(newest, oldest, window_start, now):
                continue

            # Prefer buckets that still look exportable (have rawdata).
            if not (bucket_path / "rawdata").is_dir():
                logging.warning(
                    "Bucket missing rawdata/, skipping (may be frozen leftover): %s",
                    bucket_path,
                )
                continue

            selected.append(
                BucketInfo(
                    path=bucket_path,
                    index_name=index_name,
                    name=name,
                    prefix=prefix,
                    newest_epoch=newest,
                    oldest_epoch=oldest,
                    state_dir=state_dir,
                )
            )

    selected.sort(key=lambda b: (b.index_name, b.newest_epoch, b.name), reverse=True)
    return selected


def build_exporttool_cmd(
    splunk_home: Path,
    bucket: BucketInfo,
    output_file: Path,
    *,
    earliest_epoch: Optional[int] = None,
    latest_epoch: Optional[int] = None,
) -> List[str]:
    cmd = [
        str(splunk_home / "bin" / "splunk"),
        "cmd",
        "exporttool",
        str(bucket.path),
        str(output_file),
        "-csv",
    ]
    if earliest_epoch is not None:
        cmd.extend(["-et", str(earliest_epoch)])
    if latest_epoch is not None:
        cmd.extend(["-lt", str(latest_epoch)])
    return cmd


def export_bucket(
    splunk_home: Path,
    bucket: BucketInfo,
    output_dir: Path,
    *,
    earliest_epoch: Optional[int] = None,
    latest_epoch: Optional[int] = None,
    dry_run: bool = False,
) -> Path:
    index_out = output_dir / bucket.index_name
    index_out.mkdir(parents=True, exist_ok=True)
    output_file = index_out / f"{bucket.name}.csv"
    cmd = build_exporttool_cmd(
        splunk_home,
        bucket,
        output_file,
        earliest_epoch=earliest_epoch,
        latest_epoch=latest_epoch,
    )
    logging.info("Export: %s", " ".join(cmd))
    if dry_run:
        return output_file

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"exporttool failed for {bucket.path} (rc={result.returncode}): {stderr}"
        )
    if not output_file.is_file():
        raise RuntimeError(f"exporttool reported success but no file at {output_file}")
    return output_file


def write_manifest(path: Path, buckets: Sequence[BucketInfo], exports: Sequence[Path]) -> None:
    bucket_rows = []
    for bucket in buckets:
        row = asdict(bucket)
        row["path"] = str(bucket.path)
        bucket_rows.append(row)
    payload = {
        "generated_at_epoch": int(time.time()),
        "bucket_count": len(buckets),
        "buckets": bucket_rows,
        "exports": [str(p) for p in exports],
        "notes": {
            "tarball": "Not implemented yet; outputs are per-bucket CSV from exporttool.",
            "cluster": (
                "Run on every peer. By default only originating db_* buckets are "
                "exported so replicated rb_* copies are not duplicated."
            ),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def resolve_splunk_paths(splunk_home: Path) -> tuple[Path, Path]:
    home = splunk_home.expanduser().resolve()
    splunk_db = Path(os.environ.get("SPLUNK_DB", home / "var" / "lib" / "splunk"))
    if not splunk_db.is_absolute():
        splunk_db = (home / splunk_db).resolve()
    else:
        splunk_db = splunk_db.resolve()
    return home, splunk_db


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Gather raw events from recent Splunk indexer buckets for specific "
            "indexes (indexer-cluster aware)."
        )
    )
    parser.add_argument(
        "--indexes",
        required=True,
        help="Comma-separated index names to gather (e.g. main,security).",
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=7.0,
        help="Include buckets that overlap the last N days (default: 7).",
    )
    parser.add_argument(
        "--splunk-home",
        default=os.environ.get("SPLUNK_HOME", "/opt/splunk"),
        help="SPLUNK_HOME on this peer (default: env SPLUNK_HOME or /opt/splunk).",
    )
    parser.add_argument(
        "--output-dir",
        default="./raw_event_exports",
        help="Directory for per-bucket CSV exports and manifest (default: ./raw_event_exports).",
    )
    parser.add_argument(
        "--include-replicated",
        action="store_true",
        help="Also export rb_* replicated copies (usually wrong on a cluster).",
    )
    parser.add_argument(
        "--include-hot",
        action="store_true",
        help="Include hot_* buckets (risky/unsupported while actively written).",
    )
    parser.add_argument(
        "--filter-events-to-window",
        action="store_true",
        help=(
            "Pass -et/-lt to exporttool so only events inside the max-age window "
            "are written (bucket may still be selected by overlap)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching buckets and planned exporttool commands; do not export.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    indexes = [i.strip() for i in args.indexes.split(",") if i.strip()]
    if not indexes:
        logging.error("No indexes provided.")
        return 2

    splunk_home, splunk_db = resolve_splunk_paths(Path(args.splunk_home))
    output_dir = Path(args.output_dir).expanduser().resolve()
    now = int(time.time())
    window_start = now - int(args.max_age_days * 86400)

    logging.info("SPLUNK_HOME=%s", splunk_home)
    logging.info("SPLUNK_DB=%s", splunk_db)
    logging.info(
        "Selecting buckets overlapping last %.4g days (from epoch %s)",
        args.max_age_days,
        window_start,
    )

    if not splunk_db.is_dir():
        logging.error("SPLUNK_DB does not exist: %s", splunk_db)
        return 1

    buckets = collect_buckets(
        splunk_db,
        indexes,
        max_age_days=args.max_age_days,
        now_epoch=now,
        include_replicated=args.include_replicated,
        include_hot=args.include_hot,
    )
    logging.info("Matched %d bucket(s)", len(buckets))
    for bucket in buckets:
        logging.info(
            "  %s [%s] newest=%s oldest=%s",
            bucket.path,
            bucket.state_dir,
            bucket.newest_epoch,
            bucket.oldest_epoch,
        )

    earliest = window_start if args.filter_events_to_window else None
    latest = now if args.filter_events_to_window else None
    exports: List[Path] = []
    failures = 0

    for bucket in buckets:
        try:
            exports.append(
                export_bucket(
                    splunk_home,
                    bucket,
                    output_dir,
                    earliest_epoch=earliest,
                    latest_epoch=latest,
                    dry_run=args.dry_run,
                )
            )
        except Exception as exc:  # noqa: BLE001 - continue other buckets
            failures += 1
            logging.error("%s", exc)

    manifest_path = output_dir / "manifest.json"
    if not args.dry_run:
        write_manifest(manifest_path, buckets, exports)
        logging.info("Wrote manifest %s", manifest_path)
    else:
        logging.info("Dry-run complete; no files written.")

    # Placeholder for future packaging step (tarball / transfer).
    logging.info(
        "Packaging/move step not implemented yet "
        "(next: tar CSV/raw outputs for off-box transfer)."
    )

    if failures:
        logging.error("Finished with %d failure(s)", failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
