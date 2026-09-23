[한국어](../GETTING_STARTED.md) · [English](../en/GETTING_STARTED.md) · [日本語](../ja/GETTING_STARTED.md) · **中文**

# 安装与模拟交易

本指南使用 **Windows PowerShell、Python 3.11或3.12，以及Git**。先启动账户页面，再用虚拟资金运行一次交易。

[返回README](../../README.zh-CN.md)

## 安装

打开PowerShell，运行：

```powershell
git clone https://github.com/easygap/quant_trader.git
cd quant_trader
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

创建本地配置文件。如果文件已经存在，以下命令会保留原文件。

```powershell
if (-not (Test-Path config/settings.yaml)) {
    Copy-Item config/settings.yaml.example config/settings.yaml
}
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

打开`config/settings.yaml`，确认`trading`下的`mode`设为`paper`。示例配置默认使用这个值。模拟订单由Nungum在程序内部用虚拟资金处理，与券商提供的模拟交易服务不同。

连接KIS API时，需要在`.env`中填写API密钥和账号。不要将该文件提交到Git。实盘交易还有额外的接入要求，请参阅[实盘账户指南（韩语）](../PAPER_TO_LIVE_RUNBOOK.md)。

## 打开账户页面

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard
```

保持这个PowerShell窗口运行，在同一台电脑的浏览器中打开[http://127.0.0.1:8080](http://127.0.0.1:8080)。这条命令只启动账户页面，不会启动自动交易。

首次安装时没有历史交易。README中的截图来自已有的模拟账户，新账户不会直接显示截图中的余额和收益率。

如果8080端口已被占用，可以换一个端口：

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard --dashboard-port 8081
```

然后访问[http://127.0.0.1:8081](http://127.0.0.1:8081)。

## 模拟交易

保留账户页面的PowerShell窗口，再开一个窗口，进入安装程序的`quant_trader`文件夹。

[config/baskets.yaml](../../config/baskets.yaml)中提供了两套默认配置：

| 配置ID                | 界面名称         | 默认设置                                                                                                                               |
| --------------------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `kr_pocket`           | ETF 적립         | KODEX 200（`069500`）和TIGER CD利率ETF（`357870`）；初始资金30万韩元，计划每月追加10万韩元。追加资金需要手动记录。目前仅支持模拟交易。 |
| `kr_diversified_hold` | 대형주 분산 투자 | 韩国大盘股组合，目标为60%股票、40%现金。                                                                                               |

交易标的和目标比例都在这个文件中修改。由于按整股交易，且存在最低下单金额，实际仓位可能无法精确达到目标比例。

先预览准备下达的订单。这一步需要联网获取行情，但不会下单。

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket --dry-run
```

核对标的和数量，并**确认`trading.mode`仍为`paper`**后，运行：

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket
```

这会执行一次交易流程，结束后刷新账户页面即可。行情获取失败或不满足下单条件时，可能不会产生交易，原因可在PowerShell输出中查看。

如需定期运行，请参阅[定时运行设置（韩语）](../PROJECT_GUIDE.md)。自动交易期间需要保持电脑和交易程序开启。

## 界面使用说明

界面目前为韩语。金额以韩元（KRW）显示，日期和时间统一按韩国标准时间（UTC+9）计算，不受电脑所在时区影响。

| 界面文字                            | 含义或操作                                                             |
| ----------------------------------- | ---------------------------------------------------------------------- |
| **ETF 적립** / **대형주 분산 투자** | 切换ETF账户和股票账户。                                                |
| **투자원금**                        | 初始资金加上后续记录的追加资金。                                       |
| **평가금액**                        | 包含现金在内的账户总资产。                                             |
| **수익률**                          | 剔除资金存取影响后的时间加权收益率。                                   |
| **입체** / **평면**                 | 在立体和平面图表之间切换，数据相同。立体图中的带状宽度不代表额外指标。 |
| **최근 날짜**                       | 移动日期滑块后，返回最新记录。                                         |
| **낙폭과 일별 기록 자세히 보기**    | 展开回撤曲线和每日记录。                                               |
| **이전 기록** / **다음 기록**       | 查看上一页或下一页记录，每页最多100条。                                |
| **기록 내려받기**                   | 导出所选时间范围内的全部CSV记录，不限于当前页。                        |
| **자동매매 상태**                   | 查看最近运行时间、交易暂停状态、券商连接和数据更新情况。               |
| **다시 확인**                       | 刷新状态面板。                                                         |

![韩语界面中的每日账户记录和回撤曲线](../images/history-table-20260922.png)

持仓表中的占比按买入成本计算。按当前价格计算的持仓市值和浮动盈亏显示在表格下方。

### 追加虚拟资金

点击 **적립금 기록**，选择账户并输入金额。再点击 **내용 확인**，核对账户、金额及模拟交易模式后提交。金额会计入模拟账户本金。这项功能不会从银行转账，也不会设置自动扣款。

## 回测

使用历史行情运行已公开的策略对比：

```powershell
.\.venv\Scripts\python.exe tools/risk_review.py --as-of 2026-09-22
```

结果保存到`reports/research/risk_review_20260922.json`，图表保存到`docs/images/risk-review-20260922.png`。修改日期后，输出文件名也会相应变化。这条命令不会下单。

如果各组数据的截止日期不同，回测只计算到所有数据都有记录的最后一天。9月22日检查时，指数数据只到9月17日，因此对比结果也截至9月17日。交易成本等计算条件和局限见[结果报告（韩语）](../RISK_REVIEW_20260922.md)。

模拟交易和回测结果不代表未来实盘收益，CD利率ETF也不保本。

## 从其他设备访问

页面可以适应手机屏幕，但`127.0.0.1`只对运行程序的电脑有效。从其他设备访问时，需要单独配置连接地址和身份验证。相关设置见[配置指南（韩语）](../PROJECT_GUIDE.md)。

## 获取帮助

- [实盘接入和下单限制（韩语）](../PAPER_TO_LIVE_RUNBOOK.md)
- [交易暂停时的检查方法（韩语）](../SAFETY_MODEL.md)
- [反馈问题或功能建议](https://github.com/easygap/quant_trader/issues/new/choose)。上传附件前，请隐藏账号、API密钥和密码。

[返回README](../../README.zh-CN.md)
