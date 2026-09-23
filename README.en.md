[한국어](README.md) · **English** · [日本語](README.ja.md) · [中文](README.zh-CN.md)

# <img src="monitoring/static/nungum-symbol.svg" alt="" width="32"> Nungum · 눈금

**Automated trading for Korean stocks and ETFs, on your own PC.**

Choose your holdings and target weights, try them with virtual funds, and track your portfolio in a browser. Nungum includes paper trading, historical backtests, and a connection to Korea Investment & Securities through the KIS Open API.

[Get started](docs/en/GETTING_STARTED.md) · [Use the dashboard](docs/en/GETTING_STARTED.md#using-the-dashboard) · [Backtesting](#backtesting) · [Report an issue](https://github.com/easygap/quant_trader/issues/new/choose)

This guide is in English. **The dashboard is currently in Korean**, with amounts in Korean won (KRW) and dates in Korea Standard Time (UTC+9). The guide includes the Korean button labels to help you find your way around.

## Account overview

![Paper portfolio: performance chart, date selection, and 3D and flat views](docs/images/readme-walkthrough-20260922.gif)

All screenshots on this page show a paper trading account captured on September 22, 2026. [View the still image](docs/images/readme-account-20260922.png)

See your contributions, portfolio value, and monthly returns together. Deposits do not count as investment gains. Select a date or period to review the account's performance over time.

## Holdings

![Korean stock holdings with share counts, average purchase prices, and current and target weights](docs/images/readme-holdings-20260922.png)

See share counts and average purchase prices, with current and target weights side by side. Weights in this table are based on **purchase cost**; market value and unrealized profit or loss appear below it.

## History and CSV export

![Daily portfolio value, contributions, returns, and drawdown](docs/images/history-table-20260922.png)

Review daily records in the dashboard or export them to CSV for a spreadsheet. The table shows 100 records per page; the export includes every record in the selected period.

## Trading presets

| Preset                                           | What it does                                                                                             |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| **ETF contributions** (`ETF 적립`)               | Allocates funds between KODEX 200 and the TIGER CD-rate ETF. Currently available for paper trading only. |
| **Korean large-cap stocks** (`대형주 분산 투자`) | Holds a basket of Korean stocks and cash, rebalancing when weights drift far enough from their targets.  |

You can change the securities and target weights. [Configure a portfolio](docs/en/GETTING_STARTED.md#paper-trading)

The program checks balances and trading limits before placing orders. The status panel shows the last run and any reason trading has been halted.

<details>
<summary>Trading status screenshot</summary>

![Last run, trading halt status, and broker connection status](docs/images/readme-operations-20260922.png)

</details>

<details>
<summary>Mobile layout</summary>

<a href="docs/images/dashboard-mobile-20260922.png"><img src="docs/images/readme-mobile-20260922.png" alt="Account overview and performance chart on a narrow screen" width="300"></a>

The default address works only on the PC running Nungum. Access from a phone requires a separate connection setup; see the [guide](docs/en/GETTING_STARTED.md#access-from-another-device).

</details>

## Installation

You need **Python 3.11 or 3.12 and Git**. The setup guide uses Windows PowerShell.

1. Follow the [installation guide](docs/en/GETTING_STARTED.md).
2. Open the [dashboard](http://127.0.0.1:8080) on the same PC.
3. Preview your orders, then run a [paper trading cycle](docs/en/GETTING_STARTED.md#paper-trading).

A fresh installation starts with no trading history.

The dashboard and trading process run separately. Opening the dashboard does not start trading; ongoing trading requires the PC and trading process to stay running. Live trading uses **Korea Investment & Securities' KIS Open API**. The [live account setup guide](docs/PAPER_TO_LIVE_RUNBOOK.md) is currently in Korean.

## Backtesting

Test trading rules against historical prices and compare returns, drawdowns, and trading costs. The published reports include their assumptions and limitations.

[Run a backtest](docs/en/GETTING_STARTED.md#backtesting) · [Results (Korean)](docs/RISK_REVIEW_20260922.md) · [Order sizing and costs (Korean)](docs/REBALANCE_REVIEW_20260922.md)

Paper trading and backtest results do not guarantee live returns. The CD-rate ETF can also lose principal.

## Questions and feedback

[Open a GitHub issue](https://github.com/easygap/quant_trader/issues/new/choose) for bugs or feature requests. Remove account numbers and API keys from screenshots and logs before posting.

<details>
<summary>Configuration and source code</summary>

[Project structure and scheduling (Korean)](docs/PROJECT_GUIDE.md) · [Trading halt rules (Korean)](docs/SAFETY_MODEL.md)

Built with Python, aiohttp, SQLite, and plain HTML/CSS/JavaScript.

</details>
