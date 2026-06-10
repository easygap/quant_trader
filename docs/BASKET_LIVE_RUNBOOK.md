# 바스켓 live 전환 런북 (kr_diversified_hold)

> 작성: 2026-06-10. paper 운영 평가가 `PASS_CANDIDATE`에 도달한 뒤, 임기응변 없이
> 단계적으로 live 전환하기 위한 절차서. 승격 기준은 `docs/BASKET_PAPER_EVALUATION.md`,
> 게이트 구현은 `core/live_readiness.py`(바스켓 전용 분기) 참고.

## 전제 (모두 충족해야 시작)

```
.venv\Scripts\python.exe tools/basket_paper_evaluation.py
```
- 판정 **PASS_CANDIDATE** (60영업일·스냅샷 커버리지 ≥95%·dead-letter 0건·비용 드래그 ≤1%/년)
- `--mode health` 가 바스켓 운영 이슈 없음
- `--mode deploy_check --basket kr_diversified_hold` 로 계획 주문·예상 비용·회전율 확인

## 안전장치 요약 (live 리밸런싱이 통과해야 하는 것들)

`python main.py --mode rebalance` 가 live에서 주문에 도달하려면 **전부** 통과해야 한다:
1. `config.trading.mode: "live"` (settings.yaml)
2. 환경변수 `ENABLE_LIVE_TRADING=true`
3. CLI 플래그 `--confirm-live`
4. 바스켓별 live gate `basket_rebalance:kr_diversified_hold`
   (= paper 평가 PASS_CANDIDATE + 바스켓 enabled + 비중 합 1.0 + 데이터 소스 health)
5. KIS↔DB 포지션 동기화 성공 (실패 시 즉시 중단)
6. KIS 잔고 확인 (미확인 시 사이징 fail-closed — 계획 자체를 중단)
7. 주문 단계: OrderGuard·미체결 조회 fail-closed·체결 확인·reconcile 보류·응답 유실 재전송 금지

하나라도 빠지면 주문이 나가지 않는다. 이 차단이 정상 동작이다.

## 단계적 전환

### Phase 0 — paper (현재)
- 일일 자동 사이클(스케줄 작업)이 트랙레코드를 축적 중. 개입 불필요.

### Phase 1 — KIS 모의투자 서버로 live 경로 리허설 (권장, **지금 바로 가능**)
실계좌 없이 **live 코드 경로 전체**(KIS 인증·주문·체결조회·sync)를 리허설한다.
모의서버(`use_mock: true`)는 실돈이 아니므로 **60영업일 평가를 기다리지 않고**
증거 축적과 병행할 수 있다 — 게이트가 모의서버에서는 평가 미통과를 경고로만
처리한다(실계좌 `use_mock: false` 전환 시에는 평가 통과가 다시 필수).
1. `.env`: KIS 모의투자 앱키/시크릿/계좌 설정
2. `settings.yaml`: `kis_api.use_mock: true` (모의투자 도메인), `trading.mode: "live"`
3. 실행:
   ```
   set ENABLE_LIVE_TRADING=true
   .venv\Scripts\python.exe main.py --mode rebalance --basket kr_diversified_hold --dry-run
   .venv\Scripts\python.exe main.py --mode rebalance --basket kr_diversified_hold --confirm-live
   ```
4. 검증: 주문 체결 확인 로그(`✅ 매수 완료`), KIS 모의계좌 잔고 = DB 포지션,
   `requires_reconcile` 발생 시 다음 사이클에서 자동 대조되는지
5. 며칠 반복 후 이상 없으면 Phase 2

### Phase 2 — 실계좌 소액
1. **소액만 입금한 실계좌** 사용 (live 자본은 KIS 잔고 기준으로 사이징되므로
   계좌 잔고 자체가 리스크 상한이다)
2. `settings.yaml`: `kis_api.use_mock: false`, 실계좌 번호
3. Phase 1과 동일 명령. `--dry-run`으로 계획 먼저 확인 후 실행
4. 1~2주 관찰: 체결가 품질(슬리피지), 일일 사이클 안정성, 알림 동작

### Phase 3 — 목표 자본
- Phase 2 이상 없을 때 증액. `max_turnover_ratio`(15%)가 사이클당 거래를 제한하므로
  증액 직후에도 점진 매입된다(한 번에 시장가 폭탄 없음).

## 비상 절차

| 상황 | 명령 |
|---|---|
| 전 종목 긴급 청산 | `ENABLE_LIVE_TRADING=true` + `.venv\Scripts\python.exe main.py --mode liquidate --confirm-live` |
| live 즉시 중지(주문만 차단) | 환경변수 `ENABLE_LIVE_TRADING` 제거 — 이후 모든 live 주문 경로 차단 |
| paper로 복귀 | `settings.yaml` `trading.mode: "paper"` |

## 전환 후 일상 운영

- 일일: 자동 사이클 보고(리밸런싱 결과·NAV·평가 진행률) 확인
- `--mode health`: 전략·바스켓·blocker 통합 점검 (스냅샷 끊김 자동 경고)
- DB 백업: `data/backups/`에 일일 자동 (retention 14일)
- 주의: live 일일 NAV 스냅샷은 상시 스케줄러(장마감)가 담당 — 일일 CLI 운영을
  유지한다면 rebalance 종료 시 저장되는 것은 paper 스냅샷 경로이므로, live 전환 후
  상시 스케줄러(systemd, `deploy/`) 구동을 권장
