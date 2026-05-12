#!/usr/bin/env python3
"""
Test Artifact Quarantine

reports/ 아래의 test/demo artifact를 식별하고 quarantine 디렉토리로 이동.
production operator view가 test artifact에 오염되지 않게 한다.

Usage:
    python tools/quarantine_test_artifacts.py --reports-dir reports/
    python tools/quarantine_test_artifacts.py --reports-dir reports/ --dry-run
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

REPORT_SUBDIRS = ("paper_evidence", "paper_runtime", "promotion")
QUARANTINE_DIR_NAME = "_quarantine"


# test/demo strategy 패턴
_TEST_PATTERNS = [
    "dedup_test", "test_", "demo_", "golden_test", "mock_",
    "xv_test", "bench_test", "insuf_test", "dist_test", "suf_test",
    "dup_s", "art_test", "smoke_s", "rescan_s", "consist_s",
    "block_test", "vis_test", "guard_test", "rb_test", "rb_filt",
    "gate_s", "pass_s", "nopos_s", "openpos_s", "notif_s",
    "fresh_s", "stale_s", "empty_s", "db_s", "frozen_s",
    "exit_test", "cleanup_s", "frozen_exit", "no_pos",
    "bootstrap_test", "dryrun_s", "legacy_s", "all_v1", "mix_s",
    "obs_s", "normal_s", "normal_r", "s1", "s2",
    "disabled_s",
]

_STRATEGY_PREFIXES = (
    "daily_evidence_", "runtime_status_", "preflight_status_",
    "runtime_audit_", "runtime_rebuild_", "evidence_quality_",
    "promotion_evidence_", "approval_checklist_", "weekly_summary_",
    "pilot_session_", "promotion_blocker_",
)

_PROMOTION_METADATA_KEYS = {
    "artifact_type", "schema_version", "generated_at", "summary",
    "status_counts", "live_ready_count", "blocked_from_live_count",
    "data_snapshot_hash", "data_snapshot_manifest", "evaluation_errors",
    "walk_forward_errors", "strategy_specs", "canonical_research_candidate_ids",
    "universe", "universe_rule", "eval_start", "eval_end",
    "initial_capital", "commit_hash", "config_yaml_hash",
    "config_resolved_hash", "wf_window_months", "wf_step_months",
    "wf_n_windows", "provider", "fetch_errors", "liquidity_coverage",
    "benchmark_coverage", "benchmark_meta",
}


def _matches_test_pattern(text: str) -> bool:
    lower = text.lower()
    for pat in _TEST_PATTERNS:
        pat = pat.lower()
        if lower == pat:
            return True
        if pat.endswith("_") and lower.startswith(pat):
            return True
        if lower.startswith(f"{pat}_") or lower.endswith(f"_{pat}") or f"_{pat}_" in lower:
            return True
    return False


def _strip_suffixes(strategy: str) -> str:
    out = strategy
    for ext in (".jsonl", ".json", ".md", ".txt"):
        if out.endswith(ext):
            out = out[:-len(ext)]
    if len(out) > 9 and out[-9] == "_" and out[-8:].isdigit():
        out = out[:-9]
    return out


def extract_strategy_name(name: str) -> str | None:
    """strategy-scoped artifact 파일명에서 전략명을 추출한다."""
    for prefix in _STRATEGY_PREFIXES:
        if name.startswith(prefix):
            return _strip_suffixes(name[len(prefix):])
    return None


def _is_paper_eligible_strategy(strategy: str) -> bool:
    try:
        from core.strategy_universe import is_paper_eligible
        return bool(is_paper_eligible(strategy))
    except Exception:
        return True


def is_test_artifact(name: str) -> bool:
    """파일명이나 전략명이 test/demo 패턴인지."""
    lower = name.lower()
    if _matches_test_pattern(lower):
        return True

    strategy = extract_strategy_name(name)
    if strategy and (_matches_test_pattern(strategy) or not _is_paper_eligible_strategy(strategy)):
        return True

    return False


def _payload_contains_test_artifact(payload) -> bool:
    """promotion JSON 내부의 test/demo 전략명을 감지한다."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key)
            if key_text not in _PROMOTION_METADATA_KEYS and is_test_artifact(key_text):
                return True
            if key in {"strategy", "strategy_name", "candidate_id"} and isinstance(value, str):
                if is_test_artifact(value):
                    return True
            if isinstance(value, (dict, list)) and _payload_contains_test_artifact(value):
                return True
    elif isinstance(payload, list):
        return any(_payload_contains_test_artifact(item) for item in payload)
    return False


def is_test_artifact_file(path: Path) -> bool:
    """파일명과 promotion JSON payload를 함께 확인한다."""
    if is_test_artifact(path.name) or is_test_artifact(path.stem):
        return True
    if path.suffix.lower() == ".json" and path.parent.name == "promotion":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return _payload_contains_test_artifact(payload)
    return False


def scan_test_artifacts(reports_dir: Path) -> list[Path]:
    """운영 판단에 쓰이는 reports 하위 test artifact 파일만 대상."""
    results = []
    target_dirs = [reports_dir / subdir for subdir in REPORT_SUBDIRS]
    for target in target_dirs:
        if not target.exists():
            continue
        for f in target.rglob("*"):
            if QUARANTINE_DIR_NAME in f.parts:
                continue
            if f.is_file() and is_test_artifact_file(f):
                results.append(f)
    return sorted(results)


def _ensure_under(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise ValueError(f"{path} is outside {root}")
    return resolved_path


def quarantine(reports_dir: Path, dry_run: bool = False) -> list[tuple[Path, Path]]:
    """test artifact를 quarantine 디렉토리로 이동."""
    reports_dir = reports_dir.resolve()
    quarantine_dir = reports_dir / QUARANTINE_DIR_NAME
    artifacts = scan_test_artifacts(reports_dir)

    moved = []
    for f in artifacts:
        _ensure_under(f, reports_dir)
        rel = f.relative_to(reports_dir)
        dest = quarantine_dir / rel
        if dry_run:
            print(f"  [DRY-RUN] {rel}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))
            print(f"  [MOVED] {rel} → _quarantine/{rel}")
        moved.append((f, dest))

    return moved


def main():
    parser = argparse.ArgumentParser(description="Quarantine Test Artifacts")
    parser.add_argument("--reports-dir", default="reports/")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    if not reports_dir.exists():
        print("ERROR: %s not found" % reports_dir)
        sys.exit(1)

    artifacts = scan_test_artifacts(reports_dir)
    if not artifacts:
        print("No test artifacts found in %s" % reports_dir)
        return

    print("\n=== Test Artifact Quarantine ===")
    print("  Reports dir: %s" % reports_dir)
    print("  Found: %d test artifacts" % len(artifacts))

    moved = quarantine(reports_dir, args.dry_run)

    if args.dry_run:
        print("\n  Dry run: no files moved. Remove --dry-run to execute.")
    else:
        print("\n  Quarantined %d files to _quarantine/" % len(moved))


if __name__ == "__main__":
    main()
