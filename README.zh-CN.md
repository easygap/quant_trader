[한국어](README.md) · [English](README.en.md) · [日本語](README.ja.md) · **简体中文**

<img src="monitoring/static/nungum-symbol.svg" alt="" width="40">

# Nungum · 눈금

**在自己的电脑上运行的韩国股票与 ETF 自动交易工具**

选好交易标的、设置目标仓位后，可以先用虚拟资金测试交易规则。通过浏览器就能查看账户收益、持仓和自动交易状态，也可以用历史行情进行回测。

[安装指南](docs/zh-CN/GETTING_STARTED.md) · [界面使用说明](docs/zh-CN/GETTING_STARTED.md#界面使用说明) · [回测](#回测) · [反馈问题或建议](https://github.com/easygap/quant_trader/issues/new/choose)

本页提供中文说明，**程序界面目前仅支持韩语**。金额以韩元（KRW）显示，日期和时间按韩国标准时间（UTC+9）计算。使用指南中保留了韩语按钮名称，方便对照操作。

## 账户与收益

![模拟账户的收益曲线、日期选择，以及立体和平面图表切换](docs/images/readme-walkthrough-20260922.gif)

本页所有截图均来自2026年9月22日的模拟交易账户。[查看静态截图](docs/images/readme-account-20260922.png)

投入本金、账户总资产和月度收益率集中显示，追加资金不会被计入投资收益。可以按日期和时间范围查看账户变化。

## 持仓

![韩国股票持仓数量、平均买入价、当前仓位与目标仓位](docs/images/readme-holdings-20260922.png)

持仓表列出各标的的数量、平均买入价，并对比当前仓位与目标仓位。这里的仓位占比按**买入成本**计算；按当前价格计算的持仓市值和浮动盈亏显示在表格下方。

## 历史记录与CSV导出

![每日账户资产、投入本金、收益率与回撤](docs/images/history-table-20260922.png)

每日记录可以直接查看，也可以导出为CSV，用Excel等工具继续整理。表格每页显示100条记录，导出文件包含所选时间范围内的全部记录。

## 交易设置

| 默认方案                                 | 交易方式                                                         |
| ---------------------------------------- | ---------------------------------------------------------------- |
| **ETF定投**（`ETF 적립`）                | 分配到KODEX 200和跟踪韩国CD利率的TIGER ETF。目前仅支持模拟交易。 |
| **韩国大盘股组合**（`대형주 분산 투자`） | 持有韩国大盘股和现金，仓位偏离目标比例达到一定程度时进行再平衡。 |

交易标的和目标比例都可以修改。[配置组合并运行模拟交易](docs/zh-CN/GETTING_STARTED.md#模拟交易)

下单前，程序会检查余额和交易限额。状态面板会显示最近一次运行时间，以及交易被暂停的原因。

<details>
<summary>自动交易状态截图</summary>

![最近运行时间、交易暂停状态与券商连接状态](docs/images/readme-operations-20260922.png)

</details>

<details>
<summary>手机端显示效果</summary>

<a href="docs/images/dashboard-mobile-20260922.png"><img src="docs/images/readme-mobile-20260922.png" alt="手机屏幕上的账户信息和收益图表" width="300"></a>

默认地址只能在运行程序的电脑上访问。手机访问需要[单独设置连接方式](docs/zh-CN/GETTING_STARTED.md#从其他设备访问)。

</details>

## 安装

需要安装 **Python 3.11或3.12，以及Git**。安装指南使用Windows PowerShell。

1. 按照[安装指南](docs/zh-CN/GETTING_STARTED.md)完成准备。
2. 在同一台电脑上打开[账户页面](http://127.0.0.1:8080)。
3. 先预览订单，再运行一次[模拟交易](docs/zh-CN/GETTING_STARTED.md#模拟交易)。

首次安装时还没有交易记录。

账户页面与交易程序需要分别启动。只打开页面不会开始交易；持续运行自动交易时，需要保持电脑和交易程序开启。实盘交易通过**韩国投资证券（Korea Investment & Securities）的KIS Open API**连接账户。[实盘接入指南](docs/PAPER_TO_LIVE_RUNBOOK.md)目前为韩语。

## 回测

将交易规则应用于历史行情，对比收益率、最大回撤和交易成本。结果报告同时说明了计算条件和适用范围。

[运行回测](docs/zh-CN/GETTING_STARTED.md#回测) · [结果对比（韩语）](docs/RISK_REVIEW_20260922.md) · [订单数量与交易成本（韩语）](docs/REBALANCE_REVIEW_20260922.md)

模拟交易和回测结果不代表未来实盘收益。CD利率ETF也不保本。

## 问题与建议

遇到问题或有功能建议，可以[提交GitHub Issue](https://github.com/easygap/quant_trader/issues/new/choose)。上传截图或日志前，请隐藏账号和API密钥。

<details>
<summary>配置文件与源代码</summary>

[代码结构与定时运行设置（韩语）](docs/PROJECT_GUIDE.md) · [交易暂停条件（韩语）](docs/SAFETY_MODEL.md)

使用Python、aiohttp、SQLite和原生HTML/CSS/JavaScript开发。

</details>
