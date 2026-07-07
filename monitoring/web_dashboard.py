"""
실시간 웹 대시보드
- 콘솔 대시보드(monitoring/dashboard.py)를 확장한 웹 UI
- 포트폴리오 요약·포지션·스냅샷 추이를 실시간(폴링)으로 표시
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

try:
    from aiohttp import web
except ModuleNotFoundError:
    web = None
from loguru import logger

from config.config_loader import Config
from monitoring.dashboard import Dashboard
from database.repositories import get_portfolio_snapshots


# 기본 바인드 주소·포트 (settings.yaml dashboard 섹션으로 오버라이드 가능)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def _require_aiohttp_web():
    if web is None:
        raise RuntimeError("웹 대시보드 실행에는 aiohttp 설치가 필요합니다.")
    return web


def _serialize_snapshots(df):
    """DataFrame 스냅샷을 JSON 직렬화 가능한 리스트로 변환.

    날짜형은 컬럼을 특정하지 않고 전부 문자열화한다 — 'date'만 처리하던 시절
    created_at 컬럼 추가(일간 수익률 경계용)로 pd.Timestamp가 그대로 새어나가
    /api/snapshots가 매 폴링 500이 나고 수익률 차트가 조용히 죽었다(빈 DF만
    쓰는 테스트는 통과해서 못 잡던 회귀).
    """
    if df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        d = row.to_dict()
        for k, v in d.items():
            if hasattr(v, "strftime"):          # date/datetime/pd.Timestamp
                d[k] = v.strftime("%Y-%m-%d %H:%M:%S") if k != "date" else v.strftime("%Y-%m-%d")
            elif hasattr(v, "item"):            # numpy 타입 → Python 네이티브
                d[k] = v.item()
        out.append(d)
    return out


_DASH = None  # 폴링(10초)마다 Dashboard/PortfolioManager를 새로 만들면 초기화 INFO가 스팸이 된다


def get_portfolio_json(current_prices: Optional[dict] = None) -> dict:
    """현재 포트폴리오 요약을 JSON 친화적 dict로 반환"""
    global _DASH
    config = Config.get()
    if _DASH is None:
        _DASH = Dashboard(config=config)
    dash = _DASH
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


def get_baskets_json() -> dict:
    """enabled 바스켓별 '내 돈' 요약 — 최신 스냅샷·원금(입금 포함)·배치율·보유 (DB 전용).

    대시보드는 10초 폴링이므로 네트워크 조회를 섞지 않는다 — 평가금·수익률은
    일일 사이클이 저장한 최신 스냅샷 값(TWR 반영), 보유는 DB 포지션(평균단가 기준).
    적립식 계정(kr_pocket)의 핵심 질문 "내가 넣은 돈 대비 얼마"에 답하는 화면 데이터다.
    """
    from core.basket_rebalancer import BasketRebalancer, rebalance_live_strategy_id
    from database.repositories import (
        get_all_positions,
        get_cash_flow_total,
    )
    from database.models import PortfolioSnapshot, get_session

    config = Config.get()
    baskets_cfg = BasketRebalancer._load_baskets_config()
    global_capital = (config.risk_params.get("position_sizing") or {}).get(
        "initial_capital", 10_000_000
    )

    out = []
    for name in BasketRebalancer.get_enabled_baskets():
        cfg = baskets_cfg.get(name) or {}
        key = rebalance_live_strategy_id(name)
        initial = float(cfg.get("initial_capital") or global_capital)
        deposits = float(get_cash_flow_total(account_key=key) or 0)
        principal = initial + deposits

        # 최신 스냅샷 (mdd 포함해 직접 조회 — get_latest_snapshot_summary는 TWR용 최소 필드)
        session = get_session()
        try:
            snap = (
                session.query(PortfolioSnapshot)
                .filter(PortfolioSnapshot.account_key == key)
                .order_by(PortfolioSnapshot.date.desc())
                .first()
            )
            snapshot = None
            deployment_ratio = None
            if snap is not None:
                total = float(snap.total_value or 0)
                cash = float(snap.cash or 0)
                # 음수 클램프 — 헬스(run_health_check)와 동일 규칙(현금>총액 이상치 방어)
                deployment_ratio = (max(0.0, (total - cash) / total)) if total > 0 else None
                snapshot = {
                    "date": str(snap.date)[:10],
                    "total_value": total,
                    "cash": cash,
                    "cumulative_return": float(snap.cumulative_return or 0),
                    "mdd": float(snap.mdd or 0),
                }
        finally:
            session.close()

        # 설계 비중은 리밸런서·평가·헬스와 같은 단일 규칙을 쓴다 — 여기만 다르게
        # 계산하면 대시보드와 디스코드 카드가 서로 다른 '설계 %'를 보여준다.
        from core.basket_deploy import effective_stock_fraction
        design_fraction = effective_stock_fraction(cfg, config.risk_params)

        positions = [
            {
                "symbol": p.symbol,
                "quantity": int(p.quantity or 0),
                "avg_price": float(p.avg_price or 0),
                "invested": float((p.quantity or 0) * (p.avg_price or 0)),
            }
            for p in (get_all_positions(account_key=key) or [])
            if (p.quantity or 0) > 0
        ]

        out.append({
            "basket": name,
            "account_key": key,
            "display_name": cfg.get("name") or name,
            "initial_capital": initial,
            "deposits_total": deposits,
            "principal": principal,
            "snapshot": snapshot,
            "profit_vs_principal": (
                (snapshot["total_value"] - principal) if snapshot else None
            ),
            "deployment_ratio": deployment_ratio,
            "design_fraction": design_fraction,
            "positions": positions,
        })
    return {"baskets": out, "timestamp": datetime.now().isoformat()}


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

    # KIS 통계 폴백(대시보드 프로세스에서 KISApi 신규 생성) 제거 — 레이트리미터
    # 상태가 인스턴스별이라 항상 0(아무것도 측정 안 함)에 폴링마다 초기화 로그만
    # 남겼다. 스케줄러 파일에 없으면 정직하게 '조회 불가'로 둔다.
    return out


def _html_page() -> str:
    """대시보드 단일 페이지 HTML — 2026-07 UI 개편(벤토 그리드·다크 글래스·Pretendard).

    원칙: ① 내 돈(바스켓 트랙)이 첫 화면 ② 웹의 쓰기 권한은 '입금 기록' 하나
    (매매·설정 변경은 웹에 두지 않는다) ③ 폴링 경로에 네트워크 조회 없음(DB 전용 API).
    """
    return """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>퀀트 트레이더</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0b0f14; --bg2: #0e141b; --surface: rgba(255,255,255,0.03);
      --surface2: rgba(255,255,255,0.055); --border: rgba(255,255,255,0.08);
      --text: #e8edf4; --muted: #8b97a5; --dim: #5c6773;
      --up: #34d399; --down: #f87171; --accent: #818cf8; --warn: #fbbf24;
      --radius: 16px; --radius-sm: 10px;
    }
    * { box-sizing: border-box; }
    html { scrollbar-color: #2a3441 transparent; }
    body {
      font-family: 'Pretendard Variable', Pretendard, 'Segoe UI', system-ui, sans-serif;
      margin: 0; background:
        radial-gradient(1200px 500px at 15% -10%, rgba(129,140,248,0.08), transparent 60%),
        radial-gradient(900px 400px at 95% 0%, rgba(52,211,153,0.05), transparent 55%),
        var(--bg);
      color: var(--text); min-height: 100vh; font-size: 15px; letter-spacing: -0.01em;
    }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 0 20px 64px; }

    /* 상단바 */
    .topbar {
      position: sticky; top: 0; z-index: 40; backdrop-filter: blur(14px);
      background: rgba(11,15,20,0.75); border-bottom: 1px solid var(--border);
    }
    .topbar-in { max-width: 1180px; margin: 0 auto; padding: 14px 20px; display: flex; align-items: center; gap: 14px; }
    .brand { font-weight: 800; font-size: 1.05rem; letter-spacing: -0.02em; }
    .brand small { color: var(--muted); font-weight: 500; margin-left: 8px; }
    .live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--up); box-shadow: 0 0 8px rgba(52,211,153,0.7); animation: pulse 2.4s infinite; }
    @keyframes pulse { 50% { opacity: 0.35; } }
    .topbar .meta { color: var(--muted); font-size: 0.8rem; margin-left: auto; }
    .btn {
      border: 1px solid var(--border); background: var(--surface2); color: var(--text);
      padding: 8px 16px; border-radius: 999px; font: inherit; font-size: 0.85rem; font-weight: 600;
      cursor: pointer; transition: all .15s;
    }
    .btn:hover { background: rgba(255,255,255,0.1); transform: translateY(-1px); }
    .btn-primary { background: linear-gradient(135deg, #34d399, #10b981); color: #06281c; border: none; }
    .btn-primary:hover { filter: brightness(1.08); }

    section { margin-top: 36px; }
    section > h2 { font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--dim); margin: 0 0 14px 2px; display:flex; align-items:center; gap:10px; }

    /* 패널·카드 */
    .panel {
      background: linear-gradient(180deg, var(--surface2), var(--surface));
      border: 1px solid var(--border); border-radius: var(--radius); padding: 22px;
    }
    .bento { display: grid; gap: 16px; }
    .basket-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
    .basket-head .name { font-size: 1.05rem; font-weight: 700; }
    .basket-head .tag { font-size: 0.72rem; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 2px 10px; }
    .hero-num { font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; line-height: 1.1; }
    .hero-sub { color: var(--muted); font-size: 0.82rem; margin-top: 4px; }
    .delta { font-size: 0.95rem; font-weight: 700; }
    .positive { color: var(--up); } .negative { color: var(--down); } .muted { color: var(--muted); }

    .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(128px, 1fr)); gap: 10px; margin-top: 18px; }
    .stat { background: rgba(0,0,0,0.18); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px 12px; }
    .stat .k { color: var(--dim); font-size: 0.68rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
    .stat .v { font-size: 0.98rem; font-weight: 700; margin-top: 3px; }

    .flows { margin-top: 14px; font-size: 0.8rem; color: var(--muted); }
    .flows span { margin-right: 12px; }

    /* 진행률 바 */
    .prog-row { margin-bottom: 14px; }
    .prog-row .lbl { display: flex; justify-content: space-between; font-size: 0.82rem; color: var(--muted); margin-bottom: 6px; }
    .bar { height: 8px; background: rgba(255,255,255,0.06); border-radius: 999px; overflow: hidden; }
    .bar > i { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, #818cf8, #34d399); transition: width .5s; }

    /* 카드 그리드(운영 상태) */
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); gap: 12px; }
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 13px 14px; }
    .card .label { color: var(--dim); font-size: 0.68rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 5px; }
    .card .value { font-size: 1.02rem; font-weight: 700; }

    table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
    th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--border); }
    th { color: var(--dim); font-weight: 600; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .chart-wrap { height: 280px; }
    .error { color: var(--down); font-size: 0.85rem; }
    select {
      background: var(--surface2); color: var(--text); border: 1px solid var(--border);
      border-radius: 999px; padding: 5px 12px; font: inherit; font-size: 0.8rem;
    }
    details { border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 18px; background: var(--surface); }
    details summary { cursor: pointer; color: var(--muted); font-size: 0.85rem; font-weight: 600; }

    /* 입금 모달 */
    .overlay { position: fixed; inset: 0; background: rgba(4,7,10,0.7); backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center; z-index: 100; }
    .overlay.open { display: flex; }
    .modal { width: min(420px, calc(100vw - 40px)); background: var(--bg2); border: 1px solid var(--border); border-radius: 20px; padding: 26px; }
    .modal h3 { margin: 0 0 4px; font-size: 1.15rem; }
    .modal .hint { color: var(--muted); font-size: 0.8rem; margin-bottom: 18px; }
    .field { margin-bottom: 14px; }
    .field label { display: block; font-size: 0.75rem; color: var(--muted); font-weight: 600; margin-bottom: 6px; }
    .field input, .field select { width: 100%; background: var(--surface2); border: 1px solid var(--border); color: var(--text); border-radius: var(--radius-sm); padding: 10px 12px; font: inherit; }
    .presets { display: flex; gap: 8px; margin-top: 8px; }
    .chip { flex: 1; text-align: center; padding: 8px 0; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface); font-size: 0.82rem; font-weight: 600; cursor: pointer; }
    .chip:hover, .chip.on { border-color: var(--up); color: var(--up); }
    .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }

    /* 토스트 */
    #toast { position: fixed; bottom: 26px; left: 50%; transform: translateX(-50%) translateY(20px); background: var(--bg2); border: 1px solid var(--border); border-radius: 999px; padding: 11px 22px; font-size: 0.86rem; font-weight: 600; opacity: 0; transition: all .25s; z-index: 200; pointer-events: none; }
    #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
    #toast.ok { border-color: rgba(52,211,153,0.5); color: var(--up); }
    #toast.err { border-color: rgba(248,113,113,0.5); color: var(--down); }
  </style>
</head>
<body>
  <div class="topbar"><div class="topbar-in">
    <div class="live-dot"></div>
    <div class="brand">퀀트 트레이더<small>paper 운영</small></div>
    <span class="meta">갱신 <span id="lastUpdate">-</span></span>
    <button class="btn btn-primary" onclick="openDeposit()">+ 적립 입금</button>
  </div></div>

  <div class="wrap">

  <section>
    <h2>내 자산</h2>
    <div class="bento" id="basketTracks"><div class="panel muted">불러오는 중...</div></div>
  </section>

  <section>
    <h2>수익률 추이 <select id="chartAccount"></select></h2>
    <div class="panel"><div class="chart-wrap"><canvas id="chartEquity"></canvas></div></div>
  </section>

  <section>
    <h2>승격 진행률 <span class="muted" style="text-transform:none;letter-spacing:0;">60영업일 트랙레코드</span></h2>
    <div class="panel" id="basketEval"></div>
  </section>

  <section>
    <h2>운영 상태</h2>
    <div class="grid" id="runtimeOps"></div>
    <p class="flows" id="runtimeMeta"></p>
  </section>

  <section>
    <h2>오늘 신호</h2>
    <div class="panel">
      <div id="signalsTableWrap">
        <table>
          <thead><tr><th>시각</th><th>종목</th><th>신호</th><th class="num">점수</th><th>출처</th></tr></thead>
          <tbody id="signalRows"></tbody>
        </table>
      </div>
      <p id="signalEmpty" class="muted" style="display:none;">기록된 신호 없음</p>
      <p id="signalError" class="error" style="display:none;">조회 불가</p>
    </div>
  </section>

  <section>
    <details>
      <summary>웹소켓 갭 · 레거시 기본 계정</summary>
      <div style="margin-top:14px;">
        <div class="grid" id="wsGapSummary"></div>
        <div id="wsGapTableWrap" style="display:none; margin-top:12px;">
          <table>
            <thead><tr><th>끊김</th><th>재연결</th><th class="num">갭(초)</th><th>영향 종목</th><th>REST 보충</th><th>블랙스완</th></tr></thead>
            <tbody id="wsGapRows"></tbody>
          </table>
        </div>
        <p id="wsGapEmpty" class="muted" style="display:none;">갭 이벤트 없음</p>
        <p id="wsGapNA" class="muted" style="display:none;">웹소켓 정보 없음</p>
        <div class="grid" id="summary" style="margin-top:16px;"></div>
        <div id="positionsWrap" style="margin-top:12px;">
          <table>
            <thead><tr><th>종목</th><th class="num">수량</th><th class="num">평균가</th><th class="num">현재가</th><th class="num">평가액</th><th class="num">수익률</th></tr></thead>
            <tbody id="positions"></tbody>
          </table>
        </div>
        <p id="noPositions" class="muted" style="display:none;">보유 종목 없음</p>
      </div>
    </details>
  </section>

  </div>

  <!-- 입금 모달 -->
  <div class="overlay" id="depositOverlay" onclick="if(event.target===this)closeDeposit()">
    <div class="modal">
      <h3>적립 입금 기록</h3>
      <p class="hint">paper는 기록 = 입금. 입금은 수익률(TWR)이 중화하므로 성과가 왜곡되지 않습니다. 다음 사이클이 새 현금을 흡수합니다.</p>
      <div class="field"><label>바스켓</label><select id="depBasket"></select></div>
      <div class="field">
        <label>금액 (원)</label>
        <input id="depAmount" type="number" min="1" step="10000" placeholder="100000">
        <div class="presets">
          <div class="chip" onclick="setAmt(50000,this)">5만</div>
          <div class="chip" onclick="setAmt(100000,this)">10만</div>
          <div class="chip" onclick="setAmt(200000,this)">20만</div>
        </div>
      </div>
      <div class="field"><label>메모 (선택)</label><input id="depNote" type="text" placeholder="7월 적립"></div>
      <div class="modal-actions">
        <button class="btn" onclick="closeDeposit()">취소</button>
        <button class="btn btn-primary" id="depSubmit" onclick="submitDeposit()">기록</button>
      </div>
    </div>
  </div>

  <div id="toast"></div>

  <script>
    const $ = (id) => document.getElementById(id);
    const basketTracksEl = $('basketTracks');
    const chartAccountSel = $('chartAccount');
    let chartEquity = null;
    let lastBaskets = [];
    const flowsCache = {};   // basket → flows(입금 내역)

    const fmtNum = (n) => Number(n).toLocaleString('ko-KR');
    const fmtPct = (n) => (Number(n) >= 0 ? '+' : '') + Number(n).toFixed(2) + '%';
    function escHtml(t) {
      const d = document.createElement('div');
      d.textContent = t == null ? '' : String(t);
      // textContent→innerHTML은 &<>만 이스케이프 — value="..." 속성에도 쓰이므로 따옴표까지.
      return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    const card = (label, value, cls) => `<div class="card"><div class="label">${escHtml(label)}</div><div class="value ${cls || ''}">${value}</div></div>`;
    const stat = (k, v, cls) => `<div class="stat"><div class="k">${escHtml(k)}</div><div class="v ${cls || ''}">${v}</div></div>`;

    function toast(msg, ok) {
      const t = $('toast');
      t.textContent = msg; t.className = 'show ' + (ok ? 'ok' : 'err');
      setTimeout(() => { t.className = ''; }, 3200);
    }

    /* ── 내 자산 (바스켓 트랙) ── */
    function renderBasketTracks(data) {
      const baskets = (data && data.baskets) || [];
      lastBaskets = baskets;
      if (!baskets.length) { basketTracksEl.innerHTML = '<div class="panel muted">enabled 바스켓 없음</div>'; return; }
      basketTracksEl.innerHTML = baskets.map(function(b) {
        const s = b.snapshot;
        const ret = s ? Number(s.cumulative_return) : null;
        const pvp = b.profit_vs_principal;
        const retCls = ret == null ? 'muted' : (ret >= 0 ? 'positive' : 'negative');
        const pvpCls = pvp == null ? 'muted' : (pvp >= 0 ? 'positive' : 'negative');
        const dep = b.deployment_ratio != null
          ? Math.round(b.deployment_ratio * 100) + '% <span class="muted">/ ' + Math.round(b.design_fraction * 100) + '%</span>' : '-';
        const pos = (b.positions || []).map(p => escHtml(p.symbol) + ' ' + p.quantity + '주').join(' · ') || '보유 없음';
        const fl = flowsCache[b.basket] || [];
        const flowsHtml = fl.length
          ? '<div class="flows">최근 입금: ' + fl.slice(0, 3).map(f => `<span>${escHtml(f.occurred_at.slice(0, 10))} +${fmtNum(f.amount)}원${f.note ? ' (' + escHtml(f.note) + ')' : ''}</span>`).join('') + '</div>'
          : '';
        return `<div class="panel">
          <div class="basket-head">
            <span class="name">${escHtml(b.display_name)}</span>
            <span class="tag">${escHtml(b.basket)}</span>
            ${s ? '<span class="tag">' + escHtml(s.date) + '</span>' : ''}
          </div>
          <div style="display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;">
            <div>
              <div class="hero-num">${s ? fmtNum(s.total_value) + '<span style="font-size:1rem;color:var(--muted);font-weight:600;"> 원</span>' : '<span class="muted">스냅샷 없음</span>'}</div>
              <div class="hero-sub">원금 ${fmtNum(b.principal)}원${b.deposits_total > 0 ? ' (입금 ' + fmtNum(b.deposits_total) + '원 포함)' : ''}</div>
            </div>
            <div class="delta ${pvpCls}">${pvp != null ? ((pvp >= 0 ? '+' : '') + fmtNum(pvp) + '원') : ''}</div>
            <div class="delta ${retCls}">${ret != null ? fmtPct(ret) : ''}</div>
          </div>
          <div class="stats">
            ${stat('수익률 (TWR)', ret != null ? fmtPct(ret) : '-', retCls)}
            ${stat('MDD', s ? (Number(s.mdd) === 0 ? '0.00%' : fmtPct(-Math.abs(s.mdd))) : '-', 'negative')}
            ${stat('현금', s ? fmtNum(s.cash) + '원' : '-', '')}
            ${stat('주식 배치율', dep, '')}
            ${stat('보유', escHtml(pos), '')}
          </div>
          ${flowsHtml}
        </div>`;
      }).join('');
    }

    async function refreshFlows() {
      for (const b of lastBaskets) {
        if (b.deposits_total > 0 || b.basket.indexOf('pocket') >= 0) {
          try {
            const r = await fetch('/api/cash_flows?basket=' + encodeURIComponent(b.basket));
            if (r.ok) { flowsCache[b.basket] = (await r.json()).flows || []; }
          } catch (e) { /* skip */ }
        }
      }
    }

    /* ── 차트 계정 선택 ── */
    function ensureChartAccountOptions(data) {
      const baskets = (data && data.baskets) || [];
      const wanted = baskets.map(b => ({ v: b.account_key, t: b.display_name }));
      wanted.push({ v: '', t: '기본 계정' });
      // 개수만 비교하면 바스켓 교체/개명 시 스테일 옵션이 남는다 — 값 시그니처로 비교.
      const sig = wanted.map(w => w.v + '' + w.t).join('');
      if (chartAccountSel.dataset.sig === sig) return;
      chartAccountSel.dataset.sig = sig;
      const prev = chartAccountSel.value;
      chartAccountSel.innerHTML = wanted.map(w => `<option value="${escHtml(w.v)}">${escHtml(w.t)}</option>`).join('');
      chartAccountSel.value = prev && Array.from(chartAccountSel.options).some(o => o.value === prev) ? prev : (wanted[0] ? wanted[0].v : '');
    }
    chartAccountSel.addEventListener('change', () => { if (chartEquity) { chartEquity.destroy(); chartEquity = null; } fetchData(); });

    function updateChart(snapshots) {
      if (!snapshots || snapshots.length === 0) { if (chartEquity) { chartEquity.destroy(); chartEquity = null; } return; }
      const labels = snapshots.map(s => String(s.date).slice(0, 10));
      const values = snapshots.map(s => Number(s.total_value));
      const returns = snapshots.map(s => Number(s.cumulative_return || 0));
      if (!chartEquity) {
        const ctx = $('chartEquity').getContext('2d');
        const grad = ctx.createLinearGradient(0, 0, 0, 260);
        grad.addColorStop(0, 'rgba(129,140,248,0.28)'); grad.addColorStop(1, 'rgba(129,140,248,0)');
        chartEquity = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [
            { label: '평가금 (원)', data: values, borderColor: '#818cf8', backgroundColor: grad, fill: true, tension: 0.35, pointRadius: 2, borderWidth: 2, yAxisID: 'y' },
            { label: '누적 수익률 (%)', data: returns, borderColor: '#34d399', borderDash: [5, 3], tension: 0.35, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' }
          ]},
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: { legend: { labels: { color: '#8b97a5', usePointStyle: true, boxWidth: 8 } } },
            scales: {
              x: { ticks: { color: '#5c6773', maxTicksLimit: 8 }, grid: { color: 'rgba(255,255,255,0.04)' } },
              y: { position: 'left', ticks: { color: '#5c6773' }, grid: { color: 'rgba(255,255,255,0.04)' } },
              y1: { position: 'right', ticks: { color: '#34d399' }, grid: { drawOnChartArea: false } }
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

    /* ── 승격 진행률 (프로그레스 바) ── */
    function renderEval(evs) {
      const el = $('basketEval');
      if (!evs || !evs.length) { el.innerHTML = '<p class="muted">enabled 바스켓 없음</p>'; return; }
      el.innerHTML = evs.map(function(ev) {
        const pd = ev.progress_days || 0, md = ev.min_trading_days || 60;
        const pct = Math.min(100, Math.round(pd / md * 100));
        const cov = ev.snapshot_coverage != null ? Math.round(ev.snapshot_coverage * 100) : null;
        const vCls = ev.verdict === 'PASS_CANDIDATE' ? 'positive' : (ev.verdict === 'FAIL_REVIEW' ? 'negative' : 'muted');
        const issues = (ev.issues || []).map(i => '<div class="flows">⚠ ' + escHtml(i) + '</div>').join('');
        return `<div class="prog-row">
          <div class="lbl"><span><b style="color:var(--text)">${escHtml(ev.basket)}</b> · <span class="${vCls}">${escHtml(ev.verdict || '-')}</span></span>
          <span>${pd}/${md}일 (${pct}%)${cov != null ? ' · 커버리지 ' + cov + '%' : ''}</span></div>
          <div class="bar"><i style="width:${pct}%"></i></div>
          ${issues}
        </div>`;
      }).join('');
    }

    /* ── 운영 상태 ── */
    function renderRuntime(rt) {
      const ops = $('runtimeOps'), meta = $('runtimeMeta');
      const na = '<span class="muted">조회 불가</span>';
      if (!rt) {
        ops.innerHTML = card('시장 국면', na) + card('블랙스완', na) + card('KIS 요청(60초)', na) + card('루프', na);
        $('signalsTableWrap').style.display = 'none'; $('signalEmpty').style.display = 'none'; $('signalError').style.display = 'block';
        meta.textContent = ''; return;
      }
      const mr = rt.market_regime, bs = rt.blackswan, kis = rt.kis_stats, lm = rt.loop_metrics;
      const mrTxt = mr && mr.regime ? String(mr.regime) : null;
      ops.innerHTML =
        card('시장 국면', mrTxt ? escHtml(mrTxt) : na, mrTxt === 'bearish' ? 'negative' : (mrTxt === 'bullish' ? 'positive' : '')) +
        card('블랙스완', bs && bs.display ? escHtml(bs.display) : na, bs && bs.state === 'cooldown' ? 'negative' : '') +
        card('KIS 요청(60초)', kis && kis.requests_last_60s != null ? escHtml(String(kis.requests_last_60s)) : na) +
        card('KIS 분당 활용률', kis && kis.minute_utilization_pct != null ? escHtml(Number(kis.minute_utilization_pct).toFixed(1) + '%') : na) +
        card('10분 루프 평균', lm && lm.recent_avg_elapsed_s != null ? escHtml(Number(lm.recent_avg_elapsed_s).toFixed(1) + '초') : na);
      let mp = [];
      if (rt.runtime_file_updated_at) mp.push('스케줄러 스냅샷 ' + new Date(rt.runtime_file_updated_at).toLocaleString('ko-KR'));
      if (rt.strategy) mp.push('전략 ' + rt.strategy);
      meta.textContent = mp.join(' · ');

      $('signalError').style.display = 'none';
      if (rt.signals_today == null) { $('signalsTableWrap').style.display = 'none'; $('signalEmpty').style.display = 'none'; $('signalError').style.display = 'block'; return; }
      const sigs = rt.signals_today;
      $('signalsTableWrap').style.display = sigs.length ? 'block' : 'none';
      $('signalEmpty').style.display = sigs.length ? 'none' : 'block';
      $('signalRows').innerHTML = sigs.map(r =>
        `<tr><td>${escHtml(r.at)}</td><td>${escHtml(r.symbol)}</td><td>${escHtml(r.signal)}</td><td class="num">${escHtml(Number(r.score).toFixed(2))}</td><td>${escHtml(r.source || '')}</td></tr>`
      ).join('');
    }

    function renderWsGap(rt) {
      const g = rt && rt.ws_gap;
      const sum = $('wsGapSummary');
      if (!g || !g.available) { sum.innerHTML = card('웹소켓', '<span class="muted">N/A</span>'); $('wsGapTableWrap').style.display = 'none'; $('wsGapEmpty').style.display = 'none'; $('wsGapNA').style.display = 'block'; return; }
      $('wsGapNA').style.display = 'none';
      sum.innerHTML =
        card('웹소켓 상태', g.is_connected ? '연결됨' : '끊김', g.is_connected ? 'positive' : 'negative') +
        card('총 갭 횟수', String(g.total_gap_count || 0), g.total_gap_count > 0 ? 'negative' : '');
      const gaps = g.recent_gaps || [];
      $('wsGapTableWrap').style.display = gaps.length ? 'block' : 'none';
      $('wsGapEmpty').style.display = gaps.length ? 'none' : 'block';
      $('wsGapRows').innerHTML = gaps.slice().reverse().map(ev =>
        `<tr><td>${ev.disconnect_at ? new Date(ev.disconnect_at).toLocaleString('ko-KR') : '-'}</td>
        <td>${ev.reconnect_at ? new Date(ev.reconnect_at).toLocaleString('ko-KR') : '-'}</td>
        <td class="num">${escHtml(String(ev.gap_seconds))}</td>
        <td>${escHtml((ev.affected_symbols || []).join(', ') || '-')}</td>
        <td>${ev.rest_backfill_performed ? escHtml(ev.rest_backfill_count + '건') : '미수행'}</td>
        <td>${ev.blackswan_cooldown_triggered ? '<span class="negative">발동</span>' : (ev.blackswan_checked ? '정상' : '-')}</td></tr>`
      ).join('');
    }

    /* ── 레거시 기본 계정 ── */
    function renderSummary(d) {
      $('summary').innerHTML =
        card('총 평가금', fmtNum(d.total_value) + '원') +
        card('총 수익률', fmtPct(d.total_return), d.total_return >= 0 ? 'positive' : 'negative') +
        card('현금', fmtNum(d.cash) + '원') +
        card('실현 손익', fmtNum(d.realized_pnl) + '원', d.realized_pnl >= 0 ? 'positive' : 'negative') +
        card('MDD', fmtPct(-Math.abs(d.mdd)), 'negative') +
        card('보유', d.position_count + '개');
    }
    function renderPositions(ps) {
      const has = ps && ps.length;
      $('positionsWrap').style.display = has ? 'block' : 'none';
      $('noPositions').style.display = has ? 'none' : 'block';
      if (!has) return;
      $('positions').innerHTML = ps.map(p => {
        const cls = p.pnl_rate >= 0 ? 'positive' : 'negative';
        return `<tr><td>${escHtml(p.symbol || '-')}</td><td class="num">${p.quantity ?? '-'}</td><td class="num">${fmtNum(p.avg_price)}</td><td class="num">${fmtNum(p.current_price)}</td><td class="num">${fmtNum(p.current_value)}</td><td class="num ${cls}">${fmtPct(p.pnl_rate)}</td></tr>`;
      }).join('');
    }

    /* ── 입금 모달 ── */
    function openDeposit() {
      const sel = $('depBasket');
      sel.innerHTML = lastBaskets.map(b => `<option value="${escHtml(b.basket)}">${escHtml(b.display_name)}</option>`).join('');
      // 기본 선택: pocket(적립 트랙)이 있으면 그것
      const pocket = lastBaskets.find(b => b.basket.indexOf('pocket') >= 0);
      if (pocket) sel.value = pocket.basket;
      $('depAmount').value = ''; $('depNote').value = '';
      document.querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
      $('depositOverlay').classList.add('open');
    }
    function closeDeposit() { $('depositOverlay').classList.remove('open'); }
    function setAmt(v, el) {
      $('depAmount').value = v;
      document.querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
      el.classList.add('on');
    }
    async function submitDeposit() {
      const basket = $('depBasket').value;
      const amount = Number($('depAmount').value);
      if (!basket) { toast('바스켓을 선택하세요', false); return; }
      if (!amount || amount <= 0) { toast('금액을 입력하세요 (양수)', false); return; }
      const btn = $('depSubmit'); btn.disabled = true; btn.textContent = '기록 중...';
      try {
        const res = await fetch('/api/deposit', {
          method: 'POST',
          // X-Requested-With: 서버의 CSRF 방어(커스텀 헤더 필수)와 한 쌍
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'quant-dashboard' },
          body: JSON.stringify({ basket, amount, note: $('depNote').value || '' })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          toast('입금 기록 완료: +' + fmtNum(data.amount) + '원 (누적 입금 ' + fmtNum(data.deposits_total) + '원)', true);
          closeDeposit();
          await refreshFlows();
          fetchData();
        } else {
          toast('실패: ' + (data.error || res.status), false);
        }
      } catch (e) { toast('요청 실패: ' + e, false); }
      finally { btn.disabled = false; btn.textContent = '기록'; }
    }

    /* ── 폴링 ── */
    let _polling = false;  // 오버랩 가드 — 느린 응답(운영 상태 수십 초)이 폴링을 적체시키지 않게
    async function fetchData() {
      if (_polling) return;
      _polling = true;
      try { await _fetchDataInner(); } finally { _polling = false; }
    }
    async function _fetchDataInner() {
      let ts = new Date().toLocaleTimeString('ko-KR');
      try {
        const bkRes = await fetch('/api/baskets');
        if (bkRes.ok) { const bk = await bkRes.json(); ensureChartAccountOptions(bk); renderBasketTracks(bk); }
        else { basketTracksEl.innerHTML = '<div class="panel error">바스켓 조회 불가</div>'; }
      } catch (e) { basketTracksEl.innerHTML = '<div class="panel error">바스켓 조회 불가</div>'; }
      try {
        // account_key는 빈 값이어도 항상 보낸다 — 파라미터 부재는 '무필터(전 계정 혼합)'라
        // 기본 계정('')과 의미가 다르다.
        const acct = chartAccountSel.value;
        const url = '/api/snapshots?days=30&account_key=' + encodeURIComponent(acct);
        const r = await fetch(url);
        if (r.ok) updateChart((await r.json()).snapshots || []);
      } catch (e) { /* skip */ }
      try {
        const r = await fetch('/api/basket_evaluation');
        if (r.ok) renderEval((await r.json()).evaluations || []);
        else $('basketEval').innerHTML = '<p class="error">평가 조회 불가</p>';
      } catch (e) { /* skip */ }
      try {
        const r = await fetch('/api/runtime');
        if (r.ok) { const rt = await r.json(); renderRuntime(rt); renderWsGap(rt); } else { renderRuntime(null); renderWsGap(null); }
      } catch (e) { renderRuntime(null); renderWsGap(null); }
      try {
        const r = await fetch('/api/portfolio');
        if (r.ok) { const p = await r.json(); renderSummary(p); renderPositions(p.positions || []); }
      } catch (e) { /* skip */ }
      $('lastUpdate').textContent = ts;
    }

    (async function boot() {
      await fetchData();
      await refreshFlows();
      fetchData();  // 입금 내역 반영 재렌더
      setInterval(fetchData, 10000);
      setInterval(refreshFlows, 60000);
    })();
  </script>
</body>
</html>"""


async def handle_index(_request: web.Request) -> web.Response:
    # aiohttp 3.13+: content_type에 charset을 섞으면 ValueError — 분리 인자로 전달.
    # (기존 표기는 메인 페이지 '/'를 500으로 죽이는 운영 결함이었다 — API만 검증하고
    # 페이지 서빙은 검증하지 않아 가려져 있었다.)
    return web.Response(text=_html_page(), content_type="text/html", charset="utf-8")


async def handle_api_portfolio(_request: web.Request) -> web.Response:
    # live 모드에서는 KIS 잔고 조회(동기 네트워크)가 섞일 수 있다 — 스레드로 격리.
    import asyncio

    try:
        data = await asyncio.to_thread(get_portfolio_json)
        return web.json_response(data)
    except Exception as e:
        logger.exception("API /api/portfolio 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_baskets(_request: web.Request) -> web.Response:
    """바스켓 트랙 '내 돈' 요약 — DB 전용(네트워크 조회 없음), 10초 폴링 안전."""
    try:
        return web.json_response(get_baskets_json())
    except Exception as e:
        logger.exception("API /api/baskets 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_deposit(request: web.Request) -> web.Response:
    """적립 입금 기록 (POST {basket, amount, note?}) — CLI와 동일한 단일 검증 경로.

    웹에서 가능한 쓰기는 이것 하나다(기록·조회까지가 웹의 권한 — 매매·설정 변경은
    웹에 두지 않는다). occurred_at은 서버 시각 고정이라 소급 조작이 불가능하고,
    금액 양수·바스켓 존재·TWR 체인 보호(마지막 스냅샷 이후) 검증은 공유 함수가 한다.

    CSRF 방어: 커스텀 헤더(X-Requested-With) 필수 — 루프백 바인딩이어도 브라우저
    경유 cross-site 요청은 막지 못한다(악성 페이지가 text/plain fetch로 127.0.0.1에
    POST 가능, aiohttp request.json()은 Content-Type을 보지 않음). 커스텀 헤더는
    CORS preflight를 강제하는데 이 서버는 preflight에 응답하지 않으므로 외부
    오리진에서는 실을 수 없다. 대시보드 프론트만 이 헤더를 보낸다.
    """
    if request.headers.get("X-Requested-With") != "quant-dashboard":
        return web.json_response(
            {"ok": False, "error": "대시보드 외 요청 차단(CSRF 방어)"}, status=403,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "JSON 본문이 필요합니다"}, status=400)
    try:
        from tools.record_deposit import record_basket_deposit

        result = record_basket_deposit(
            str(body.get("basket") or ""),
            body.get("amount"),
            note=str(body.get("note") or ""),
        )
        if not result.get("ok"):
            return web.json_response(result, status=400)
        logger.info(
            "웹 입금 기록: {} +{:,.0f}원 (누적 입금 {:,.0f}원)",
            result["account_key"], result["amount"], result["deposits_total"],
        )
        return web.json_response(result)
    except Exception as e:
        logger.exception("API /api/deposit 오류: {}", e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_api_cash_flows(request: web.Request) -> web.Response:
    """바스켓 입금 내역 (GET ?basket=) — 최근 12건."""
    try:
        from core.basket_rebalancer import rebalance_live_strategy_id
        from database.repositories import get_recent_cash_flows

        basket = request.query.get("basket") or ""
        if not basket:
            return web.json_response({"error": "basket 파라미터 필요"}, status=400)
        key = rebalance_live_strategy_id(basket)
        return web.json_response({"basket": basket, "flows": get_recent_cash_flows(key)})
    except Exception as e:
        logger.exception("API /api/cash_flows 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_runtime(_request: web.Request) -> web.Response:
    # get_runtime_json은 시장 국면 실시간 조회(동기 네트워크, 수십 초 가능)를 포함한다 —
    # 이벤트 루프에서 직접 부르면 그동안 '/'·내 자산·차트까지 전부 멈춘다(첫 로드 체감 저하).
    # 스레드로 내려 다른 엔드포인트는 즉시 응답하게 한다.
    import asyncio

    try:
        data = await asyncio.to_thread(get_runtime_json)
        return web.json_response(data)
    except Exception as e:
        logger.exception("API /api/runtime 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_snapshots(request: web.Request) -> web.Response:
    try:
        days = int(request.query.get("days", 30))
        # 파라미터 '존재'와 '빈 값'을 구분한다: account_key=(빈)은 기본 계정('')의
        # 시계열을 뜻한다 — `or None`으로 강등하면 전 계정이 무필터로 섞여
        # 10M/30만 스케일이 한 차트에 뒤엉킨 톱니가 나온다.
        raw_key = request.query.get("account_key")
        account_key = raw_key if raw_key is not None else None
        data = get_snapshots_json(days=days, account_key=account_key)
        return web.json_response(data)
    except Exception as e:
        logger.exception("API /api/snapshots 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


# 평가 결과 캐시 (TTL 60초) — 수집기가 호출마다 TradingHours를 새로 만들어
# 10초 폴링이면 INFO 로그 2줄×8,640회/일 스팸이 되고, holidays.yaml이 사라진
# 환경에서는 pykrx 네트워크 갱신이 sync-in-async 핸들러를 매 폴링 블로킹할 수
# 있다(잠재). 진행률은 하루 단위로 변하는 값이라 60초 캐시는 충분히 신선하다.
_BASKET_EVAL_CACHE: dict = {"at": 0.0, "data": None}
_BASKET_EVAL_TTL_SEC = 60.0


async def handle_api_basket_evaluation(_request: web.Request) -> web.Response:
    """바스켓 paper 운영 평가(승격 진행률) — 게이트와 같은 수집기라 판정이 동일하다.

    read-only. include_benchmark=False로 네트워크(KS11 조회)를 피한다 — 대시보드는
    10초 폴링이므로 외부 조회를 섞으면 안 된다. 결과는 60초 TTL 캐시.
    """
    import asyncio
    import time as _time

    def _collect_all() -> dict:
        from core.basket_evaluation import collect_basket_paper_evaluation
        from core.basket_rebalancer import BasketRebalancer

        out = []
        for name in BasketRebalancer.get_enabled_baskets():
            result, basket_name = collect_basket_paper_evaluation(
                include_benchmark=False, basket_name=name,
            )
            out.append({
                "basket": basket_name,
                "verdict": result.get("verdict"),
                "progress_days": result.get("progress_days"),
                "min_trading_days": result.get("min_trading_days"),
                "snapshot_coverage": result.get("snapshot_coverage"),
                "issues": result.get("issues", []),
            })
        return {"evaluations": out}

    try:
        now = _time.monotonic()
        if (
            _BASKET_EVAL_CACHE["data"] is not None
            and now - _BASKET_EVAL_CACHE["at"] < _BASKET_EVAL_TTL_SEC
        ):
            return web.json_response(_BASKET_EVAL_CACHE["data"])

        # 수집기는 TradingHours 초기화·(잠재) pykrx 갱신 등 동기 작업 — 스레드로 내려
        # 캐시 미스 시에도 이벤트 루프가 다른 요청을 계속 처리하게 한다.
        payload = await asyncio.to_thread(_collect_all)
        _BASKET_EVAL_CACHE["at"] = now
        _BASKET_EVAL_CACHE["data"] = payload
        return web.json_response(payload)
    except Exception as e:
        logger.exception("API /api/basket_evaluation 오류: {}", e)
        return web.json_response({"error": str(e)}, status=500)


def create_app() -> web.Application:
    web_mod = _require_aiohttp_web()
    app = web_mod.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/portfolio", handle_api_portfolio)
    app.router.add_get("/api/runtime", handle_api_runtime)
    app.router.add_get("/api/snapshots", handle_api_snapshots)
    app.router.add_get("/api/baskets", handle_api_baskets)
    app.router.add_post("/api/deposit", handle_api_deposit)
    app.router.add_get("/api/cash_flows", handle_api_cash_flows)
    app.router.add_get("/api/basket_evaluation", handle_api_basket_evaluation)
    return app


def _config_settings_dict(config) -> dict:
    settings = getattr(config, "settings", {})
    if callable(settings):
        settings = settings()
    return settings if isinstance(settings, dict) else {}


def resolve_dashboard_bind(host: Optional[str] = None, port: Optional[int] = None) -> tuple[str, int]:
    """대시보드 바인드 주소를 해석한다. 기본은 로컬 루프백이다."""
    try:
        cfg = Config.get()
        settings = _config_settings_dict(cfg)
        dash_cfg = (settings.get("dashboard") or {}) if isinstance(settings, dict) else {}
        host = host or str(dash_cfg.get("host") or "").strip() or DEFAULT_HOST
        port = port if port is not None else dash_cfg.get("port") or DEFAULT_PORT
    except Exception:
        host = host or DEFAULT_HOST
        port = port if port is not None else DEFAULT_PORT
    return host, int(port)


def run_web_dashboard(host: Optional[str] = None, port: Optional[int] = None):
    """웹 대시보드 서버 실행 (블로킹). host/port 미지정 시 config dashboard 섹션 또는 기본값 사용."""
    host, port = resolve_dashboard_bind(host=host, port=port)
    web_mod = _require_aiohttp_web()
    app = create_app()
    logger.info("웹 대시보드 서버 시작: http://{}:{}/", host, port)
    web_mod.run_app(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="퀀트 트레이더 웹 대시보드")
    p.add_argument("--host", default=None, help="바인드 주소 (기본: config 또는 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="포트 (기본: config 또는 8080)")
    args = p.parse_args()
    from database.models import init_database
    from monitoring.logger import setup_logger
    setup_logger()
    init_database()
    run_web_dashboard(host=args.host, port=args.port)
