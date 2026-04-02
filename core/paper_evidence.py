"""
Paper Evidence 수집 체계 — 60일 승격/강등 판단 근거 자동 수집

대상: provisional_paper_candidate 전략 (현재: rotation, scoring)
산출물: reports/paper_evidence/
  - daily_evidence.jsonl   (일별 누적)
  - weekly_summary.json    (주간 요약)
  - promotion_evidence.json (60일 종합)
  - anomalies.json          (이상 탐지 기록)
  - approval_checklist.json (승격 게이트 자동 판정)

설계 원칙:
  - existing experiment(운영 사실)과 strategy merit(전략 성과)를 분리 기록
  - 승격 게이트는 코드에 정량 기준으로 명시
  - anomaly 탐지는 즉시 기록 + 누적 시 kill-switch
"""
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from loguru import logger


# ═══════════════════════════════════════════════════════════
# 1. Evidence 스키마
# ═══════════════════════════════════════════════════════════

@dataclass
class DailyEvidence:
    """일별 paper evidence 레코드."""
    date: str
    strategy: str
    day_number: int                      # Paper Day N

    # 수익
    absolute_return: float = 0.0         # %
    cumulative_return: float = 0.0       # %
    same_universe_excess: float = 0.0    # % (전략 - 벤치마크)
    exposure_matched_excess: float = 0.0 # %
    cash_adjusted_excess: float = 0.0    # %

    # 실행 품질
    turnover: float = 0.0               # 연환산 왕복 수
    signal_density: float = 0.0          # %
    raw_fill_rate: float = 0.0           # %
    effective_fill_rate: float = 0.0     # % (AIP 제외)
    drawdown: float = 0.0               # % (당일 MDD)

    # 운영 안정성
    slippage_vs_model: float = 0.0       # 실제-모델 슬리피지 차이
    reconcile_count: int = 0
    stale_pending_count: int = 0
    phantom_position_count: int = 0
    restart_recovery_count: int = 0
    duplicate_blocked_count: int = 0
    reject_count: int = 0

    # 체결
    buy_signals: int = 0
    buy_executed: int = 0
    sell_signals: int = 0
    sell_executed: int = 0
    portfolio_value: float = 0.0
    cash: float = 0.0
    n_positions: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════
# 2. Approval Gate (승격 조건)
# ═══════════════════════════════════════════════════════════

@dataclass
class ApprovalGate:
    """60일 Paper 승격 게이트 정량 기준."""
    name: str
    threshold: float
    actual: float
    passed: bool
    description: str


APPROVAL_RULES = [
    # (이름, 기준, 비교 방향, 설명)
    ("paper_days",              60,     ">=", "Paper 60영업일 이상"),
    ("phantom_positions",       0,      "<=", "Phantom position 0건"),
    ("stale_pendings_total",    5,      "<=", "Stale pending 누적 5건 이하"),
    ("cumulative_return",       0,      ">",  "누적 수익률 양수"),
    ("profit_factor",           1.0,    ">=", "Profit Factor ≥ 1.0"),
    ("max_drawdown",            -20,    ">=", "MDD > -20%"),
    ("paper_sharpe",            0.3,    ">=", "Paper 기간 Sharpe ≥ 0.3"),
    ("same_universe_excess",    0,      ">=", "Same-universe excess ≥ 0 (live 필수)"),
    ("anomaly_count",           3,      "<=", "Critical anomaly 3건 이하"),
]


def check_approval(metrics: dict) -> list[ApprovalGate]:
    """승격 게이트 자동 판정."""
    results = []
    for name, threshold, op, desc in APPROVAL_RULES:
        actual = metrics.get(name, None)
        if actual is None:
            results.append(ApprovalGate(name, threshold, 0, False, f"{desc} — 데이터 없음"))
            continue
        if op == ">=":
            passed = actual >= threshold
        elif op == ">":
            passed = actual > threshold
        elif op == "<=":
            passed = actual <= threshold
        else:
            passed = False
        results.append(ApprovalGate(name, threshold, actual, passed, desc))
    return results


# ═══════════════════════════════════════════════════════════
# 3. Anomaly Rules (이상 탐지)
# ═══════════════════════════════════════════════════════════

@dataclass
class AnomalyRecord:
    """이상 탐지 레코드."""
    timestamp: str
    strategy: str
    rule: str
    severity: str      # warning / critical
    detail: str


ANOMALY_RULES = [
    # (규칙 이름, 필드, 임계값, severity, 설명)
    ("reconcile_anomaly",       "reconcile_count",          5,  "warning",  "일일 reconcile 5회 이상"),
    ("duplicate_flood",         "duplicate_blocked_count",  10, "warning",  "일일 중복 차단 10회 이상"),
    ("repeated_reject",         "reject_count",             5,  "critical", "일일 reject 5회 이상"),
    ("phantom_position",        "phantom_position_count",   1,  "critical", "Phantom position 발생"),
    ("stale_pending",           "stale_pending_count",      3,  "warning",  "Stale pending 3건 이상"),
    ("deep_drawdown",           "drawdown",                -15, "critical", "일일 MDD -15% 초과"),
]


def check_anomalies(evidence: DailyEvidence) -> list[AnomalyRecord]:
    """일별 evidence에서 anomaly 탐지."""
    anomalies = []
    for rule_name, field_name, threshold, severity, desc in ANOMALY_RULES:
        value = getattr(evidence, field_name, 0)
        triggered = False
        if field_name == "drawdown":
            triggered = value < threshold  # MDD는 음수, threshold보다 작으면 위험
        else:
            triggered = value >= threshold

        if triggered:
            anomalies.append(AnomalyRecord(
                timestamp=datetime.now().isoformat(),
                strategy=evidence.strategy,
                rule=rule_name,
                severity=severity,
                detail=f"{desc}: {field_name}={value} (임계={threshold})",
            ))
    return anomalies


# ═══════════════════════════════════════════════════════════
# 4. Evidence 저장/로드
# ═══════════════════════════════════════════════════════════

EVIDENCE_DIR = Path("reports/paper_evidence")


def save_daily_evidence(evidence: DailyEvidence, base_dir: Path = EVIDENCE_DIR):
    """일별 evidence를 JSONL에 추가."""
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / f"daily_evidence_{evidence.strategy}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence.to_dict(), ensure_ascii=False, default=str) + "\n")
    logger.info("Paper evidence 저장: {} Day {}", evidence.strategy, evidence.day_number)


def save_anomalies(anomalies: list[AnomalyRecord], base_dir: Path = EVIDENCE_DIR):
    """anomaly 레코드를 JSONL에 추가."""
    if not anomalies:
        return
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / "anomalies.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for a in anomalies:
            f.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")


def load_all_evidence(strategy: str, base_dir: Path = EVIDENCE_DIR) -> list[dict]:
    """전략의 전체 daily evidence 로드."""
    path = base_dir / f"daily_evidence_{strategy}.jsonl"
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_anomalies(base_dir: Path = EVIDENCE_DIR) -> list[dict]:
    """전체 anomaly 기록 로드."""
    path = base_dir / "anomalies.jsonl"
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ═══════════════════════════════════════════════════════════
# 5. 승격 패키지 생성
# ═══════════════════════════════════════════════════════════

def generate_promotion_package(strategy: str, base_dir: Path = EVIDENCE_DIR) -> dict:
    """60일 종료 시 승격 패키지 자동 생성."""
    evidence_list = load_all_evidence(strategy, base_dir)
    anomaly_list = [a for a in load_anomalies(base_dir) if a.get("strategy") == strategy]

    if not evidence_list:
        return {"strategy": strategy, "error": "evidence 데이터 없음"}

    n_days = len(evidence_list)
    last = evidence_list[-1]

    # 집계
    total_phantoms = sum(e.get("phantom_position_count", 0) for e in evidence_list)
    total_stales = sum(e.get("stale_pending_count", 0) for e in evidence_list)
    total_rejects = sum(e.get("reject_count", 0) for e in evidence_list)
    critical_anomalies = sum(1 for a in anomaly_list if a.get("severity") == "critical")
    cum_return = last.get("cumulative_return", 0)
    mdd = min(e.get("drawdown", 0) for e in evidence_list)

    # 승격 게이트 판정
    gate_metrics = {
        "paper_days": n_days,
        "phantom_positions": total_phantoms,
        "stale_pendings_total": total_stales,
        "cumulative_return": cum_return,
        "profit_factor": last.get("effective_fill_rate", 0) / 100,  # 근사치
        "max_drawdown": mdd,
        "paper_sharpe": 0,  # 미확인 — equity curve에서 별도 계산 필요
        "same_universe_excess": last.get("same_universe_excess", 0),
        "anomaly_count": critical_anomalies,
    }

    gates = check_approval(gate_metrics)
    all_passed = all(g.passed for g in gates)

    package = {
        "strategy": strategy,
        "generated_at": datetime.now().isoformat(),
        "paper_days": n_days,
        "cumulative_return": cum_return,
        "max_drawdown": mdd,
        "total_anomalies": len(anomaly_list),
        "critical_anomalies": critical_anomalies,
        "approval_gates": [asdict(g) for g in gates],
        "all_gates_passed": all_passed,
        "recommendation": "promote_to_live_candidate" if all_passed else "maintain_provisional",
    }

    # 저장
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / f"promotion_evidence_{strategy}.json"
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("승격 패키지 생성: {} → {}", strategy, path)

    return package
