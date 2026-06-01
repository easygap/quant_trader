# 수익성 정직 점검 결과 (먼저 읽을 것)

> 목적: 이 시스템으로 "안정적 고수익"이 가능한지, 지금까지의 정직한 검증 결과를 한 곳에
> 모은다. 헤드라인 백테스트 수치만 보고 실거래에 들어가는 것을 막기 위한 문서다.
> 최종 갱신: 2026-06-01.

## 한 줄 결론 (2026-06-01 갱신)

**모멘텀/로테이션 계열은 전부 벤치마크에 패배했지만, 저변동성(low-volatility) 팩터에서
처음으로 벤치마크를 이기는 후보가 나왔다.** 이 후보(`low_vol_top5_120d_invvol`)는 초과수익
+12.7%p, 초과 Sharpe +0.23, walk-forward 6/6 window 양수, OOS holdout 통과, deflated
Sharpe 0.985(≥0.95)로 지금까지 만든 모든 정직한 게이트를 통과한 **유일한** 후보다.
단, 이건 대형 유동주 10종목·비(非)생존자통제 백테스트 결과이므로 canonical 평가 +
60영업일 paper 검증 전까지는 "유망한 연구 후보"이지 "검증된 수익"이 아니다.

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

즉 "train으로 고르고 test로 본다"는 정직한 방식으로 봐도, 모멘텀 계열은 초과수익이 없다.

## 돌파구 — 저변동성 팩터 (2026-06-01)

모멘텀이 실패한 이유는 전부 "수익 추격(return-chasing)"이었기 때문이다. 저변동성 팩터는 그
베타/모멘텀 틸트와 **직교(orthogonal)** 한 유일한 축이라 따로 시험했고, 처음으로 벤치마크를
이겼다. 같은 10종목·같은 OSS holdout 방식, `--candidate-family low_volatility`:

- **판정: RUN_CANONICAL_EVALUATION** (모멘텀의 NO_ALPHA_CANDIDATE와 정반대).
- **최고 후보 `low_vol_top5_120d_invvol`** (120일 실현변동성 최저 5종목 선택 + 역변동성 비중, 월 1회):

  | 지표 | 값 | 의미 |
  |------|----|----|
  | 벤치마크 초과수익 | **+12.7%p** | EW B&H를 실제로 이김(베타 아님) |
  | 초과 Sharpe | **+0.23** | risk-adjusted로도 우위 |
  | 전체 수익/Sharpe/MDD | +124.7% / 1.30 / −18.2% | 벤치마크 Sharpe 1.07보다 높음 |
  | Walk-forward | **6/6 window 양수** (positive·sharpe+ 모두 1.0) | 구간 안정성 |
  | OOS holdout (untouched 2025) | **통과**, test Sharpe 2.05 | 선택 과적합 아님 |
  | Deflated Sharpe | **DSR 0.985 ≥ 0.95** | 다중검정 운 아님 |
  | 회전율 | 194%/년 | 모멘텀(~800~1000%)보다 훨씬 낮음(비용 유리) |
  | validation_warnings | **없음** | 모든 정직 게이트 통과 |

경제적 근거: EW B&H는 고변동 종목까지 동일비중으로 담아 수익 대비 변동성만 키운다(저변동성
이상현상). 변동성 낮은 종목을 골라 역변동성으로 담으면 절대수익은 비슷해도 변동성·낙폭이 낮아
초과 Sharpe가 양수가 된다. 이게 모멘텀 40여 변형이 못 한 것이다.

**넓은 유니버스(20종목) 재현 — 좁은 유니버스 특이성 아님:** 위 10종목에 10개를 더한 20종목으로
재실행해도 `RUN_CANONICAL_EVALUATION` 유지, 오히려 더 강함. `low_vol_top5_60d_equal`은 초과수익
**+27.2%p**, 초과 Sharpe **+0.44**, MDD −13.7%, WF 6/6; `low_vol_top5_60d_invvol`은 +13.3%p,
+0.41, MDD −11.4%. 두 유니버스 모두 "저변동 top-5 선택"이 일관되게 양수 초과수익을 냈다(다만
최적 lookback은 유니버스마다 달라 — 그 부분은 약한 lookback 과적합이므로 canonical에서 고정 필요).

**정직한 한계(이게 "검증된 수익"이 아닌 이유):**
1. 대형 유동주 10종목 한정 — 유니버스가 좁아 저변동성 스프레드가 작다. 넓은 유니버스로 재현 필요.
2. 비(非)생존자통제 — 생존자 편향 영향이 모멘텀보다는 작지만(저변동 선택이라) 여전히 미통제.
3. canonical 평가 + 60영업일 paper 미통과 — live 게이트는 그대로 다 통과해야 한다.

재현: `tools/research_candidate_sweep.py --candidate-family low_volatility --oos-holdout-split 2025-01-01`.
구현은 `_target_weight_score_panel`의 `score_mode="low_volatility"` + `build_target_weight_low_volatility_candidate_specs`.

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

저변동성 후보가 나왔으니 우선순위가 바뀌었다:

1. **저변동성 후보 넓은 유니버스 재현**: 10종목이 아니라 canonical 유동성 유니버스(top-50~200)에서
   `--candidate-family low_volatility`를 돌려 초과수익이 유지되는지 확인(좁은 유니버스 특이성 배제).
2. **저변동성 후보 canonical 평가 → 60영업일 paper**: 재현되면 `evaluate_and_promote.py`로 canonical
   평가에 올리고, live 게이트 전제인 60영업일 execution-backed paper 증거를 쌓는다.
3. **생존자 통제 재측정**: pykrx KRX 과거 상장목록이 동작하는 환경에서 `--canonical`로
   `survivorship_controlled=true` 확인(저변동 선택이라 생존자 영향은 모멘텀보다 작지만 미통제).
4. **추가 직교 팩터 탐색**: 저변동성이 통했으니 quality/value(재무 데이터 복구 시), 단기 reversal 등
   모멘텀과 직교인 다른 축도 같은 OOS 방식으로 검증.

저변동성 후보가 넓은 유니버스에서도 양수 초과수익을 유지하기 전까지는 여전히 "유망한 연구 후보"다.
운영 경로는 `docs/PAPER_TO_LIVE_RUNBOOK.md` 참고.
