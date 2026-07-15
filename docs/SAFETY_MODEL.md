# 안전 모델과 실전 운영 경계

## 결론부터

이 프로그램은 **원금 보전, 무손실, 수익 또는 주문 체결을 보장하지 않는다.** 안전장치는
실수·중복 주문·장부 오염을 줄이고 손실 한도를 지키기 위한 방어선이지, 시장 위험을 없애는
수단이 아니다. 갭, 급격한 변동, 거래정지, VI, 유동성 고갈, 슬리피지, 증권사·네트워크 장애,
전략 성능 저하 때문에 설정한 손절가보다 큰 손실이나 주문 미체결이 발생할 수 있다.

**현재 실전 판정은 NO-GO다.** 운영자는 최신 코드와 설정으로 만든 검증 증거, paper 운영
게이트, KIS 연결·잔고 동기화, 명시적 계좌 라우팅, clean Git worktree를 모두 통과하기 전에는
실전 주문을 열면 안 된다. 게이트 통과는 미래 수익을 뜻하지 않으며, 통과 후에도 소액으로
별도 승인해야 한다.

## 기본 원칙

- 조회 실패, NaN/무한대, 상태 불명, 검증 증거 불일치는 성공으로 간주하지 않고 주문을 막는다.
- 신규 BUY는 노출·현금·종목 수·1회 손실 예산 등 위험 한도를 주문 후 예상 상태로 검사한다.
- 손절과 긴급 청산 SELL은 신규 BUY 차단과 분리해 열어 두되, 실제 체결은 보장하지 않는다.
- 실계좌 KIS 주문 함수의 직접 호출은 차단하고, 주문 실행기의 가드가 승인한 짧은 구간에서만
  제출한다.
- `use_mock=true`라도 실효 URL이 공식 VTS(`openapivts`)가 아니면 실돈 가능 endpoint로 취급해
  kill switch, 실계좌 리스크 프로필과 주문 capability를 모두 요구한다.
- 실전 주문 응답을 잃었을 때는 같은 주문을 재전송하지 않는다. 중복 체결보다 운영 중단과
  수동 대조를 우선한다.

## paper/live 장부 격리

`Position`과 `PortfolioSnapshot`은 `mode`를 포함해 각각
`(mode, account_key, symbol)`, `(mode, account_key, date)` 단위로 유일하다. 관련 저장소 조회와
쓰기에도 `paper` 또는 `live` 모드를 전달하므로 같은 전략·종목이라도 두 장부가 서로 덮어쓰지
않는다.

기존 DB의 mode 없는 행은 거래 이력의 mode가 하나로 일관될 때만 그 mode로 귀속한다. 거래
이력이 없거나 paper/live가 섞인 행은 `legacy`로 격리하며 신규 paper/live 조회에 포함하지
않는다. 이 이전은 브로커 잔고가 맞다는 증명이 아니다. 마이그레이션 전 백업을 보존하고,
legacy 행은 KIS 체결·잔고와 수동 대조한 뒤 별도 처리한다.

## 실전 진입 조건

실전은 설정 파일의 기본 계좌로 조용히 폴백하지 않는다. 실행할 전략 키를
`config/settings.yaml`의 `kis_api.accounts`에 먼저 선언하고 계좌번호는 `.env`로 주입한다.

```yaml
kis_api:
  accounts:
    scoring: ""  # 실제 번호는 저장소에 커밋하지 않는다
```

```dotenv
KIS_ACCOUNT_NO_SCORING=12345678-01
```

키에 `:` 또는 `-`가 있으면 환경변수 이름에서는 `_`로 바뀐다. 예를 들어
`basket_rebalance:kr_pocket`은 `KIS_ACCOUNT_NO_BASKET_REBALANCE_KR_POCKET`이다. YAML에 키를
선언하지 않은 `KIS_ACCOUNT_NO_*` 환경변수는 무시되며 경고가 남는다. 같은 계좌를 여러 전략이
공유하려는 경우에도 각 전략 키에 의도적으로 같은 번호를 선언해야 한다.

실전 명령은 다음 두 운영자 확인을 모두 요구하지만, 이것만으로 충분하지 않다.

```powershell
$env:ENABLE_LIVE_TRADING = "true"
.venv\Scripts\python.exe main.py --mode live --strategy scoring --confirm-live
```

이후에도 다음 조건 중 하나라도 실패하면 진입하지 않는다.

- 전략 레지스트리의 live 허용 상태와 최신 canonical 승격 증거
- 증거의 commit/config hash, 최신성, paper 품질 및 blocker 판정
- tracked/untracked 변경이 모두 없는 clean Git worktree; Git 상태 확인 실패도 차단
- 단일 live 런타임 락 획득
- KIS 인증·연결·잔고 조회 및 KIS↔DB 포지션 동기화
- 전략별 `kis_api.accounts` 라우팅

`--mode schedule`은 paper 전용이다. 게이트를 우회하는 `--force-live` 경로는 없다.

## 실전 긴급 전량 청산

실계좌를 대상으로 할 때는 `--liquidate-live`를 반드시 명시한다. 이 플래그가 없으면 CLI 청산은
paper 장부를 대상으로 한다.

```powershell
$env:ENABLE_LIVE_TRADING = "true"
.venv\Scripts\python.exe main.py --mode liquidate --liquidate-live --confirm-live
```

실전 청산은 브로커 동기화나 포지션 조회보다 먼저 영속 전역 HALT를 기록해 다른 프로세스의
신규 BUY를 막는다. HALT는 청산 종료 후 자동 해제되지 않는다. KIS 체결·미체결·잔고와 DB를
대조한 뒤에만 사유를 남겨 해제한다.

```powershell
.venv\Scripts\python.exe tools/clear_trading_halt.py --confirm --reason "KIS 체결·미체결·잔고 대조 완료"
```

손절, 트레일링 스탑, 갭다운, 블랙스완, 강제 청산 등 긴급 사유의 국내 주식 SELL은 KIS 시장가
(`ORD_DVSN=01`, 가격 0)로 제출한다. 이는 오래 남는 지정가보다 체결 가능성을 우선하는 선택일
뿐이다. 유동성이 부족하거나 거래가 정지되면 미체결·부분 체결될 수 있고, 급변장에서는 예상보다
매우 불리한 가격에 체결될 수 있다. 시장가 주문도 손실 상한을 보장하지 않는다.

## 체결 불확실성과 장부 실패 대응

다음 상태는 정상 완료가 아니라 **운영 사고 대응 상태**다.

- 주문 응답 유실로 접수 여부를 모름
- 부분 체결 또는 체결 수량·가격을 확정하지 못함
- 브로커 체결 후 `TradeHistory`/`Position` 저장이 완료되지 않음

이 경우 주문을 재전송하지 않고, 미완료 주문 기록과 종목 중복 주문 가드를 유지·연장한다.
전역 HALT와 critical 알림을 남기고 장부 반영을 보류하거나 가능한 DB 기록을 되돌린다. 자동
처리가 끝났다는 뜻이 아니며 다음 순서로 수동 복구해야 한다.

1. live 프로세스와 신규 주문을 중지하고 HALT를 유지한다.
2. KIS 주문·체결 내역, 미체결 수량, 현재 잔고를 주문번호 기준으로 확인한다.
3. 동일한 `mode=live`와 `account_key`의 `OrderRecord`, `TradeHistory`, `Position`을 대조한다.
4. 부분 체결 수량과 실제 평균 체결가를 확정해 승인된 reconcile 절차로 장부를 복구한다.
5. 다시 잔고 동기화를 통과하고 미체결이 없음을 확인한 뒤에만 HALT를 명시적으로 해제한다.

DB를 추측으로 직접 수정하거나, 응답이 없었다는 이유로 같은 주문을 다시 보내면 안 된다.

## 알려진 한계

- KIS 주문 정정·취소(cancel/replace) API와 증권사 서버에 상주하는 native stop 주문을 구현하지
  않았다. 손절은 프로그램이 실행 중이고 시세·API가 정상일 때 감지해 주문하므로 프로세스
  중단이나 통신 장애 사이의 손실을 막지 못한다.
- 긴급 SELL 전에 기존 미체결 BUY를 자동 취소하지 않는다. 미체결 조회와 수동 대조가 필요하다.
- 호출 제한기는 같은 Python 프로세스 안에서 동일한 KIS app key·도메인을 쓰는 인스턴스끼리
  초당/분당 예산을 공유한다. 여러 OS 프로세스·호스트 사이에는 공유되지 않는다.
- 부분 체결분을 자동으로 최종 포지션에 합치는 완전한 broker reconciliation은 없다. HALT 후
  운영자 대조가 필요하다.
- 중복 주문 가드는 사고 가능성을 낮추지만 브로커·DB·프로세스 전체를 아우르는 정확히 한 번
  체결(exactly-once)을 보장하지 않는다.
- 백테스트와 paper 성과는 실전 성과가 아니다. 비용, 세금, 체결 지연, 시장 충격과 미래 시장
  구조 변화 때문에 결과가 달라질 수 있다.

이 한계 중 하나라도 현재 운영 방식에 수용 불가능하면 실전은 계속 NO-GO다.
