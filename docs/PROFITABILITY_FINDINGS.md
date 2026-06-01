# 수익성 정직 점검 결과 (먼저 읽을 것)

> 목적: 이 시스템으로 "안정적 고수익"이 가능한지, 지금까지의 정직한 검증 결과를 한 곳에
> 모은다. 헤드라인 백테스트 수치만 보고 실거래에 들어가는 것을 막기 위한 문서다.
> 최종 갱신: 2026-06-01.

## 한 줄 결론 (2026-06-01 최종)

**넓은 canonical 유니버스에서, 종목 선택이든 비중 방식이든 단순 스킴으로 동일비중 buy&hold를
이기는 검증된 alpha는 없다.** 모멘텀 40여 변형, 저변동성 선택, risk-parity(선택 0·비중만) — 셋 다
공정한 넓은 유니버스(30종목)에선 음(-)의 초과수익이다. 좁게 고른 10~20종목에서 나온 좋은 수치
(+12~27%p)는 진짜 alpha가 아니라 **유니버스 선택 효과**였다(넓히니 −34~−57%p로 반증). 지금 상태로
실거래에 자본을 넣는 것은 권장하지 않는다.

> 정직성 메모: 이건 검증 도구가 제대로 작동한 사례다. 좁은 유니버스 결과(+12~27%p)에 흥분하지
> 않고 넓은 유니버스 재현 테스트를 돌렸더니 음(-)으로 뒤집혔다. 좁은 유니버스에서 EW 벤치마크
> Sharpe는 ~1.06이었는데 넓은 30종목에서는 1.24로 훨씬 높아져, 저변동 선택이 그걸 못 넘었다.
> 좁은 유니버스 성과는 "벤치마크가 약한 종목을 내가 골랐기 때문"이었다.

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

## 저변동성 팩터 시험 — 좁은 유니버스에선 통했지만 넓히니 사라짐 (2026-06-01)

모멘텀이 실패한 이유는 전부 "수익 추격(return-chasing)"이었기 때문이다. 저변동성 팩터는 그
베타/모멘텀 틸트와 **직교(orthogonal)** 한 유일한 축이라 따로 시험했다. 좁은 유니버스에서는
처음으로 벤치마크를 이겼지만(아래), **넓은 canonical 유니버스에서는 우위가 사라졌다(맨 아래).**
같은 10종목·같은 OOS holdout 방식, `--candidate-family low_volatility`:

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

**20종목 확장:** 위 10종목에 10개를 더해도 `RUN_CANONICAL_EVALUATION` 유지(`low_vol_top5_60d_equal`
초과 +27.2%p). 여기까지는 유망해 보였다.

**그런데 canonical 30종목(섹터 다양·시총순)으로 넓히니 뒤집혔다 — 결정적 반증:**
`--top-n 50`에서 FDR로 깨끗하게 받아지는 30종목(에코프로비엠·포스코·한화에어로·KB금융·삼성바이오·
엔씨 등 다양 섹터)으로 재실행한 결과 **판정 NO_ALPHA_CANDIDATE**. 5개 후보 전부 음(-)의 초과수익:

  | 후보 | 절대수익 | 초과수익 | 초과 Sharpe |
  |------|---------|---------|------------|
  | low_vol_top5_60d_invvol | +89.6% | **−34.3%p** | −0.14 |
  | low_vol_top5_60d_equal | +87.0% | **−36.8%p** | −0.16 |
  | low_vol_top5_120d_invvol | +75.1% | **−48.7%p** | −0.25 |

원인은 명확하다: 좁은 유니버스의 EW 벤치마크 Sharpe는 ~1.06이었는데 **넓은 30종목에서는 1.24**로
훨씬 높아졌다. 즉 좁은 유니버스 성과는 "내가 고른 종목들의 EW 벤치마크가 약했기 때문"이었고,
공정한 넓은 유니버스에서는 저변동 선택이 그 벤치마크를 못 넘는다. **저변동성도 진짜 alpha가
아니라 유니버스 선택 효과였다.**

**결론:** 좁은 유니버스 결과(+12~27%p)는 검증된 엣지가 아니다. 검증 도구(넓은 유니버스 재현)가
이 거짓 양성(false positive)을 정확히 잡아냈다는 점이 이 시험의 진짜 성과다.

**가장 엄격한 테스트 — risk-parity(선택 베팅 0):** "혹시 비중 방식만으로 이길 수 있나?"를 확인하려고
전체 30종목을 다 보유하되 비중만 동일 → 역변동성으로 바꿨다(`risk_parity_holdall_*`, top_n=999).
선택 효과가 0이라 유니버스 선택 artifact가 원천 불가능한 가장 깨끗한 테스트다. 결과도 음(-):
`risk_parity_holdall_120d_invvol` ret +84.8%, **Sharpe 1.11 < EW B&H 1.24**, 초과 −39%p, 초과 Sharpe −0.13.
즉 **순수 역변동성 비중도 동일비중을 못 이긴다(수익·Sharpe 둘 다).** 이유: 2023~25 KOSPI 대형주는
고변동 종목(반도체·2차전지)이 곧 승자였어서, 역변동성으로 그걸 down-weight하면 변동성 감소보다
수익 손실이 더 컸다. **선택이든 비중이든, 단순 스킴으로 EW B&H를 이기는 길은 이 유니버스엔 없다.**

**정직한 한계(왜 아직 "검증된 수익"이 없는가):**
1. 좁은 유니버스 성과는 벤치마크 선택 효과 — 넓은 30종목에서 음(-)으로 반증됨.
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

저변동성도 넓은 유니버스에서 반증됐으니, 손쉬운 alpha는 없다는 게 현재까지의 정직한 결론이다.
남은 정직한 경로:

1. **벤치마크 정의 재검토**: 지금 벤치마크는 "같은 유니버스 동일비중 B&H"라 매우 강하다(넓을수록
   Sharpe↑). 운영 목표가 절대수익/낙폭 관리라면 KOSPI 지수 대비 또는 위험조정 기준으로 게이트를
   바꾸는 것도 한 방법(단, 이건 게이트를 느슨하게 하는 것이므로 신중히).
2. **다요인 결합**: 단일 팩터(모멘텀/저변동)는 EW B&H를 못 이긴다. 저변동 선택 + 모멘텀 필터 +
   quality 같은 다요인 결합을 같은 OOS 방식으로 시험(재무 데이터 복구 필요).
3. **생존자 통제 재측정**: pykrx 과거 상장목록 동작 환경에서 `--canonical` 재실행. 단 이건 보통
   수치를 더 낮추는 방향이라, alpha를 만들어내지는 못한다.
4. **현실 직시**: 대형 유동주에서 EW B&H를 꾸준히 이기는 건 어렵다는 게 데이터의 일관된 메시지다.
   "안정적 고수익"보다 "벤치마크 추종 + 낙폭 관리"가 더 현실적 목표일 수 있다.

**가장 중요한 운영 원칙:** 어떤 후보든 **넓은 canonical 유니버스 + OOS holdout + deflated Sharpe**를
모두 통과하기 전엔 paper/live로 올리지 않는다. 좁은 유니버스 좋은 수치는 이번처럼 거짓 양성일 수
있다. 운영 경로는 `docs/PAPER_TO_LIVE_RUNBOOK.md` 참고.
