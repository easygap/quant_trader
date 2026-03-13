# QUANT TRADER

Python으로 만든 자동 주식 매매 쪽 실험 프로젝트입니다.  
데이터랑 규칙 기반으로 신호를 만들고, KIS(한국투자증권) API로 국내주식 시세·주문을 다루는 구조입니다.

---

## 주요 구성요소

- **기술적 지표**: RSI, MACD, 볼린저 밴드, 이동평균, 스토캐스틱, ADX, ATR, OBV 등
- **전략**: 멀티 지표 스코어링, 평균 회귀, 추세 추종
- **리스크**: 1% 룰, 손절/익절, 트레일링 스탑, MDD 제한
- **백테스트**: 과거 데이터로 전략 검증 (수수료·세금·슬리피지 반영)
- **알림·실시간**: 디스코드 웹훅, KIS 웹소켓 체결가

Python 3.14+ 기준이고, 일부 라이브러리는 3.14 미지원이라 자체 계산 로직을 넣어둔 부분이 있습니다.

---

## 설치

```bash
python -m pip install -r requirements.txt
```

---

## 설정

`config/` 폴더 안 YAML에서 기본 설정을 하고, 민감한 값은 환경 변수로 두는 방식을 쓰면 됩니다.  
자세한 건 `config/settings.yaml`이랑 프로젝트에 포함된 설정 예시를 참고하면 됩니다.

---

## 실행 예시

```bash
# 백테스트만
python main.py --mode backtest --strategy scoring --symbol 005930

# 모의 매매
python main.py --mode paper --strategy scoring

# 실전 (실제 주문 나감 — 각자 책임 하에)
python main.py --mode live --strategy scoring
```

---

## 폴더 구조

```
config/       설정 YAML
core/         데이터 수집, 지표, 신호, 리스크, 주문
strategies/   전략 구현
api/          KIS API·웹소켓
backtest/     백테스트·리포트
monitoring/   로그, 디스코드, 대시보드
database/     DB
main.py       진입점
```

---

⚠ 실전에 쓰기 전에 모의로 충분히 돌려보시고, 투자 손실은 전적으로 사용자 책임입니다.
