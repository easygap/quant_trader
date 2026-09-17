#!/usr/bin/env python3
"""리스크 오버레이 정직 백테스트 — 추세 필터·변동성 목표·낙폭 제어가 손실을 줄이는가.

이 저장소의 확정 결론(docs/PROFITABILITY_FINDINGS.md)은 "종목 선택 알파는 없고, 베타를 싸게
담는 것이 답"이다. 그래서 여기서는 종목을 고르지 않는다. 대신 이미 운용 중인 두 트랙의
**주식 비중을 언제 줄이고 언제 되돌리는가**만 바꿔 보고, 같은 비용·같은 적립 규칙 아래에서
낙폭(MDD)·손실 연도·샤프가 어떻게 달라지는지 잰다.

트랙 1 (kr_pocket 형) : 지수 ETF + 금리 파킹, 월 10만원 적립, 목표 주식 비중 50%
트랙 2 (kr_diversified_hold 형) : 대형주 10종목 동일비중, 목표 주식 비중 60%

정책
  static      : 목표 비중 고정 (현재 운용과 동일)
  trend       : 지수가 200일선 아래면 주식 비중을 off_scale 배로 축소 (Faber 10개월 SMA 계열)
  vol         : 실현 변동성이 목표(연 15%)를 넘으면 목표/실현 비율만큼 축소 (하한 0.5, 상한 1.0)
  trend+vol   : 두 배수의 곱
  dd          : 시간가중 NAV가 고점 대비 -10% 아래면 0.5배, -5% 안으로 회복하면 복귀

비용: ETF 편도 수수료 0.015% + 슬리피지 0.05%, 증권거래세 없음(국내 주식형 ETF 비과세).
      개별 주식은 매도 시 0.20% 세금 추가. 파킹 사채·현금은 연 3% 일할 가정.
데이터: FinanceDataReader. KS200 지수는 2002년부터(배당 제외 가격지수 — 보수적),
      069500은 2014년부터. 결과는 reports/research/ 와 docs/images/ 에 남긴다.

Usage:
    python tools/risk_overlay_backtest.py                # 전체
    python tools/risk_overlay_backtest.py --track pocket # 트랙 1만
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

BASKET_SYMBOLS = ["005930", "000660", "035420", "005380", "051910",
                  "005490", "055550", "035720", "012330", "105560"]
COMMISSION = 0.00015
SLIPPAGE = 0.0005
STOCK_TAX = 0.0020
RF_ANNUAL = 0.03
TRADING_DAYS = 252


@dataclass
class Policy:
    name: str
    label: str
    trend: bool = False
    trend_ma_days: int = 200
    trend_off_scale: float = 0.5
    trend_band: float = 0.02          # 200일선 ±2% 히스테리시스 — 선 근처 왕복 매매를 막는다
    vol: bool = False
    vol_target: float = 0.15
    vol_lookback: int = 60
    vol_min_scale: float = 0.5
    vol_max_scale: float = 1.0
    vol_step: float = 0.1             # 배수를 0.1 단위로 양자화 — 매일 미세 조정하지 않는다
    dd: bool = False
    dd_trigger: float = -0.10
    dd_release: float = -0.05
    dd_scale: float = 0.5
    notes: list[str] = field(default_factory=list)


POLICIES = [
    Policy("static", "고정 비중(현행)"),
    Policy("trend50", "추세 필터 · 200일선 아래면 절반", trend=True, trend_off_scale=0.5),
    Policy("trend0", "추세 필터 · 200일선 아래면 0", trend=True, trend_off_scale=0.0),
    Policy("vol15", "변동성 목표 15%", vol=True),
    Policy("vol20", "변동성 목표 20%", vol=True, vol_target=0.20),
    Policy("dd10", "낙폭 제어 · -10%에서 절반", dd=True),
    Policy("trend50_dd10", "추세 절반 + 낙폭 제어", trend=True, trend_off_scale=0.5, dd=True),
    Policy("trend50_vol20", "추세 절반 + 변동성 20%", trend=True, trend_off_scale=0.5, vol=True, vol_target=0.20),
]


def _fdr(symbol: str, start: str):
    import FinanceDataReader as fdr
    df = fdr.DataReader(symbol, start)
    df = df.rename(columns={c: c.lower() for c in df.columns})
    df.index = df.index.tz_localize(None) if getattr(df.index, "tz", None) is not None else df.index
    return df["close"].astype(float).dropna()


def _scale_series(closes, nav_twr, policy: Policy, index_closes=None):
    """날짜별 주식 비중 배수(0~1). 모든 계산은 전일까지의 정보만 쓴다(shift 1)."""
    import numpy as np
    import pandas as pd

    scale = pd.Series(1.0, index=closes.index)
    if policy.trend:
        ref = index_closes if index_closes is not None else closes
        ma = ref.rolling(policy.trend_ma_days).mean()
        rel = (ref / ma - 1).reindex(closes.index).ffill().shift(1)
        # 히스테리시스: 선 아래로 band만큼 내려가야 '하락', 위로 band만큼 올라와야 '상승'.
        below = []
        state = False
        for v in rel.to_numpy():
            if v != v:  # NaN
                below.append(False)
                continue
            if state and v > policy.trend_band:
                state = False
            elif (not state) and v < -policy.trend_band:
                state = True
            below.append(state)
        below = pd.Series(below, index=closes.index)
        scale = scale.where(~below, policy.trend_off_scale)
    if policy.vol:
        rets = closes.pct_change()
        realized = rets.rolling(policy.vol_lookback).std() * math.sqrt(TRADING_DAYS)
        ratio = (policy.vol_target / realized).shift(1)
        vol_scale = ratio.clip(lower=policy.vol_min_scale, upper=policy.vol_max_scale).fillna(1.0)
        if policy.vol_step > 0:
            vol_scale = (vol_scale / policy.vol_step).round() * policy.vol_step
        scale = scale * vol_scale
    return scale.clip(0.0, 1.0)


def simulate(closes, policy: Policy, *, index_closes=None, target_stock=0.5,
             initial=300_000.0, monthly=100_000.0, tax_rate=0.0,
             drift_band=0.08, rf_annual=RF_ANNUAL):
    """단일 위험자산 + 현금성 자산의 DCA 포트폴리오. 위험자산은 소수 주 허용(연구용).

    - 매월 첫 거래일에 monthly 적립 → 현금으로 들어와 다음 리밸런싱에서 배분.
    - 리밸런싱: 목표 비중(= target_stock × 정책 배수)과 실제 비중의 차이가 drift_band를
      넘거나, 정책 배수가 바뀐 날 실행. 체결은 다음날 시가가 없으므로 당일 종가에
      슬리피지를 얹어 보수적으로 근사한다.
    - 시간가중수익률(TWR)로 성과를 재고, 적립은 수익에서 분리한다.
    """
    import numpy as np
    import pandas as pd

    rf_daily = (1 + rf_annual) ** (1 / TRADING_DAYS) - 1
    dates = closes.index
    scale = _scale_series(closes, None, policy, index_closes)

    shares = 0.0
    cash = initial
    peak_index = 1.0
    twr_index = 1.0
    dd_active = False
    prev_scale = None
    prev_month = None
    contributed = initial
    turnover_value = 0.0
    exposure_sum = 0.0
    rows = []
    prev_total_after_flow = None

    for i, day in enumerate(dates):
        price = float(closes.iloc[i])
        # 1) 적립 (월 첫 거래일)
        flow = 0.0
        if prev_month is not None and day.month != prev_month and monthly > 0:
            cash += monthly
            contributed += monthly
            flow = monthly
        prev_month = day.month
        # 2) 현금 이자
        cash *= (1 + rf_daily)
        total_before = shares * price + cash
        # 3) 낙폭 제어 상태 (전일까지의 TWR 기준)
        s = float(scale.iloc[i])
        if policy.dd:
            dd_now = twr_index / peak_index - 1
            if dd_active and dd_now >= policy.dd_release:
                dd_active = False
            elif not dd_active and dd_now <= policy.dd_trigger:
                dd_active = True
            if dd_active:
                s *= policy.dd_scale
        target_w = target_stock * s
        cur_w = shares * price / total_before if total_before > 0 else 0.0
        need = (prev_scale is None) or (abs(cur_w - target_w) > drift_band) or (abs(s - (prev_scale or 0)) > 1e-9 and abs(cur_w - target_w) > 0.005)
        if flow > 0 and cur_w < target_w - 0.005:
            need = True
        if need:
            target_value = total_before * target_w
            diff_value = target_value - shares * price
            if abs(diff_value) > 1_000:
                fill = price * (1 + SLIPPAGE) if diff_value > 0 else price * (1 - SLIPPAGE)
                qty = diff_value / fill
                cost = abs(diff_value) * COMMISSION + (abs(diff_value) * tax_rate if diff_value < 0 else 0.0)
                cash -= qty * fill + cost
                shares += qty
                turnover_value += abs(diff_value)
        prev_scale = s
        total = shares * price + cash
        # 4) TWR: 적립 직후 가치 대비 당일 종가 가치
        if prev_total_after_flow is not None and prev_total_after_flow > 0:
            twr_index *= (total / (prev_total_after_flow))
        peak_index = max(peak_index, twr_index)
        prev_total_after_flow = total
        # 다음 날 적립이 들어오면 분모를 적립 포함으로 갱신해야 하므로 flow는 다음 루프의 total_before에 반영됨
        exposure_sum += (shares * price / total) if total > 0 else 0.0
        rows.append({"date": day, "total": total, "twr": twr_index, "contributed": contributed,
                     "stock_w": shares * price / total if total > 0 else 0.0, "scale": s})
        # 적립이 들어오는 날 TWR 분모 보정: 다음 루프에서 total_before에 flow가 포함되므로
        # prev_total_after_flow에 flow를 더해 둔다 (적립은 수익이 아니다).
    frame = pd.DataFrame(rows).set_index("date")
    # TWR 재계산(적립을 분모에 더하는 방식으로 정확히): 일별 수익률 = total_t / (total_{t-1} + flow_t)
    flows = frame["contributed"].diff().fillna(0.0)
    daily = frame["total"] / (frame["total"].shift(1) + flows) - 1
    daily.iloc[0] = 0.0
    twr = (1 + daily).cumprod()
    frame["twr"] = twr
    frame["drawdown"] = twr / twr.cummax() - 1
    return frame, {"turnover_value": turnover_value, "avg_exposure": exposure_sum / len(frame)}


def metrics(frame, extra, years_hint=None):
    import numpy as np
    twr = frame["twr"]
    daily = twr.pct_change().dropna()
    n_years = (frame.index[-1] - frame.index[0]).days / 365.25
    cagr = twr.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else 0.0
    vol = daily.std() * math.sqrt(TRADING_DAYS)
    sharpe = (daily.mean() * TRADING_DAYS - RF_ANNUAL) / vol if vol > 0 else 0.0
    mdd = frame["drawdown"].min()
    calmar = cagr / abs(mdd) if mdd < 0 else float("nan")
    yearly = twr.resample("YE").last().pct_change()
    first_year = twr.resample("YE").last().iloc[0] / twr.iloc[0] - 1
    yearly.iloc[0] = first_year
    monthly = twr.resample("ME").last().pct_change().dropna()
    avg_total = frame["total"].mean()
    turnover_per_year = extra["turnover_value"] / max(avg_total, 1) / n_years if n_years > 0 else 0.0
    final = frame["total"].iloc[-1]
    contributed = frame["contributed"].iloc[-1]
    return {
        "years": round(n_years, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "mdd_pct": round(float(mdd) * 100, 2),
        "calmar": round(float(calmar), 2) if calmar == calmar else None,
        "worst_year_pct": round(float(yearly.min()) * 100, 2),
        "losing_years": int((yearly < 0).sum()),
        "total_years": int(len(yearly)),
        "negative_months_pct": round(float((monthly < 0).mean()) * 100, 1),
        "avg_exposure_pct": round(extra["avg_exposure"] * 100, 1),
        "turnover_per_year_pct": round(turnover_per_year * 100, 1),
        "final_value": round(float(final)),
        "contributed": round(float(contributed)),
        "profit": round(float(final - contributed)),
        "yearly": {str(k.year): round(float(v) * 100, 2) for k, v in yearly.items()},
    }


def run_pocket(start="2002-01-01", use_etf=False):
    """트랙 1: 지수 위험자산(KS200 지수 또는 069500) + 현금성(연 3%)."""
    symbol = "069500" if use_etf else "KS200"
    closes = _fdr(symbol, start)
    index = _fdr("KS200", start)
    results = {}
    frames = {}
    for policy in POLICIES:
        frame, extra = simulate(closes, policy, index_closes=index, target_stock=0.5,
                                initial=300_000, monthly=100_000, tax_rate=0.0)
        results[policy.name] = {"label": policy.label, **metrics(frame, extra)}
        frames[policy.name] = frame
    return {"symbol": symbol, "start": str(closes.index[0].date()), "end": str(closes.index[-1].date()),
            "results": results}, frames


def run_basket(start="2021-12-01"):
    """트랙 2: 대형주 10종목 동일비중을 하나의 위험자산(EW 지수)으로 묶고, 주식 비중 60%."""
    import pandas as pd
    panel = pd.DataFrame({s: _fdr(s, start) for s in BASKET_SYMBOLS}).dropna(how="any")
    ew = (panel / panel.iloc[0]).mean(axis=1)          # 동일비중 지수 (일별 리밸런싱 근사 없음: 첫날 동일비중 보유)
    # 첫날 동일비중 매수 후 보유한 가치가 정확히 위 식이다 (수량 고정, 가격만 변동).
    index = _fdr("KS200", start)
    results = {}
    frames = {}
    for policy in POLICIES:
        frame, extra = simulate(ew, policy, index_closes=index, target_stock=0.6,
                                initial=10_000_000, monthly=0.0, tax_rate=STOCK_TAX)
        results[policy.name] = {"label": policy.label, **metrics(frame, extra)}
        frames[policy.name] = frame
    return {"symbol": "EW10", "symbols": BASKET_SYMBOLS, "start": str(ew.index[0].date()),
            "end": str(ew.index[-1].date()), "results": results}, frames


def _plot(track_name, summary, frames, out_png, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for name in ["Malgun Gothic", "NanumGothic", "Noto Sans KR", "Apple SD Gothic Neo"]:
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False
    paper, ink, faint, up, down = "#f2f0ec", "#0d0c0b", "#8c8983", "#c33d2b", "#2557c9"
    order = ["static", "trend50", "dd10", "trend50_dd10", "vol20", "trend0"]
    colors = {"static": faint, "trend50": ink, "dd10": up, "trend50_dd10": down, "vol20": "#b06d12", "trend0": "#6b5d9e"}
    fig, axes = plt.subplots(2, 1, figsize=(11, 7.2), sharex=True, gridspec_kw={"height_ratios": [2.4, 1]})
    fig.patch.set_facecolor(paper)
    for ax in axes:
        ax.set_facecolor(paper)
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)
        ax.grid(axis="y", color=ink, alpha=0.08, linewidth=0.8)
        ax.tick_params(colors=ink, labelsize=9)
    for name in order:
        if name not in frames:
            continue
        f = frames[name]
        lw = 1.6 if name in ("static", "trend50", "dd10", "trend50_dd10") else 1.0
        axes[0].plot(f.index, f["twr"] * 100, color=colors[name], linewidth=lw, label=f"{summary['results'][name]['label']} · 연 {summary['results'][name]['cagr_pct']:.1f}% · 낙폭 {summary['results'][name]['mdd_pct']:.0f}%")
        axes[1].fill_between(f.index, f["drawdown"] * 100, 0, color=colors[name], alpha=0.12 if name != "static" else 0.25, linewidth=0)
        axes[1].plot(f.index, f["drawdown"] * 100, color=colors[name], linewidth=0.9)
    axes[0].set_title(title, loc="left", fontsize=13, color=ink, pad=12)
    axes[0].set_ylabel("시간가중 지수 (시작=100)", fontsize=9, color=ink)
    axes[0].legend(loc="upper left", fontsize=8.5, frameon=False)
    axes[1].set_ylabel("고점 대비 낙폭 (%)", fontsize=9, color=ink)
    fig.text(0.01, 0.01, "비용 반영(수수료·슬리피지·주식 거래세) · 적립은 시간가중으로 분리 · 모든 신호는 전일 정보만 사용", fontsize=8, color=faint)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=150, facecolor=paper)
    plt.close(fig)


def _markdown(track_title, summary):
    lines = [f"### {track_title}", "", f"- 데이터: {summary['symbol']} {summary['start']} → {summary['end']}", "",
             "| 정책 | 연수익률(CAGR) | 최대낙폭 | 샤프 | 칼마 | 최악 연도 | 손실 연도 | 평균 주식 비중 | 연 회전율 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, r in summary["results"].items():
        lines.append(f"| {r['label']} | {r['cagr_pct']:+.2f}% | {r['mdd_pct']:.1f}% | {r['sharpe']:.2f} | {r['calmar'] if r['calmar'] is not None else '—'} | {r['worst_year_pct']:+.1f}% | {r['losing_years']}/{r['total_years']} | {r['avg_exposure_pct']:.0f}% | {r['turnover_per_year_pct']:.0f}% |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--track", choices=["all", "pocket", "pocket_etf", "basket"], default="all")
    parser.add_argument("--out-dir", default=str(_ROOT / "reports" / "research"))
    parser.add_argument("--image-dir", default=str(_ROOT / "docs" / "images"))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    image_dir = Path(args.image_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = {}
    md = ["# 리스크 오버레이 백테스트 결과", ""]
    if args.track in ("all", "pocket"):
        summary, frames = run_pocket("2002-01-01", use_etf=False)
        report["pocket_ks200"] = summary
        _plot("pocket", summary, frames, image_dir / "overlay-pocket-ks200.png",
              "적립 트랙 · 코스피200 지수 50% + 현금성 50% · 2002년부터 월 10만원 적립")
        md += [_markdown("적립 트랙 (KS200 지수, 2002~)", summary), ""]
    if args.track in ("all", "pocket_etf"):
        summary, frames = run_pocket("2014-01-01", use_etf=True)
        report["pocket_etf"] = summary
        _plot("pocket_etf", summary, frames, image_dir / "overlay-pocket-etf.png",
              "적립 트랙 · KODEX 200 실제 ETF 50% + 현금성 50% · 2014년부터 월 10만원 적립")
        md += [_markdown("적립 트랙 (KODEX 200 ETF, 2014~)", summary), ""]
    if args.track in ("all", "basket"):
        summary, frames = run_basket("2021-12-01")
        report["basket"] = summary
        _plot("basket", summary, frames, image_dir / "overlay-basket.png",
              "관찰 트랙 · 대형주 10종목 동일비중 60% + 현금 40% · 2022년 약세장 포함")
        md += [_markdown("관찰 트랙 (대형주 10종목, 2021-12~)", summary), ""]
    (out_dir / "risk_overlay_backtest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "risk_overlay_backtest.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
