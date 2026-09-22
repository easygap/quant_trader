[한국어](../GETTING_STARTED.md) · **English** · [日本語](../ja/GETTING_STARTED.md) · [简体中文](../zh-CN/GETTING_STARTED.md)

# Set up Nungum and try paper trading

This guide uses **Windows PowerShell, Python 3.11 or 3.12, and Git**. It starts with a local dashboard, then walks through one trading cycle with virtual funds.

[Back to the README](../../README.en.md)

## Install

Open PowerShell and run:

```powershell
git clone https://github.com/easygap/quant_trader.git
cd quant_trader
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create your local configuration files. These commands leave any existing files in place.

```powershell
if (-not (Test-Path config/settings.yaml)) {
    Copy-Item config/settings.yaml.example config/settings.yaml
}
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

Open `config/settings.yaml` and check that `mode` under `trading` is set to `paper`. This is the example configuration's default. Paper orders are simulated inside Nungum with virtual funds; this is separate from the broker's own demo trading service.

For a KIS API connection, put your API credentials and account number in `.env`. Do not commit this file. Live trading has additional requirements described in the [live account guide (Korean)](../PAPER_TO_LIVE_RUNBOOK.md).

## Open the dashboard

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard
```

Leave this terminal running and open [http://127.0.0.1:8080](http://127.0.0.1:8080) on the same PC. This starts the dashboard, not the trading process.

A new installation has no saved trades. The screenshots show an existing paper account, so its balances and returns will not appear in your new account.

If port 8080 is in use, choose another port:

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard --dashboard-port 8081
```

Then open [http://127.0.0.1:8081](http://127.0.0.1:8081).

## Paper trading

Open a second PowerShell window and change to your `quant_trader` folder so the dashboard can keep running.

The two default portfolios are configured in [config/baskets.yaml](../../config/baskets.yaml):

| Configuration ID      | Dashboard label  | Default setup                                                                                                                                                                                    |
| --------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `kr_pocket`           | ETF 적립         | KODEX 200 (`069500`) and TIGER CD-rate ETF (`357870`); KRW 300,000 starting capital and a planned KRW 100,000 monthly contribution. Contributions must be recorded manually. Paper trading only. |
| `kr_diversified_hold` | 대형주 분산 투자 | Korean large-cap stocks with a target allocation of 60% stocks and 40% cash.                                                                                                                     |

Edit this file to change holdings and target weights. Whole-share prices and minimum order sizes can prevent the portfolio from reaching its exact target weights.

Preview the proposed orders first. This fetches prices over the internet but does not place orders.

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket --dry-run
```

Check the securities and quantities. **Confirm that `trading.mode` is still `paper`** before running:

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket
```

This runs one cycle. Refresh the dashboard afterward. If prices are unavailable or the order conditions are not met, there may be no trades; check the terminal output for the reason.

To run repeatedly, use the [scheduling instructions (Korean)](../PROJECT_GUIDE.md). Keep the PC and trading process running while you want automated trading to continue.

## Using the dashboard

The interface is currently in Korean. Amounts are in KRW; all dates and times use Korea Standard Time (UTC+9), regardless of your computer's time zone.

| On-screen label                     | Meaning / action                                                                                |
| ----------------------------------- | ----------------------------------------------------------------------------------------------- |
| **ETF 적립** / **대형주 분산 투자** | Switch between the ETF and stock portfolios.                                                    |
| **투자원금**                        | Initial capital plus recorded contributions.                                                    |
| **평가금액**                        | Portfolio value, including cash.                                                                |
| **수익률**                          | Time-weighted return, excluding the effect of deposits and withdrawals.                         |
| **입체** / **평면**                 | Switch between 3D and flat views of the same returns. Ribbon width is not an additional metric. |
| **최근 날짜**                       | Return to the latest saved date after using the date slider.                                    |
| **낙폭과 일별 기록 자세히 보기**    | Open the drawdown chart and daily records.                                                      |
| **이전 기록** / **다음 기록**       | Move between pages of up to 100 daily records.                                                  |
| **기록 내려받기**                   | Export all records in the selected period to CSV, not just the current page.                    |
| **자동매매 상태**                   | View the last run, trading halt status, broker connection, and data updates.                    |
| **다시 확인**                       | Refresh the status panel.                                                                       |

![Daily records and drawdown in the Korean dashboard](../images/history-table-20260922.png)

The holdings table uses purchase-cost weights. Market value and unrealized profit or loss are shown separately below it.

### Add virtual funds

Select **적립금 기록**, choose the account, and enter an amount. Select **내용 확인**, check the account, amount, and paper trading mode, then submit. The amount is added to paper capital. This does not transfer money from a bank account or set up a direct debit.

## Backtesting

Run the published strategy comparison with historical prices:

```powershell
.\.venv\Scripts\python.exe tools/risk_review.py --as-of 2026-09-22
```

The results are saved to `reports/research/risk_review_20260922.json` and the chart to `docs/images/risk-review-20260922.png`. Changing the date also changes the output filenames. This command does not place orders.

Comparisons stop at the latest date shared by all input series. In the September 22 review, the index data ended on September 17, so the comparison also ends on September 17. See the [report (Korean)](../RISK_REVIEW_20260922.md) for cost assumptions and limitations.

Paper and backtest results do not guarantee live returns. The CD-rate ETF is not principal-protected.

## Access from another device

The layout adapts to phone screens, but `127.0.0.1` only works on the computer running Nungum. Access from another device requires separate connection and authentication settings. The [configuration guide (Korean)](../PROJECT_GUIDE.md) covers these settings.

## Help

- [Live account setup and order limits (Korean)](../PAPER_TO_LIVE_RUNBOOK.md)
- [Why trading may be halted (Korean)](../SAFETY_MODEL.md)
- [Report a bug or request a feature](https://github.com/easygap/quant_trader/issues/new/choose). Remove account numbers, API keys, and passwords from any attachments.

[Back to the README](../../README.en.md)
