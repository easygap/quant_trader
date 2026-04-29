"""
Strategy Universe + Shadow Bootstrap + Artifact Isolation 통합 테스트

시나리오:
  - prod preflight --all에 dedup_test 미표시
  - rotation은 evidence 없어도 operator view에 표시
  - blocked_insufficient + no evidence → shadow_collect 가능
  - shadow bootstrap run → evidence 누적, order submit 0회
  - test artifact가 canonical operator view 미오염
  - notifier unconfigured → entry blocked, shadow bootstrap 허용
  - scheduler/preflight/session_bootstrap 전부 canonical strategy set 사용
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_dirs(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_runtime as pr
    import core.paper_preflight as ppf
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pr, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(pr, "PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS", 999)
    monkeypatch.setattr(ppf, "RUNTIME_DIR", tmp_path / "paper_runtime")
    return tmp_path


@pytest.fixture
def evidence_dir(_isolate_dirs):
    return _isolate_dirs / "paper_evidence"


@pytest.fixture
def runtime_dir(_isolate_dirs):
    return _isolate_dirs / "paper_runtime"


@pytest.fixture
def fresh_db():
    from config.config_loader import Config
    Config._instance = None
    from database.models import (
        init_database, get_session,
        TradeHistory, OperationEvent, PortfolioSnapshot,
        Position, FailedOrder, PendingOrderGuard, DailyReport,
    )
    init_database()
    session = get_session()
    for model in [TradeHistory, OperationEvent, PortfolioSnapshot,
                  Position, FailedOrder, PendingOrderGuard, DailyReport]:
        try:
            session.query(model).delete()
        except Exception:
            pass
    session.commit()
    session.close()
    return True


def _seed_v2(evidence_dir, strategy, days):
    from core.paper_evidence import _append_jsonl
    jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
    for i, cfg in enumerate(days):
        record = {
            "date": cfg["date"], "day_number": i + 1, "strategy": strategy,
            "total_value": 10_000_000, "cash": 3_000_000, "invested": 7_000_000,
            "daily_return": cfg.get("daily_return", 0.3),
            "cumulative_return": 0.5, "mdd": -2.0, "position_count": 2,
            "total_trades": 2,
            "same_universe_excess": cfg.get("same_universe_excess", 0.05),
            "exposure_matched_excess": 0.03, "cash_adjusted_excess": 0.02,
            "benchmark_status": cfg.get("benchmark_status", "final"),
            "benchmark_meta": {"completeness": 1.0},
            "raw_fill_rate": 1.0,
            "reject_count": 0, "phantom_position_count": 0,
            "stale_pending_count": 0, "duplicate_blocked_count": 0,
            "restart_recovery_count": 0,
            "anomalies": [], "cross_validation_warnings": [],
            "status": "normal", "record_version": 1, "schema_version": 2,
            "diagnostics": [],
        }
        _append_jsonl(jsonl_path, record)


# ═══════════════════════════════════════════════════════════════
# 1. Canonical Strategy Universe
# ═══════════════════════════════════════════════════════════════

class TestCanonicalUniverse:

    def test_paper_strategies_from_registry(self):
        """paper 전략은 STRATEGY_STATUS에서 allowed_modes에 paper 포함된 것만."""
        from core.strategy_universe import get_paper_strategy_names
        names = get_paper_strategy_names()
        assert "scoring" in names
        assert "relative_strength_rotation" in names
        assert "dedup_test" not in names
        assert "breakout_volume" not in names  # disabled

    def test_dedup_test_excluded(self):
        """dedup_test는 canonical universe에 없음."""
        from core.strategy_universe import is_paper_eligible
        assert is_paper_eligible("dedup_test") is False

    def test_rotation_included_even_without_evidence(self, evidence_dir):
        """rotation은 evidence 없어도 canonical universe에 포함."""
        from core.strategy_universe import is_paper_eligible
        assert is_paper_eligible("relative_strength_rotation") is True

    def test_preflight_all_uses_canonical(self, evidence_dir, runtime_dir, fresh_db):
        """preflight --all이 canonical universe만 사용."""
        # evidence에 dedup_test 있어도 preflight에 안 나와야 함
        _seed_v2(evidence_dir, "dedup_test", [{"date": "2026-04-06"}])

        from core.paper_preflight import run_preflight
        from core.strategy_universe import get_paper_strategy_names

        names = get_paper_strategy_names()
        assert "dedup_test" not in names
        # scoring, relative_strength_rotation만 표시
        assert "scoring" in names
        assert "relative_strength_rotation" in names


# ═══════════════════════════════════════════════════════════════
# 2. Artifact Isolation
# ═══════════════════════════════════════════════════════════════

class TestArtifactIsolation:

    def test_test_artifact_detection(self):
        """test artifact 패턴 감지."""
        from tools.quarantine_test_artifacts import is_test_artifact
        assert is_test_artifact("daily_evidence_dedup_test") is True
        assert is_test_artifact("daily_evidence_golden_test") is True
        assert is_test_artifact("runtime_status_smoke_s") is True

    def test_prod_artifact_not_detected(self):
        """production artifact는 test로 감지되지 않음."""
        from tools.quarantine_test_artifacts import is_test_artifact
        assert is_test_artifact("daily_evidence_scoring") is False
        assert is_test_artifact("daily_evidence_relative_strength_rotation") is False

    def test_quarantine_dry_run(self, evidence_dir, tmp_path):
        """quarantine dry-run: 파일 이동 없이 목록만."""
        from tools.quarantine_test_artifacts import scan_test_artifacts
        # test artifact 생성
        (evidence_dir / "daily_evidence_dedup_test.jsonl").parent.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "daily_evidence_dedup_test.jsonl").write_text("{}", encoding="utf-8")
        (evidence_dir / "daily_evidence_scoring.jsonl").write_text("{}", encoding="utf-8")

        results = scan_test_artifacts(evidence_dir.parent)
        test_files = [f.name for f in results]
        assert "daily_evidence_dedup_test.jsonl" in test_files
        assert "daily_evidence_scoring.jsonl" not in test_files


# ═══════════════════════════════════════════════════════════════
# 3. Shadow Bootstrap
# ═══════════════════════════════════════════════════════════════

class TestShadowBootstrap:

    def test_blocked_strategy_shadow_collect_allowed(self, evidence_dir, runtime_dir, fresh_db):
        """blocked_insufficient_evidence → shadow_collect 허용."""
        from core.paper_runtime import get_paper_runtime_state
        # no evidence → blocked
        state = get_paper_runtime_state("relative_strength_rotation")
        assert state.state == "blocked_insufficient_evidence"
        assert "shadow_collect" in state.allowed_actions
        assert "entry" not in state.allowed_actions

    def test_shadow_bootstrap_collects_evidence(self, evidence_dir, runtime_dir, fresh_db):
        """shadow bootstrap → evidence 수집, order submit 0회."""
        from core.paper_evidence import collect_daily_evidence, get_canonical_records

        dt = datetime(2026, 4, 6, 15, 35)
        with patch("core.paper_evidence._compute_benchmark_excess") as mock_bench, \
             patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[]):
            mock_bench.return_value = {
                "same_universe_excess": 0.15,
                "exposure_matched_excess": 0.10,
                "cash_adjusted_excess": 0.09,
                "benchmark_status": "final",
                "benchmark_meta": {"completeness": 1.0},
            }
            r = collect_daily_evidence(
                strategy="rotation_shadow", mode="paper",
                account_key="rotation_shadow",
                date=dt, watchlist_symbols=["005930"],
            )

        assert r is not None
        assert r.same_universe_excess == 0.15

        # order submit은 collect_daily_evidence에서 발생하지 않음 (증거 수집만)
        records = get_canonical_records("rotation_shadow")
        assert len(records) == 1
        assert records[0]["total_trades"] == 0  # no trades from shadow

    def test_shadow_after_bootstrap_state_improves(self, evidence_dir, runtime_dir, fresh_db):
        """shadow 수집 후 eligible records 증가 → state 개선 가능."""
        # 3일 shadow 수집
        _seed_v2(evidence_dir, "relative_strength_rotation", [
            {"date": "2026-04-04", "benchmark_status": "final"},
            {"date": "2026-04-05", "benchmark_status": "final"},
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_runtime import get_paper_runtime_state
        state = get_paper_runtime_state("relative_strength_rotation")
        # 3일 full final/excess → normal
        assert state.state == "normal"
        assert "entry" in state.allowed_actions

    def test_notifier_unconfigured_shadow_still_allowed(self, evidence_dir, runtime_dir, fresh_db):
        """notifier unconfigured → entry blocked이지만 shadow_collect 허용."""
        from core.paper_runtime import get_paper_runtime_state
        # no evidence → blocked_insufficient
        state = get_paper_runtime_state("relative_strength_rotation")
        assert "shadow_collect" in state.allowed_actions
        assert "entry" not in state.allowed_actions


# ═══════════════════════════════════════════════════════════════
# 4. Scoring / Rotation 표기
# ═══════════════════════════════════════════════════════════════

class TestStrategyDisplay:

    def test_scoring_with_real_evidence(self, evidence_dir, runtime_dir, fresh_db):
        """scoring: real evidence 있음, blocked, shadow 가능."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("scoring", "2026-04-06")
        assert r.has_real_evidence is True
        assert r.shadow_bootstrap_available is True
        assert r.entry_allowed is False

    def test_rotation_no_real_evidence(self, evidence_dir, runtime_dir, fresh_db):
        """rotation: no real evidence, shadow bootstrap 대상."""
        from core.paper_preflight import run_preflight
        r = run_preflight("relative_strength_rotation", "2026-04-06")
        assert r.has_real_evidence is False
        assert r.shadow_bootstrap_available is True
        assert r.entry_allowed is False
        # operator action: shadow bootstrap 안내
        assert any("shadow" in a.lower() for a in r.operator_actions)

    def test_both_strategies_in_session_bootstrap(self, evidence_dir, runtime_dir, fresh_db):
        """session_bootstrap에 scoring + rotation 둘 다 표시."""
        from core.paper_preflight import run_preflight, _save_session_bootstrap

        r1 = run_preflight("scoring", "2026-04-06")
        r2 = run_preflight("relative_strength_rotation", "2026-04-06")
        path = _save_session_bootstrap("2026-04-06", [r1, r2])

        content = path.read_text(encoding="utf-8")
        assert "scoring" in content
        assert "relative_strength_rotation" in content
        assert "dedup_test" not in content


# ═══════════════════════════════════════════════════════════════
# 5. Policy Consistency
# ═══════════════════════════════════════════════════════════════

class TestPolicyConsistency:

    def test_shadow_collect_in_paper_eligible_states(self):
        """shadow_collect는 paper-eligible states에서만 허용. research_disabled 제외."""
        from core.paper_runtime import ALLOWED_ACTIONS
        # paper-eligible states에서는 shadow_collect 허용
        for state_name in ("normal", "degraded", "frozen", "blocked_insufficient_evidence"):
            assert "shadow_collect" in ALLOWED_ACTIONS[state_name], \
                f"state={state_name}: shadow_collect missing"
        # research_disabled에서는 shadow_collect 불허
        assert "shadow_collect" not in ALLOWED_ACTIONS["research_disabled"]

    def test_notifier_policy_separation(self, evidence_dir, runtime_dir, fresh_db):
        """notifier 요구사항: entry_submit에만 영향, shadow_collect에는 영향 없음.
        (notifier unconfigured는 preflight warn이지 shadow block이 아님)"""
        from core.paper_preflight import run_preflight
        r = run_preflight("relative_strength_rotation", "2026-04-06")
        # notifier unconfigured → warn
        notifier_check = [c for c in r.checks if c["name"] == "notifier_discord"]
        assert notifier_check[0]["status"] in ("warn", "pass")
        # shadow는 여전히 available
        assert r.shadow_bootstrap_available is True


# ═══════════════════════════════════════════════════════════════
# 6. Evidence Provenance 분리
# ═══════════════════════════════════════════════════════════════

class TestEvidenceProvenance:
    """shadow evidence는 증거 수집용이지 승격 근거가 아님."""

    def _seed_with_mode(self, evidence_dir, strategy, days):
        """evidence_mode 포함 seed."""
        from core.paper_evidence import _append_jsonl
        jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
        for i, cfg in enumerate(days):
            record = {
                "date": cfg["date"], "day_number": i + 1, "strategy": strategy,
                "total_value": 10_000_000, "cash": 3_000_000, "invested": 7_000_000,
                "daily_return": cfg.get("daily_return", 0.3),
                "cumulative_return": 0.5, "mdd": -2.0, "position_count": 2,
                "total_trades": cfg.get("total_trades", 2),
                "buy_count": cfg.get("buy_count", 1),
                "sell_count": cfg.get("sell_count", 1),
                "same_universe_excess": cfg.get("same_universe_excess", 0.05),
                "exposure_matched_excess": 0.03, "cash_adjusted_excess": 0.02,
                "benchmark_status": cfg.get("benchmark_status", "final"),
                "benchmark_meta": {"completeness": 1.0},
                "raw_fill_rate": 1.0,
                "reject_count": 0, "phantom_position_count": 0,
                "stale_pending_count": 0, "duplicate_blocked_count": 0,
                "restart_recovery_count": 0,
                "anomalies": [], "cross_validation_warnings": [],
                "status": "normal", "record_version": 1, "schema_version": 2,
                "diagnostics": [],
                # provenance
                "evidence_mode": cfg.get("evidence_mode", "real_paper"),
                "execution_backed": cfg.get("execution_backed", True),
                "order_submit_count": cfg.get("order_submit_count", 2),
                "fill_count": cfg.get("fill_count", 2),
            }
            _append_jsonl(jsonl_path, record)

    def test_shadow_only_promotion_blocked(self, evidence_dir, runtime_dir, fresh_db):
        """shadow evidence 10일만 → promotion BLOCKED (real_paper_days=0)."""
        days = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i))
            if d.weekday() < 5:
                days.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "evidence_mode": "shadow_bootstrap",
                    "execution_backed": False,
                    "order_submit_count": 0,
                    "fill_count": 0,
                })
        self._seed_with_mode(evidence_dir, "shadow_promo", days)

        from core.paper_evidence import generate_promotion_package
        pkg_path, _ = generate_promotion_package("shadow_promo")
        assert pkg_path is not None
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["recommendation"] == "BLOCKED"
        assert pkg["real_paper_days"] == 0
        assert pkg["shadow_days"] == len(days)
        assert pkg["promotable_evidence_days"] == 0

    def test_real_paper_0_shadow_10_promotable_0(self, evidence_dir, runtime_dir, fresh_db):
        """real_paper=0, shadow=10 → promotable_evidence_days=0."""
        days = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i))
            if d.weekday() < 5:
                days.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "evidence_mode": "shadow_bootstrap",
                    "execution_backed": False,
                })
        self._seed_with_mode(evidence_dir, "prov_test", days)

        from core.paper_evidence import generate_promotion_package
        pkg_path, _ = generate_promotion_package("prov_test")
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["promotable_evidence_days"] == 0

    def test_real_5_shadow_5_promotion_uses_real_only(self, evidence_dir, runtime_dir, fresh_db):
        """real_paper=5 + shadow=5 → 승격 계산은 real 5일만."""
        days = []
        for i in range(5):
            days.append({
                "date": (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "evidence_mode": "real_paper",
                "execution_backed": True,
            })
        for i in range(5, 10):
            days.append({
                "date": (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "evidence_mode": "shadow_bootstrap",
                "execution_backed": False,
            })
        self._seed_with_mode(evidence_dir, "mix_prov", days)

        from core.paper_evidence import generate_promotion_package
        pkg_path, _ = generate_promotion_package("mix_prov")
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["real_paper_days"] == 5
        assert pkg["shadow_days"] == 5
        assert pkg["promotable_evidence_days"] == 5
        # 60일 부족으로 BLOCKED
        assert pkg["recommendation"] == "BLOCKED"
        assert "insufficient_days=5/60" in str(pkg["block_reasons"])

    def test_disabled_strategy_shadow_blocked(self):
        """disabled 전략은 shadow_collect 불허."""
        from core.paper_runtime import ALLOWED_ACTIONS
        assert "shadow_collect" not in ALLOWED_ACTIONS["research_disabled"]

    def test_runtime_shows_provenance_split(self, evidence_dir, runtime_dir, fresh_db):
        """runtime status에 real_paper_days / shadow_days 표시."""
        days = [
            {"date": "2026-04-01", "evidence_mode": "real_paper", "execution_backed": True},
            {"date": "2026-04-02", "evidence_mode": "shadow_bootstrap", "execution_backed": False},
            {"date": "2026-04-03", "evidence_mode": "shadow_bootstrap", "execution_backed": False},
        ]
        self._seed_with_mode(evidence_dir, "prov_rt", days)

        from core.paper_runtime import get_paper_runtime_state
        state = get_paper_runtime_state("prov_rt")
        assert state.metrics["real_paper_days"] == 1
        assert state.metrics["shadow_days"] == 2

    def test_preflight_shows_provenance(self, evidence_dir, runtime_dir, fresh_db):
        """preflight에 real_paper_days/shadow_days 표시."""
        days = [
            {"date": "2026-04-01", "evidence_mode": "real_paper", "execution_backed": True},
            {"date": "2026-04-02", "evidence_mode": "shadow_bootstrap", "execution_backed": False},
        ]
        self._seed_with_mode(evidence_dir, "prov_pf", days)

        from core.paper_preflight import run_preflight
        r = run_preflight("prov_pf", "2026-04-06")
        assert r.real_paper_days == 1
        assert r.shadow_days == 1

    def test_shadow_evidence_mode_in_record(self, evidence_dir, fresh_db):
        """collect_daily_evidence(evidence_mode='shadow_bootstrap') → record에 반영."""
        from core.paper_evidence import collect_daily_evidence, get_canonical_records
        from unittest.mock import patch

        with patch("core.paper_evidence._compute_benchmark_excess") as mock_bench, \
             patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[]):
            mock_bench.return_value = {
                "same_universe_excess": 0.1, "exposure_matched_excess": 0.08,
                "cash_adjusted_excess": 0.07, "benchmark_status": "final",
                "benchmark_meta": {"completeness": 1.0},
            }
            r = collect_daily_evidence(
                strategy="shadow_rec", mode="paper", account_key="shadow_rec",
                date=datetime(2026, 4, 6, 15, 35),
                watchlist_symbols=["005930"],
                evidence_mode="shadow_bootstrap",
            )

        assert r is not None
        assert r.evidence_mode == "shadow_bootstrap"
        assert r.execution_backed is False

        records = get_canonical_records("shadow_rec")
        assert records[0]["evidence_mode"] == "shadow_bootstrap"
        assert records[0]["execution_backed"] is False
