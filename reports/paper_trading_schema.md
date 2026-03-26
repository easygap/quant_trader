# Paper Trading Schema

## TradeHistory
signal_at, order_at, executed_at, expected_price, price_gap, strategy, symbol, reason, mode

## PortfolioSnapshot
total_value, cumulative_return, mdd, peak_value

## OperationEvent
event_type (SIGNAL/API_FAILURE/DUPLICATE_BLOCKED/BLACKSWAN/SL_TP/MDD_HALT/WARNING)
severity (info/warning/error/critical)
symbol, strategy, message, detail(JSON)
