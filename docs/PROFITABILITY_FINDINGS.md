# 수익성 정직 점검 결과 (먼저 읽을 것)

> 목적: 이 시스템으로 "안정적 고수익"이 가능한지, 지금까지의 정직한 검증 결과를 한 곳에
> 모은다. 헤드라인 백테스트 수치만 보고 실거래에 들어가는 것을 막기 위한 문서다.
> 최종 갱신: 2026-06-01.

## 한 줄 결론

**현재 전략으로는 벤치마크를 이기는 검증된 alpha가 없다.** 절대수익은 높아 보여도 그건
시장(KOSPI) 베타이지 초과수익이 아니다. 지금 상태로 실거래에 자본을 넣는 것은 권장하지 않는다.

## 전략별 현황

| 전략 | 절대수익 | 벤치마크 초과 | 판정 |
|------|---------|-------------|------|
| target_weight_rotation 계열 | 높음(+87~171%) | **음(-)** | 베타뿐, alpha 미검증 |
| scoring, mean_reversion, trend_following, breakout_volume, relative_strength_rotation | 다양 | **음(-) (−140~−160%p)** | buy&hold에 패배 |

벤치마크 = 같은 유니버스 동일비중 buy & hold (EW B&H).

## 정직한 OOS holdout 실측 (2026-06-01)

대형 유동주 10종목(삼성전자·SK하이닉스·NAVER·LG화학·삼성SDI·현대차·기아·셀트리온·카카오·
현대모비스)으로 target_weight_rotation 21개 변형을 `--oos-holdout-split 2025-01-01`로 검증
(train 2023–24로만 변형 선택, test 2025는 한 번도 안 봄):

- **판정: NO_ALPHA_CANDIDATE — 21개 변형 전부 벤치마크에 패배** (초과수익 모두 음수).
- 최고 변형 `top3_40_100_hold2`: 전체기간 +87.6%, Sharpe 0.88, MDD −34.5%.
  그러나 **벤치마크 초과수익 −24.4%p, 초과 Sharpe −0.19.** EW B&H(Sharpe 1.07)가 더 낫다.
- **Deflated Sharpe DSR=0.748 < 0.95** → 21개 변형 탐색의 다중검정 운으로 설명 가능(과적합 신호).
- OOS holdout이 "통과"한 건 절대 Sharpe 기준(test 1.71)일 뿐, 벤치마크 대비로는 여전히 패배.

즉 "train으로 고르고 test로 본다"는 정직한 방식으로 봐도, 대형주에서는 초과수익이 없다.

## 헤드라인(+171%)이 과대평가된 이유

이전 canonical 평가의 +171%/Sharpe 1.41 수치는 다음 때문에 부풀려져 있다:

1. **생존자 편향**: 과거 유니버스가 현재 상장목록 기준이라 상폐 종목이 빠졌다. (코드는 수정됐지만
   시점 데이터에 pykrx 과거 상장목록 조회가 필요한데, 이 환경에선 빈 값이 와서 정직한 재측정 불가.)
2. **다중검정**: 수십 개 변형 중 최고를 고르면 in-sample Sharpe가 구조적으로 부풀려진다(DSR로 확인).
3. **진짜 holdout 부재**: 기존 walk-forward는 전체기간 튜닝 파라미터를 각 구간에 재평가 → 전부 in-sample.
   (이건 `--oos-holdout-split`으로 해결, 위 실측이 그 결과.)

## 검증 도구 (이 결론을 재현하는 방법)

```bash
# 정직한 OOS holdout sweep (train으로 고르고 untouched test 보고)
.venv\Scripts\python.exe tools/research_candidate_sweep.py \
  --symbols "005930,000660,035420,051910,006400,005380,000270,068270,035720,012330" \
  --candidate-family target_weight_rotation \
  --start 2023-01-01 --end 2025-12-31 --oos-holdout-split 2025-01-01 --quick

# 산출물 JSON의 oos_holdout / multiple_testing / validation_warnings 확인
```

- `backtest/statistical_validation.py`: Probabilistic/Deflated Sharpe (다중검정 보정).
- `research_candidate_sweep.evaluate_oos_holdout()`: 진짜 out-of-time holdout.
- 두 신호 모두 sweep artifact와 Markdown 리포트에 노출된다.

## 그래서 다음에 뭘 해야 하나

"안정적 고수익"으로 가는 정직한 경로는 둘 중 하나다:

1. **생존자 통제 유니버스로 헤드라인 재측정**: pykrx KRX 과거 상장목록이 동작하는 환경(또는 상폐
   포함 데이터셋)에서 `evaluate_and_promote.py --canonical`을 돌려 `survivorship_controlled=true`로
   진짜 엣지를 측정. 그래도 초과수익이 양수로 남는지 확인.
2. **새로운 alpha 소스 설계**: 단순 모멘텀 로테이션을 넘어 벤치마크를 실제로 이기는 신호를 연구.
   (지금까지의 모든 변형은 벤치마크 대비 음수였다.)

둘 중 하나로 **벤치마크 초과수익이 양수인 후보**가 나오기 전까지는, 60영업일 paper도 live도
"수익 검증"이 아니라 "운영 안정성 검증"으로만 의미가 있다. 자세한 운영 경로는
`docs/PAPER_TO_LIVE_RUNBOOK.md` 참고.
