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

## 4-B. 분산 대형주 buy&hold 운용 (수익성 결론의 배포 경로)

`docs/PROFITABILITY_FINDINGS.md` 결론대로, 대형주에서 능동 alpha는 없고 **분산 보유(베타)가
현실적 고수익 경로**다. 이걸 실제로 굴리는 경로는 바스켓 리밸런싱이다.

```bash
# 1) 계획만 확인 (주문 없음) — 목표비중 vs 실제 드리프트, 매수/매도 계획 출력
.venv\Scripts\python.exe main.py --mode rebalance --basket kr_diversified_hold --dry-run

# 2) 실제 paper 실행 (config trading.mode=paper 일 때) — 드리프트 임계 초과분만 매매
.venv\Scripts\python.exe main.py --mode rebalance --basket kr_diversified_hold
```
- `config/baskets.yaml`의 `kr_diversified_hold`는 섹터 분산 10종목 균등(각 10%), 드리프트 8%p,
  회전 상한 15%로 **저회전 buy&hold**에 맞췄다(능동 리밸런싱은 비용이라 회전을 최소화). 기본
  `enabled: false`이며, 운영자가 paper로 충분히 검증한 뒤 `enabled: true`로 켠다.
- 실행 경로는 빈 포트폴리오에서 목표비중으로 진입 → 이후 드리프트 8%p 초과 시에만 부분 교정한다.
  회전 상한 때문에 한 번에 전 종목이 안 채워질 수 있고, 다음 리밸런싱에서 채워진다(의도된 저비용 동작).
- live 바스켓 리밸런싱은 §5의 게이트(바스켓별 canonical live gate + 계정/태그 일치 + KIS↔DB 동기화)를
  모두 통과해야 실주문이 나간다. paper에서는 그 전 단계까지 안전하게 검증할 수 있다.

**중요 — paper 실주문 게이트 구조(실측 확인 2026-06-01):**
바스켓은 `config/baskets.yaml`에 정의된 포트폴리오라 `strategies/__init__.py`의 STRATEGY_STATUS
레지스트리(=시그널 전략 목록)와는 별개다. 그래서:
- `--dry-run`은 계정/전략 등록과 무관하게 항상 동작한다(계획 확인용).
- **paper 신규 BUY 실주문**은 `OrderExecutor`의 paper-entry 가드를 통과해야 한다. 이 가드는
  `account_key`로 전략을 식별해 preflight/runtime 상태를 본다. 바스켓을 paper로 "증거 축적"까지
  하려면 두 경로 중 하나가 필요하다:
  - (A) 바스켓을 능동 알파 전략처럼 취급하지 않는다 → buy&hold는 60일 evidence 승격이 목적이
    아니므로, paper에서는 `--dry-run` + 계획/비용 점검으로 검증하고, 실거래는 §5의 **바스켓 전용
    live gate**(`basket_rebalance:<name>`)로 바로 승격하는 게 설계 의도에 맞다.
  - (B) 굳이 paper evidence를 쌓고 싶으면, 바스켓에 대응하는 전략명을 STRATEGY_STATUS에 paper
    허용으로 등록하고 `tools/paper_bootstrap.py --mode shadow`로 bootstrap paradox를 푼 뒤
    `tools/paper_preflight.py`로 preflight를 통과시킨다.
- 요약: **buy&hold 바스켓의 정상 경로는 (A)** — paper는 dry-run 검증, 실거래는 바스켓 live gate.
  preflight가 바스켓 실BUY를 막는 건 버그가 아니라 "전략 등록 없는 임의 paper 주문"을 막는 안전장치다.

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
