# Schedule Dry Run Summary
실행 시각: 2026-03-26T17:20:27.225098
Git commit: 9c88254
Config hash: 5611a6d9c5d4823c

## 실행 결과
| 항목 | 결과 |
|------|------|
| schedule 시작 | ✅ 정상 (EXIT=124, timeout에 의한 종료) |
| 전략 상태 체크 | ✅ scoring=experimental, schedule 허용 |
| watchlist 체크 | ✅ 3종목 (005930, 000660, 035420) |
| 장전 분석 (_run_pre_market) | ✅ 3종목 신호 생성, OperationEvent 3건 |
| 장마감 리포트 (_run_post_market) | ✅ DailyReport 1건, PortfolioSnapshot 1건 |
| 거래시간 정책 | ✅ 17:18 = 비장중 → 대기 모드 진입 |

## DB 기록 확인
| 테이블 | 건수 | 상태 |
|--------|------|------|
| OperationEvent | 7건 | ✅ SIGNAL 이벤트 기록 |
| TradeHistory | 1건 | ✅ BUY lifecycle 기록 (signal_at/order_at/fill_at) |
| PortfolioSnapshot | 1건 | ✅ 총 9,996,107원, -0.04% |
| DailyReport | 1건 | ✅ 매매 1건, 실현손익 -3,893원 |

## 장중/장마감 후 실행 정책
- **장전 (08:50~09:00)**: _run_pre_market() — watchlist 전체 신호 분석, 매수 후보 선정
- **장중 (09:00~15:30)**: 10분 간격 _run_monitoring() — SL/TP 체크, 블랙스완 감지, 재스캔
- **장 시작 30분 (09:00~09:30)**: 신규 매수 차단 (변동성 구간)
- **장 종료 30분 (15:00~15:30)**: 신규 매수 차단 (변동성 구간)
- **장마감 (15:30~15:40)**: _run_post_market() — 스냅샷 저장, 일간 리포트, 주간(금) 리포트
- **비장중**: 대기 (60초 간격 체크, 거래 없음)

## 본실험 조건
- 전략: scoring (experimental, paper only)
- watchlist: 005930 (삼성전자), 000660 (SK하이닉스), 035420 (NAVER)
- 벤치마크: KOSPI (KS11)
- 기간: 2026-03-27 ~ 2026-06-19 (60영업일)
- 비용: 수수료 0.015% + 세금 0.20% + 슬리피지 0.05%

## 판정
60영업일 본실험 시작 가능.
