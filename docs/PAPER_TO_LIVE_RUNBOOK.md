# Paper → Live 운영 런북 (수익까지의 검증된 경로)

> 목적: 백테스트로 유망한 전략을 **정직하게 검증**하고, 60영업일 paper 증거를 쌓아
> 안전 게이트를 통과시킨 뒤 live로 승격하는 전체 경로를 한 곳에 정리한다.
> 작성: 2026-05-29. 관련 설계는 `quant_trader_design.md`, 구조는 `docs/PROJECT_GUIDE.md` 참고.

---

## 0. 정직한 현재 상태 (먼저 읽을 것)

- **수익 나는 후보는 `target_weight_rotation` 패밀리 하나뿐.** 나머지 baseline 전략
  (scoring, mean_reversion, trend_following, breakout_volume, relative_strength_rotation)은
  벤치마크(KOSPI) 대비 **음(-)의 초과수익** → live 후보 아님.
- target_weight 헤드라인(+171%/3년, Sharpe 1.41)은 **과대평가**되어 있다:
  - **생존자 편향**: canonical 유니버스가 과거엔 현재 상장목록을 써서 상폐 종목을 제외했다.
    (수정됨 — 아래 §3 참고. 단, 시점 데이터엔 pykrx가 필요.)
  - **다중검정**: 23개+ 변형을 탐색해 최고를 고르면 in-sample Sharpe가 부풀려진다.
    (deflated Sharpe로 보고 — 아래 §3.)
  - **진짜 OOS holdout 부재**: 현재 walk-forward는 전체 구간 튜닝 파라미터를 각 구간에
    재평가하므로 모든 구간이 in-sample. (TODO: §6.)
- ⇒ **"안정적 고수익"은 아직 입증되지 않았다.** 아래 경로로 정직한 엣지를 먼저 확인할 것.

---

## 1. 사전 점검 (매 작업 시작 시)

```bash
# 항상 프로젝트 venv 사용 (bare python은 의존성 없음)
.venv\Scripts\python.exe -m pytest tests/ -q          # 전체 그린 확인
.venv\Scripts\python.exe tools/evaluate_and_promote.py --check-only   # 운영 artifact 동기화/freshness
```
- `--check-only`가 FAIL(stale)이면 §3의 `--canonical` 재실행 필요.
- 테스트는 임시 DB로 격리되어 **운영 `data/quant_trader.db`를 건드리지 않는다**
  (이전 "DB 복구(restore)" 반복의 근본 원인이었음 — `tests/conftest.py`로 차단).

## 2. 백테스트로 후보 탐색

```bash
.venv\Scripts\python.exe main.py --mode backtest --strategy scoring --symbols 005930 --start 2023-01-01 --end 2025-12-31
.venv\Scripts\python.exe main.py --mode validate --strategy <name>     # 3~5년, 벤치마크 대비
```

## 3. 정직한 canonical 평가 (생존자 편향·다중검정 보정)

```bash
.venv\Scripts\python.exe tools/evaluate_and_promote.py --canonical
```
- 산출물 `reports/promotion/`의 `universe_selection.survivorship_controlled`를 **반드시 확인**:
  - `true` → pykrx 시점(point-in-time) 유니버스로 만들어진 정직한 수치.
  - `false` → 현재 상장목록 폴백(생존자 편향). **이 수치로 live 판단 금지.**
- ⚠️ **이 저장소(dev 샌드박스)에서는 pykrx 과거 상장 endpoint가 비어 있어 항상
  `false`로 폴백된다.** 정직한 수치는 **pykrx KRX 과거조회가 동작하는 환경**(또는 상폐
  포함 구성종목 데이터셋)에서 `--canonical`을 돌려야 얻는다.
- 다중검정: 스윕 artifact의 `multiple_testing.best_by_sharpe_deflated.dsr`(≥0.95 권장)와
  `validation_warnings`를 확인. `deflated_sharpe_fail`이면 과적합 의심.

## 4. Paper pilot 60영업일 누적 (실제 수익 경로)

```bash
# 단발 1회 순회 (스모크/수동)
.venv\Scripts\python.exe main.py --mode paper --strategy <name>

# 무인 상시 구동(권장) — 배포 환경에서 systemd로
#   deploy/quant_trader.service : ExecStart=... main.py --mode schedule  (Restart=always)
#   기본 signal-only. full paper 진입은 QUANT_AUTO_ENTRY=true.
```
- 매 영업일 장마감 후 evidence가 `reports/paper_evidence/daily_evidence_<strategy>.jsonl`에
  append되고, 다음날 장전 finalize로 benchmark가 provisional→final 승격된다.
- 진행 점검: `tools/paper_runtime_status.py`, `tools/paper_pilot_control.py --status`,
  target-weight는 `tools/target_weight_rotation_pilot.py --daily-ops-summary`.
- **DB 영속성 주의**: 운영 DB(`data/quant_trader.db`)는 gitignore·로컬 전용이다. 60일
  누적이 유효하려면 **DB가 보존되는 단일 서버에서 상시 구동**해야 한다(체크아웃/리셋·테스트가
  DB를 비우면 증거가 끊긴다 — 테스트發 wipe는 §1대로 이미 차단됨).

## 5. Live 승격 (4중 안전 게이트)

`current_blockers.go_live=true` + canonical live gate 통과 + 전략 상태 레지스트리 live 허용
+ `ENABLE_LIVE_TRADING=true` + `--confirm-live` 가 **모두** 충족돼야 한다.
```bash
.venv\Scripts\python.exe main.py --mode live --strategy <name> --confirm-live
```
승격 전제: 60영업일 execution-backed paper 증거 + 양(+)의 paper Sharpe/benchmark 초과 +
MDD/PF/turnover 게이트 통과 + **정직한(생존자 통제) 백테스트 엣지**.

## 6. 남은 검증 강화 (TODO — 정직한 환경에서)

1. **시점 유니버스 재검증**(#5): pykrx 동작 환경에서 `--canonical` → `survivorship_controlled=true`로
   target_weight 진짜 엣지 측정. 헤드라인 대비 얼마나 남는지 판정.
2. **진짜 OOS holdout**(#6): 함수는 구현됨 — `research_candidate_sweep.evaluate_oos_holdout()`
   (train 구간 rank_score로만 변형 선택 → untouched test 구간 성과/degradation 보고).
   남은 일: `run_candidate_sweep`에 `--oos-holdout-split` 옵션으로 연결 + 정직한 유니버스 환경에서 실행.
3. **deflated Sharpe 게이트화**: 현재 report-only인 DSR/생존자 경고를 승격 게이트에 연결 검토.
