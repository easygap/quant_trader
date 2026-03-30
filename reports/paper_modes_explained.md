# Paper Modes

## signal_only_paper (기본)
auto_entry=False. 신호만 기록. TradeHistory 미생성.

## full_paper_auto_entry
QUANT_AUTO_ENTRY=true 환경변수. BUY/SELL 자동 실행. TradeHistory 생성.

## 커맨드
- signal-only: python main.py --mode schedule --strategy scoring
- full paper: QUANT_AUTO_ENTRY=true python main.py --mode schedule --strategy scoring
