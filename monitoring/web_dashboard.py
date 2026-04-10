"""
실시간 웹 대시보드
- 콘솔 대시보드(monitoring/dashboard.py)를 확장한 웹 UI
- 포트폴리오 요약·포지션·스냅샷 추이를 실시간(폴링)으로 표시
"""

from datetime import datetime
from typing import Optional

from aiohttp import web
from loguru import logger

from config.config_loader import Config
from monitoring.dashboard import Dashboard
from database.repositories import get_portfolio_snapshots


# 기본 바인드 주소·포트 (settings.yaml dashboard 섹션으로 오버라이드 가능)
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def _serialize_snapshots(df):
    """DataFrame 스냅샷을 JSON 직렬화 가능한 리스트로 변환"""
    if df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        d = row.to_dict()
        if "date" in d and hasattr(d["date"], "strftime"):
            d["date"] = d["date"].strftime("%Y-%m-%d")
        # numpy 타입 → Python 네이티브
        for k, v in d.items():
            if hasattr(v, "item"):
                d[k] = v.item()
        out.append(d)
    return out


def get_portfolio_json(current_prices: Optional[dict] = None) -> dict:
    """현재 포트폴리오 요약을 JSON 친화적 dict로 반환"""
    config = Config.get()
    dash = Dashboard(config=config)
    summary = dash.portfolio_manager.get_portfolio_summary(current_prices or {})
    return {
        "timestamp": datetime.now().isoformat(),
        "initial_capital": dash.initial_capital,
        "total_value": summary["total_value"],
        "cash": summary["cash"],
        "invested": summary["invested"],
        "current_value": summary["current_value"],
        "total_return": summary["total_return"],
        "mdd": summary["mdd"],
        "position_count": summary["position_count"],
        "realized_pnl": summary["realized_pnl"],
        "unrealized_pnl": summary["unrealized_pnl"],
        "positions": summary["positions"],
    }


def get_snapshots_json(days: int = 30, account_key: Optional[str] = None) -> dict:
    """최근 N일 스냅샷을 JSON으로 반환"""
    df = get_portfolio_snapshots(days=days, account_key=account_key)
    return {"snapshots": _serialize_snapshots(df), "days": days}


def get_runtime_json() -> dict:
    """
    시장 국면(실시간 조회) + 스케줄러가 기록한 신호·루프·블랙스완·KIS 통계(JSON 파일).
    각 항목 실패 시 해당 필드만 null — 프론트에서 '조회 불가' 표시.
    """
    out: dict = {
        "timestamp": datetime.now().isoformat(),
        "market_regime": None,
        "signals_today": None,
        "signals_date": None,
        "strategy": None,
        "kis_stats": None,
        "kis_stats_source": None,
        "blackswan": None,
        "loop_metrics": None,
        "ws_gap": None,
        "runtime_file_updated_at": None,
    }

    try:
        cfg = Config.get()
        from core.market_regime import check_market_regime
        from core.data_collector import DataCollector

        mr = check_market_regime(cfg, DataCollector())
        out["market_regime"] = {
            "regime": mr.get("regime"),
            "position_scale": mr.get("position_scale"),
            "allow_buys": mr.get("allow_buys"),
        }
    except Exception as e:
        logger.debug("get_runtime_json market_regime: {}", e)

    try:
        from monitoring.dashboard_runtime_state import read_state

        st = read_state()
        out["runtime_file_updated_at"] = st.get("updated_at")
        _raw_sigs = st.get("signals_today")
        out["signals_today"] = _raw_sigs if isinstance(_raw_sigs, list) else []
        out["signals_date"] = st.get("signals_date")
        out["strategy"] = st.get("strategy")
        out["loop_metrics"] = st.get("loop_metrics")
        out["blackswan"] = st.get("blackswan")
        out["ws_gap"] = st.get("ws_gap")
        if st.get("kis_stats") is not None:
            out["kis_stats"] = st.get("kis_stats")
            out["kis_stats_source"] = "scheduler_file"
    except Exception as e:
        logger.debug("get_runtime_json read_state: {}", e)
        out["signals_today"] = None

    if out["kis_stats"] is None:
        try:
            from api.kis_api import KISApi

            out["kis_stats"] = KISApi().get_rate_limit_stats()
            out["kis_stats_source"] = "dashboard_process"
        except Exception as e:
            logger.debug("get_runtime_json KISApi: {}", e)

    return out


def _html_page() -> str:
    """대시보드 단일 페이지 HTML (인라인 CSS/JS, Chart.js CDN)"""
    return """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>퀀트 트레이더 대시보드</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root { --bg: #0f1419; --card: #1a2332; --text: #e6edf3; --muted: #8b949e; --up: #3fb950; --down: #f85149; --border: #30363d; }
    * { box-sizing: border-box; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 16px; background: var(--bg); color: var(--text); min-height: 100vh; }
    h1 { font-size: 1.5rem; margin: 0 0 8px 0; }
    .meta { color: var(--muted); font-size: 0.875rem; margin-bottom: 20px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
    .card .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; margin-bottom: 4px; }
    .card .value { font-size: 1.25rem; font-weight: 600; }
    .card .value.positive { color: var(--up); }
    .card .value.negative { color: var(--down); }
    section { margin-bottom: 24px; }
    section h2 { font-size: 1.1rem; margin: 0 0 12px 0; color: var(--muted); }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-weight: 600; }
    .num { text-align: right; }
    .positive { color: var(--up); }
    .negative { color: var(--down); }
    .chart-wrap { max-width: 800px; height: 260px; }
    .error { color: var(--down); font-size: 0.875rem; }
    #loading { color: var(--muted); }
    .muted { color: var(--muted); }
  </style>
</head>
<body>
  <h1>📊 퀀트 트레이더 대시보드</h1>
  <p class="meta">마지막 갱신: <span id="lastUpdate">-</span> · <span id="loading">자동 갱신 중 (10초 간격)</span></p>

  <section>
    <h2>시장 국면 · 리스크 · API · 루프</h2>
    <div class="grid" id="runtimeOps"></div>
    <p class="meta" id="runtimeMeta"></p>
  </section>

  <section>
    <h2>웹소켓 갭 모니터링</h2>
    <div class="grid" id="wsGapSummary"></div>
    <div id="wsGapTableWrap" style="display:none;">
      <table>
        <thead><tr><th>끊김 시각</th><th>재연결 시각</th><th class="num">갭(초)</th><th>영향 종목</th><th>REST 보충</th><th>관측 변동률</th><th>블랙스완 쿨다운</th></tr></thead>
        <tbody id="wsGapRows"></tbody>
      </table>
    </div>
    <p id="wsGapEmpty" class="meta" style="display:none;">갭 이벤트 없음</p>
    <p id="wsGapNA" class="meta" style="display:none;">웹소켓 정보 없음 (N/A)</p>
  </section>

  <section>
    <h2>오늘 발생 신호</h2>
    <div id="signalsTableWrap">
      <table>
        <thead><tr><th>시각</th><th>종목</th><th>신호</th><th class="num">점수</th><th>출처</th></tr></thead>
        <tbody id="signalRows"></tbody>
      </table>
    </div>
    <p id="signalEmpty" class="meta" style="display:none;">기록된 신호 없음 (스케줄러 장전 분석 이후 누적)</p>
    <p id="signalError" class="error" style="display:none;">조회 불가</p>
  </section>

  <section>
    <div class="grid" id="summary"></div>
  </section>

  <section>
    <h2>보유 포지션</h2>
    <div id="positionsWrap">
      <table>
        <thead><tr><th>종목</th><th class="num">수량</th><th class="num">평균가</th><th class="num">현재가</th><th class="num">평가액</th><th class="num">수익률</th></tr></thead>
        <tbody id="positions"></tbody>
      </table>
    </div>
    <p id="noPositions" style="display:none; color: var(--muted);">보유 종목 없음</p>
  </section>

  <section>
    <h2>수익률 추이 (최근 30일)</h2>
    <div class="chart-wrap"><canvas id="chartEquity"></canvas></div>
  </section>

  <script>
    const summaryEl = document.getElementById('summary');
    const runtimeOps = document.getElementById('runtimeOps');
    const runtimeMeta = document.getElementById('runtimeMeta');
    const signalRows = document.getElementById('signalRows');
    const signalEmpty = document.getElementById('signalEmpty');
    const signalError = document.getElementById('signalError');
    const signalsTableWrap = document.getElementById('signalsTableWrap');
    const positionsEl = document.getElementById('positions');
    const positionsWrap = document.getElementById('positionsWrap');
    const noPositions = document.getElementById('noPositions');
    const lastUpdate = document.getElementById('lastUpdate');
    let chartEquity = null;

    function fmtNum(n) { return Number(n).toLocaleString('ko-KR'); }
    function fmtPct(n) { return (Number(n) >= 0 ? '+' : '') + Number(n).toFixed(2) + '%'; }
    function escHtml(t) {
      const d = document.createElement('div');
      d.textContent = t == null ? '' : String(t);
      return d.innerHTML;
    }

    function card(label, value, cls) {
      return '<div class="card"><div class="label">' + escHtml(label) + '</div><div class="value ' + (cls || '') + '">' + value + '</div></div>';
    }

    function renderRuntime(rt) {
      const na = '<span class="muted">조회 불가</span>';
      if (!rt) {
        runtimeOps.innerHTML = card('시장 국면', na, '') + card('블랙스완', na, '') + card('KIS 60초 요청', na, '') + card('KIS 분당 활용률', na, '') + card('10분 루프(최근5회 평균)', na, '');
        runtimeMeta.textContent = '';
        signalsTableWrap.style.display = 'none';
        signalEmpty.style.display = 'none';
        signalError.style.display = 'block';
        return;
      }
      const mr = rt.market_regime;
      const mrTxt = mr && mr.regime ? String(mr.regime) : null;
      const bs = rt.blackswan;
      const bsTxt = bs && bs.display ? String(bs.display) : null;
      const kis = rt.kis_stats;
      const k60 = kis && (kis.requests_last_60s != null) ? String(kis.requests_last_60s) : null;
      const kpct = kis && (kis.minute_utilization_pct != null) ? (Number(kis.minute_utilization_pct).toFixed(1) + '%') : null;
      const lm = rt.loop_metrics;
      const loopAvg = lm && (lm.recent_avg_elapsed_s != null) ? (Number(lm.recent_avg_elapsed_s).toFixed(1) + '초') : null;
      const loopDetail = lm && Array.isArray(lm.recent_elapsed_last5) && lm.recent_elapsed_last5.length
        ? ' (' + lm.recent_elapsed_last5.map(function(x) { return Number(x).toFixed(0); }).join(', ') + '초)'
        : '';

      runtimeOps.innerHTML =
        card('시장 국면', mrTxt ? escHtml(mrTxt) : na, mrTxt === 'bearish' ? 'negative' : (mrTxt === 'bullish' ? 'positive' : '')) +
        card('블랙스완', bsTxt ? escHtml(bsTxt) : na, bs && bs.state === 'cooldown' ? 'negative' : '') +
        card('KIS 최근 60초 요청 수', k60 != null ? escHtml(k60) : na, '') +
        card('KIS 분당 활용률', kpct != null ? escHtml(kpct) : na, '') +
        card('10분 루프 평균(최근 5회)', loopAvg ? (escHtml(loopAvg + loopDetail)) : na, '');

      let metaParts = [];
      if (rt.runtime_file_updated_at) metaParts.push('스케줄러 스냅샷: ' + new Date(rt.runtime_file_updated_at).toLocaleString('ko-KR'));
      if (rt.kis_stats_source) metaParts.push('KIS 통계 출처: ' + rt.kis_stats_source);
      if (rt.strategy) metaParts.push('전략: ' + rt.strategy);
      runtimeMeta.textContent = metaParts.join(' · ');

      signalError.style.display = 'none';
      if (rt.signals_today === null || rt.signals_today === undefined) {
        signalsTableWrap.style.display = 'none';
        signalEmpty.style.display = 'none';
        signalError.style.display = 'block';
        return;
      }
      signalsTableWrap.style.display = 'block';
      const sigs = rt.signals_today;
      if (!sigs.length) {
        signalRows.innerHTML = '';
        signalEmpty.style.display = 'block';
        return;
      }
      signalEmpty.style.display = 'none';
      signalRows.innerHTML = sigs.map(function(r) {
        return '<tr><td>' + escHtml(r.at) + '</td><td>' + escHtml(r.symbol) + '</td><td>' + escHtml(r.signal) + '</td><td class="num">' + escHtml(Number(r.score).toFixed(2)) + '</td><td>' + escHtml(r.source || '') + '</td></tr>';
      }).join('');
    }

    const wsGapSummary = document.getElementById('wsGapSummary');
    const wsGapRows = document.getElementById('wsGapRows');
    const wsGapTableWrap = document.getElementById('wsGapTableWrap');
    const wsGapEmpty = document.getElementById('wsGapEmpty');
    const wsGapNA = document.getElementById('wsGapNA');

    function renderWsGap(rt) {
      const g = rt && rt.ws_gap;
      if (!g || !g.available) {
        wsGapSummary.innerHTML = card('웹소켓 상태', '<span class="muted">N/A</span>', '');
        wsGapTableWrap.style.display = 'none';
        wsGapEmpty.style.display = 'none';
        wsGapNA.style.display = 'block';
        return;
      }
      wsGapNA.style.display = 'none';
      const connTxt = g.is_connected ? '연결됨' : '끊김';
      const connCls = g.is_connected ? 'positive' : 'negative';
      const gapSince = g.current_gap_since ? new Date(g.current_gap_since).toLocaleString('ko-KR') : '-';
      wsGapSummary.innerHTML =
        card('웹소켓 상태', escHtml(connTxt), connCls) +
        card('총 갭 횟수', String(g.total_gap_count || 0), g.total_gap_count > 0 ? 'negative' : '') +
        card('진행 중 갭 시작', g.current_gap_since ? escHtml(gapSince) : '-', g.current_gap_since ? 'negative' : '');

      const gaps = g.recent_gaps || [];
      if (gaps.length === 0) {
        wsGapTableWrap.style.display = 'none';
        wsGapEmpty.style.display = 'block';
        return;
      }
      wsGapEmpty.style.display = 'none';
      wsGapTableWrap.style.display = 'block';
      wsGapRows.innerHTML = gaps.slice().reverse().map(function(ev) {
        const dAt = ev.disconnect_at ? new Date(ev.disconnect_at).toLocaleString('ko-KR') : '-';
        const rAt = ev.reconnect_at ? new Date(ev.reconnect_at).toLocaleString('ko-KR') : '-';
        const syms = (ev.affected_symbols || []).join(', ') || '-';
        const rest = ev.rest_backfill_performed ? (ev.rest_backfill_count + '건 조회') : '미수행';
        const vol = ev.observed_volatility && Object.keys(ev.observed_volatility).length
          ? Object.entries(ev.observed_volatility).map(function(e) { return e[0] + ': ' + e[1] + '%'; }).join(', ')
          : '-';
        const bsCool = ev.blackswan_cooldown_triggered ? '<span class="negative">발동</span>' : (ev.blackswan_checked ? '정상' : '-');
        return '<tr><td>' + escHtml(dAt) + '</td><td>' + escHtml(rAt) + '</td><td class="num">' + escHtml(String(ev.gap_seconds)) + '</td><td>' + escHtml(syms) + '</td><td>' + escHtml(rest) + '</td><td>' + escHtml(vol) + '</td><td>' + bsCool + '</td></tr>';
      }).join('');
    }

    function renderSummary(data) {
      const items = [
        { label: '총 평가금', value: fmtNum(data.total_value) + '원', cls: '' },
        { label: '총 수익률', value: fmtPct(data.total_return), cls: data.total_return >= 0 ? 'positive' : 'negative' },
        { label: '현금', value: fmtNum(data.cash) + '원', cls: '' },
        { label: '투자금', value: fmtNum(data.invested) + '원', cls: '' },
        { label: '실현 손익', value: fmtNum(data.realized_pnl) + '원', cls: data.realized_pnl >= 0 ? 'positive' : 'negative' },
        { label: '미실현 손익', value: fmtNum(data.unrealized_pnl) + '원', cls: data.unrealized_pnl >= 0 ? 'positive' : 'negative' },
        { label: 'MDD', value: fmtPct(-Math.abs(data.mdd)), cls: 'negative' },
        { label: '보유 종목', value: data.position_count + '개', cls: '' },
      ];
      summaryEl.innerHTML = items.map(i => '<div class="card"><div class="label">' + i.label + '</div><div class="value ' + i.cls + '">' + i.value + '</div></div>').join('');
    }

    function renderPositions(positions) {
      if (!positions || positions.length === 0) {
        positionsWrap.style.display = 'none';
        noPositions.style.display = 'block';
        return;
      }
      positionsWrap.style.display = 'block';
      noPositions.style.display = 'none';
      positionsEl.innerHTML = positions.map(p => {
        const cls = (p.pnl_rate >= 0 ? 'positive' : 'negative');
        return '<tr><td>' + (p.symbol || '-') + '</td><td class="num">' + (p.quantity ?? '-') + '</td><td class="num">' + fmtNum(p.avg_price) + '</td><td class="num">' + fmtNum(p.current_price) + '</td><td class="num">' + fmtNum(p.current_value) + '</td><td class="num ' + cls + '">' + fmtPct(p.pnl_rate) + '</td></tr>';
      }).join('');
    }

    function updateChart(snapshots) {
      if (!snapshots || snapshots.length === 0) return;
      const labels = snapshots.map(s => s.date);
      const values = snapshots.map(s => Number(s.total_value));
      const returns = snapshots.map(s => Number(s.cumulative_return || 0));

      if (!chartEquity) {
        const ctx = document.getElementById('chartEquity').getContext('2d');
        chartEquity = new Chart(ctx, {
          type: 'line',
          data: {
            labels: labels,
            datasets: [
              { label: '총 평가금 (원)', data: values, borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.1)', fill: true, yAxisID: 'y' },
              { label: '누적 수익률 (%)', data: returns, borderColor: '#3fb950', borderDash: [4,2], yAxisID: 'y1' }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
              y: { type: 'linear', position: 'left', title: { display: true, text: '평가금' } },
              y1: { type: 'linear', position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: '수익률 %' } }
            }
          }
        });
      } else {
        chartEquity.data.labels = labels;
        chartEquity.data.datasets[0].data = values;
        chartEquity.data.datasets[1].data = returns;
        chartEquity.update('none');
      }
    }

    async function fetchData() {
      let ts = '-';
      try {
        const portRes = await fetch('/api/portfolio');
        if (portRes.ok) {
          const portfolio = await portRes.json();
          ts = portfolio.timestamp ? new Date(portfolio.timestamp).toLocaleString('ko-KR') : '-';
          renderSummary(portfolio);
          renderPositions(portfolio.positions || []);
        } else {
          summaryEl.innerHTML = '<p class="error">포트폴리오 조회 불가</p>';
        }
      } catch (e) {
        summaryEl.innerHTML = '<p class="error">포트폴리오 조회 불가</p>';
      }
      try {
        const snapRes = await fetch('/api/snapshots?days=30');
        if (snapRes.ok) {
          const snapData = await snapRes.json();
          updateChart(snapData.snapshots || []);
        }
      } catch (e) { /* 차트만 스킵 */ }
      try {
        const rtRes = await fetch('/api/runtime');
        if (rtRes.ok) { const rtData = await rtRes.json(); renderRuntime(rtData); renderWsGap(rtData); }
        else { renderRuntime(null); renderWsGap(null); }
      } catch (e) {
        renderRuntime(null); renderWsGap(null);
      }
      lastUpdate.textContent = ts;
    }

    fetchData();
    setInterval(fetchData, 10000);
  </script>
</body>
</html>"""


async def handle_index(_request: web.Request) -> web.Response:
    return web.Response(text=_html_page(), content_type="text/html; charset=utf-8")


async def handle_api_portfolio(_request: web.Request) -> web.Response:
    try:
        data = get_portfolio_json()
        return web.json_response(data)
    except Exception as e:
        logger.exception("API /api/portfolio 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_runtime(_request: web.Request) -> web.Response:
    try:
        return web.json_response(get_runtime_json())
    except Exception as e:
        logger.exception("API /api/runtime 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_snapshots(request: web.Request) -> web.Response:
    try:
        days = int(request.query.get("days", 30))
        account_key = request.query.get("account_key") or None
        data = get_snapshots_json(days=days, account_key=account_key)
        return web.json_response(data)
    except Exception as e:
        logger.exception("API /api/snapshots 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/portfolio", handle_api_portfolio)
    app.router.add_get("/api/runtime", handle_api_runtime)
    app.router.add_get("/api/snapshots", handle_api_snapshots)
    return app


def run_web_dashboard(host: Optional[str] = None, port: Optional[int] = None):
    """웹 대시보드 서버 실행 (블로킹). host/port 미지정 시 config dashboard 섹션 또는 기본값 사용."""
    try:
        cfg = Config.get()
        settings = cfg.settings() if hasattr(cfg, "settings") else {}
        dash_cfg = (settings.get("dashboard") or {}) if isinstance(settings, dict) else {}
        host = host or dash_cfg.get("host") or DEFAULT_HOST
        port = port if port is not None else dash_cfg.get("port") or DEFAULT_PORT
    except Exception:
        host = host or DEFAULT_HOST
        port = port if port is not None else DEFAULT_PORT
    app = create_app()
    logger.info("웹 대시보드 서버 시작: http://{}:{}/", host if host != "0.0.0.0" else "127.0.0.1", port)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="퀀트 트레이더 웹 대시보드")
    p.add_argument("--host", default=None, help="바인드 주소 (기본: config 또는 0.0.0.0)")
    p.add_argument("--port", type=int, default=None, help="포트 (기본: config 또는 8080)")
    args = p.parse_args()
    from database.models import init_database
    from monitoring.logger import setup_logger
    setup_logger()
    init_database()
    run_web_dashboard(host=args.host, port=args.port)
