"""Replay stored canonical artifacts under the new policy in an isolated directory.

No strategy execution, runtime activation, network request or production DB write.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(source_dir: Path, out_dir: Path) -> dict:
    source_dir, out_dir = source_dir.resolve(), out_dir.resolve()
    if out_dir.exists() or source_dir == out_dir or source_dir in out_dir.parents:
        raise ValueError("out-dir must be a new directory outside the source artifact directory")
    names = ["run_metadata.json", "metrics_summary.json", "walk_forward_summary.json",
             "benchmark_comparison.json", "promotion_result.json"]
    hashes = {name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest() for name in names}
    out_dir.mkdir(parents=True)
    os.environ["QUANT_DB_PATH"] = str(out_dir / "isolated.db")
    from core.research_admission import RESEARCH_ADMISSION_POLICY, canonical_research_review
    from core.promotion_engine import load_metrics_from_artifact, promote
    from tools.evaluate_and_promote import recalculate_promotion_bundle_from_artifacts

    results = {}
    for case in ("missing_proof", "invalid_proof"):
        target = out_dir / case
        target.mkdir()
        for name in names:
            shutil.copy2(source_dir / name, target / name)
        metadata = json.loads((target / "run_metadata.json").read_text(encoding="utf-8"))
        metrics = json.loads((target / "metrics_summary.json").read_text(encoding="utf-8"))
        metadata["research_admission_policy"] = RESEARCH_ADMISSION_POLICY
        metadata["research_admission_proofs"] = (
            {name: {"proof_hash": "invalid", "artifact_type": "invalid"} for name in metrics}
            if case == "invalid_proof" else {}
        )
        (target / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        promotions, refreshed, _ = recalculate_promotion_bundle_from_artifacts(target, evidence_dir=out_dir / "no_paper_evidence")
        (target / "promotion_result.json").write_text(json.dumps(promotions, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "metrics_summary.json").write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
        loaded = load_metrics_from_artifact(str(target), evidence_dir=str(out_dir / "no_paper_evidence"))
        assert loaded and set(loaded) == set(promotions), "persisted artifact reload must cover all candidates"
        assert all(p["status"] == "research_only" and p["allowed_modes"] == ["backtest"] for p in promotions.values())
        assert all(promote(m).status == "research_only" for m in loaded.values())
        assert all("research admission required" in p["reason"] for p in promotions.values())
        results[case] = {"candidate_count": len(promotions), "all_research_only": True,
                         "paper_or_live_authorizations": 0, "artifact_reload_verified": True,
                         "reviews": {name: canonical_research_review(metadata, name) for name in promotions}}
    unchanged = all(hashlib.sha256((source_dir / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items())
    assert unchanged, "source artifacts changed during verification"
    result = {"source": str(source_dir), "source_sha256": hashes, "source_unchanged": unchanged, "cases": results}
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("reports/promotion"))
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.source_dir, args.out_dir)
    print(json.dumps({"source_unchanged": result["source_unchanged"],
                      "cases": {name: {key: value for key, value in case.items() if key != "reviews"}
                                for name, case in result["cases"].items()}}, ensure_ascii=False, indent=2))
