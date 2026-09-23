(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);

  /* ---------- 서식 ---------- */

  const nf0 = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
  const compact = new Intl.NumberFormat("ko-KR", {
    notation: "compact",
    maximumFractionDigits: 1,
  });
  const MINUS = "−";

  // null·undefined·빈 문자열은 0이 아니라 "값 없음"으로 다룬다.
  const num = (value) => (value == null || value === "" ? NaN : Number(value));
  function won(value) {
    const n = num(value);
    return Number.isFinite(n) ? `${nf0.format(Math.round(n))}원` : "—";
  }
  function signedWon(value) {
    const n = num(value);
    if (!Number.isFinite(n)) return "—";
    const sign = n > 0 ? "+" : n < 0 ? MINUS : "";
    return `${sign}${nf0.format(Math.round(Math.abs(n)))}원`;
  }
  function compactWon(value) {
    const n = num(value);
    return Number.isFinite(n) ? `${compact.format(n)}원` : "—";
  }
  function pct(value, { sign = true, digits = 2 } = {}) {
    const n = num(value);
    if (!Number.isFinite(n)) return "—";
    const s = !sign ? "" : n > 0 ? "+" : n < 0 ? MINUS : "";
    return `${s}${Math.abs(n).toFixed(digits)}%`;
  }
  function tone(value) {
    const n = num(value);
    if (!Number.isFinite(n) || Math.abs(n) < 1e-9) return "flat";
    return n > 0 ? "up" : "down";
  }
  function parseDate(value) {
    if (!value) return null;
    if (value instanceof Date)
      return Number.isNaN(value.getTime()) ? null : new Date(value.getTime());
    const text = String(value).trim().replace(" ", "T");
    // 장부의 시간대 없는 시각은 한국 시간이다. Z·UTC 오프셋이 있으면 그대로 읽는다.
    const normalized = /^\d{4}-\d{2}-\d{2}$/.test(text)
      ? `${text}T00:00:00+09:00`
      : /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(text)
        ? `${text}+09:00`
        : text;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  const pad2 = (n) => String(n).padStart(2, "0");
  const DAY_MS = 86_400_000;
  const KST_OFFSET_MS = 9 * 60 * 60 * 1000;
  // 표시용 날짜만 +9시간 옮겨 UTC 필드로 읽는다. 시간 비교에는 원래 시각을 쓴다.
  function koreaClock(value) {
    const instant = parseDate(value);
    return instant ? new Date(instant.getTime() + KST_OFFSET_MS) : null;
  }
  function koreaDate(value) {
    return koreaClock(value)?.toISOString().slice(0, 10) || "";
  }
  function fmtLong(value) {
    const d = koreaClock(value);
    return d
      ? `${d.getUTCFullYear()}년 ${d.getUTCMonth() + 1}월 ${d.getUTCDate()}일`
      : "—";
  }
  function fmtMD(value) {
    const d = koreaClock(value);
    return d ? `${d.getUTCMonth() + 1}월 ${d.getUTCDate()}일` : "—";
  }
  function fmtDT(value) {
    const d = koreaClock(value);
    return d
      ? `${d.getUTCMonth() + 1}월 ${d.getUTCDate()}일 ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`
      : "—";
  }
  function fmtHM(value) {
    const d = koreaClock(value);
    return d ? `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}` : "—";
  }
  function formatAge(minutes) {
    if (minutes == null) return "기록 없음";
    if (minutes < 1) return "방금";
    if (minutes < 60) return `${Math.round(minutes)}분 전`;
    if (minutes < 1440) return `${Math.round(minutes / 60)}시간 전`;
    return `${Math.round(minutes / 1440)}일 전`;
  }
  function calendarAgeDays(value) {
    const date = koreaDate(value);
    if (!date) return null;
    return Math.max(
      0,
      Math.round((parseDate(koreaDate(new Date())) - parseDate(date)) / DAY_MS),
    );
  }
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const FONT =
    '"Nungum UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif';
  const COLORS = {
    ink: "#0f62fe",
    up: "#da1e28",
    down: "#0072c3",
    warn: "#a2191f",
  };
  const inkAlpha = (a) => `rgba(22, 22, 22, ${a})`;

  /* ---------- 상태 ---------- */

  const query = new URLSearchParams(window.location.search);
  const requestedDays = Number(query.get("days"));
  const allowedDays = [30, 90, 365, 3650];

  const state = {
    mode: "unknown",
    baskets: null,
    evaluations: null,
    runtime: null,
    legacy: null,
    flows: new Map(),
    flowStatus: new Map(),
    flowError: false,
    series: new Map(),
    seriesStatus: new Map(),
    chartRows: [],
    chartDays: allowedDays.includes(requestedDays) ? requestedDays : 90,
    chartAccount: query.get("account") || null,
    lastCoreSuccess: null,
    coreError: null,
    coreStatus: "loading",
    runtimeStatus: "loading",
    activeRequests: new Map(),
    chartRequest: 0,
    chartDataAccount: null,
    depositConfirming: false,
    depositRequestId: null,
  };

  const el = {
    chrome: $("chrome"),
    syncMark: $("syncMark"),
    syncStatus: $("syncStatus"),
    lastUpdate: $("lastUpdate"),
    modeBadge: $("modeBadge"),
    openDeposit: $("openDepositButton"),
    depositAvailability: $("depositAvailability"),
    decisionTitle: $("decisionTitle"),
    decisionDescription: $("decisionDescription"),
    decisionMeta: $("decisionMeta"),
    decisionAction: $("decisionAction"),
    statusRow: $("statusRow"),
    basketTracks: $("basketTracks"),
    portfolioAsOf: $("portfolioAsOf"),
    chartAccount: $("chartAccount"),
    chartSummary: $("chartSummary"),
    chartMaturity: $("chartMaturity"),
    chartWrap: $("chartWrap"),
    chartBox: $("chartBox"),
    chartEquity: $("chartEquity"),
    chartDrawdown: $("chartDrawdown"),
    chartTip: $("chartTip"),
    chartEmpty: $("chartEmpty"),
    chartRows: $("chartDataRows"),
    monthStrip: $("monthStrip"),
    basketEval: $("basketEval"),
    runtimeOps: $("runtimeOps"),
    runtimeMeta: $("runtimeMeta"),
    haltGuidance: $("haltGuidance"),
    depositDialog: $("depositDialog"),
    depositForm: $("depositForm"),
    depositFields: $("depositFields"),
    depositConfirm: $("depositConfirm"),
    depositError: $("depositError"),
    depositSubmit: $("depositSubmitButton"),
    depositBack: $("depositBackButton"),
  };

  /* ---------- 네트워크 ---------- */

  async function fetchJson(
    url,
    { timeout = 15_000, options = {}, key = url } = {},
  ) {
    const previous = state.activeRequests.get(key);
    if (previous) previous.abort();
    const controller = new AbortController();
    state.activeRequests.set(key, controller);
    const timer = window.setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      if (!response.ok)
        throw new Error(
          payload && payload.error ? payload.error : `HTTP ${response.status}`,
        );
      return payload;
    } finally {
      window.clearTimeout(timer);
      if (state.activeRequests.get(key) === controller)
        state.activeRequests.delete(key);
    }
  }

  /* ---------- 연결 상태 · 모드 ---------- */

  function setSync(kind, label) {
    el.syncMark.dataset.state = kind;
    el.syncStatus.textContent = label;
  }
  function updateSyncIndicator() {
    if (state.coreStatus === "loading") setSync("loading", "계좌 기록 확인 중");
    else if (state.coreStatus === "error")
      setSync("error", "계좌 기록 연결 실패");
    else if (state.runtimeStatus === "error")
      setSync("partial", "계좌 기록 정상 · 운영 상태 확인 불가");
    else if (state.coreStatus === "partial")
      setSync("partial", "일부 데이터 지연");
    else setSync("ok", "계좌 기록 연결 정상");
    updateDepositAvailability();
  }
  function markCoreSuccess(timestamp) {
    const parsed = parseDate(timestamp) || new Date();
    state.lastCoreSuccess = parsed;
    state.coreError = null;
    el.lastUpdate.dateTime = parsed.toISOString();
    el.lastUpdate.textContent = `${fmtHM(parsed)} 갱신`;
    el.lastUpdate.hidden = false;
  }
  function setMode(mode) {
    const normalized = String(mode || "unknown").toLowerCase();
    const nextMode = ["paper", "live"].includes(normalized)
      ? normalized
      : "unknown";
    if (state.mode !== nextMode) {
      state.flows.clear();
      state.flowStatus.clear();
      state.series.clear();
      state.seriesStatus.clear();
      state.chartDataAccount = null;
    }
    state.mode = nextMode;
    el.modeBadge.dataset.mode = state.mode;
    el.modeBadge.textContent =
      state.mode === "paper"
        ? "모의투자"
        : state.mode === "live"
          ? "실전 · 실계좌 주문"
          : "모드 확인 불가";
    updateDepositCopy();
  }
  const modeLabel = () =>
    state.mode === "live"
      ? "실전"
      : state.mode === "paper"
        ? "모의투자"
        : "모드 확인 불가";

  function canRecordDeposit() {
    const baskets = state.baskets;
    const halt = state.runtime && state.runtime.trading_halt;
    return (
      state.coreStatus === "ready" &&
      state.runtimeStatus === "ready" &&
      ["paper", "live"].includes(state.mode) &&
      Array.isArray(baskets) &&
      baskets.length > 0 &&
      baskets.every((b) => state.flowStatus.get(b.basket) === "ready") &&
      halt &&
      halt.halted === false
    );
  }
  function updateDepositAvailability() {
    const available = canRecordDeposit();
    el.openDeposit.disabled = !available;
    el.depositAvailability.textContent = available
      ? "적립을 기록할 수 있습니다."
      : "계좌 기록과 거래 상태를 확인한 뒤 기록할 수 있습니다.";
  }

  /* ---------- 바스켓 정렬 ---------- */

  function sortedBaskets() {
    return [...(state.baskets || [])].sort((a, b) => {
      const d = Number(Boolean(b.is_primary)) - Number(Boolean(a.is_primary));
      return d || String(a.basket).localeCompare(String(b.basket));
    });
  }
  const primaryBasket = () =>
    sortedBaskets().find((b) => b.is_primary) ||
    sortedBaskets().find((b) => b.snapshot) ||
    null;

  /* ---------- 시계열 계산 ---------- */

  function rowInstant(row) {
    const d = parseDate(row.date);
    if (!d) return null;
    const created = parseDate(row.created_at);
    // 과거 날짜를 나중에 복원한 행의 저장 시각으로 미래 입금을 당겨 넣지 않는다.
    if (created && koreaDate(created) === koreaDate(d)) return created;
    return new Date(`${koreaDate(d)}T23:59:59.999+09:00`);
  }

  /** 스냅샷·입금 기록으로 평가금액, 투자원금(계단), 시간가중 자산, 낙폭 시계열을 만든다. */
  function buildSeries(rows, flows, initialCapital) {
    if (
      rows.some(
        (r) =>
          !parseDate(r.date) ||
          !Number.isFinite(num(r.total_value)) ||
          num(r.total_value) < 0 ||
          !Number.isFinite(num(r.cumulative_return)) ||
          num(r.cumulative_return) < -100,
      )
    )
      throw new Error("날짜 또는 계좌 금액이 올바르지 않습니다.");
    if (
      new Set(rows.map((r) => String(r.date).slice(0, 10))).size !== rows.length
    )
      throw new Error("같은 날짜의 계좌 기록이 중복됐습니다.");
    const sorted = [...rows]
      .filter((r) => parseDate(r.date))
      .sort((a, b) => parseDate(a.date) - parseDate(b.date));
    const dates = sorted.map((r) => parseDate(r.date));
    const value = sorted.map((r) => Number(r.total_value));
    const cr = sorted.map((r) => Number(r.cumulative_return));
    const twr = cr.map((c) => initialCapital * (1 + c / 100));
    const sortedFlows = [...(flows || [])]
      .map((f) => ({
        at: parseDate(f.occurred_at),
        amount: Number(f.amount || 0),
        note: f.note || "",
      }))
      .filter((f) => f.at)
      .sort((a, b) => a.at - b.at);
    const instants = sorted.map(rowInstant);
    let flowIndex = 0;
    let principalAt = initialCapital;
    const principal = instants.map((at) => {
      while (
        flowIndex < sortedFlows.length &&
        at &&
        sortedFlows[flowIndex].at <= at
      ) {
        principalAt += sortedFlows[flowIndex++].amount;
      }
      return principalAt;
    });
    // 계좌 기록과 같은 규칙: 시작 자본을 첫 고점으로 두고 시간가중 자산으로 낙폭을 잰다.
    let peak = initialCapital > 0 ? initialCapital : -Infinity;
    let peakIdx = -1;
    let maxDD = 0;
    let maxDDIdx = 0;
    let maxDDPeakIdx = -1;
    const dd = twr.map((v, i) => {
      if (v > peak) {
        peak = v;
        peakIdx = i;
      }
      const d = peak > 0 ? v / peak - 1 : 0;
      if (d < maxDD) {
        maxDD = d;
        maxDDIdx = i;
        maxDDPeakIdx = peakIdx;
      }
      return d;
    });
    const markers = sortedFlows
      .map((f) => ({
        ...f,
        index: instants.findIndex((at) => at && at >= f.at),
      }))
      .filter((m) => m.index >= 0);
    return {
      rows: sorted,
      dates,
      value,
      cr,
      twr,
      principal,
      dd,
      maxDD,
      maxDDIdx,
      maxDDPeakIdx,
      markers,
      initialCapital,
    };
  }

  function monthlyReturns(series) {
    const out = [];
    let prevIndex = null;
    let current = null;
    const currentYear = koreaDate(new Date()).slice(0, 4);
    series.dates.forEach((d, i) => {
      const key = koreaDate(d).slice(0, 7);
      if (!current || current.key !== key) {
        if (current) out.push(current);
        current = {
          key,
          label: `${key.slice(0, 4) !== currentYear ? `${key.slice(2, 4)}년 ` : ""}${Number(key.slice(5, 7))}월`,
          startIndex: prevIndex,
          endIndex: i,
        };
      }
      current.endIndex = i;
      prevIndex = i;
    });
    if (current) out.push(current);
    // 월 수익률 = 전월 말 시간가중 지수 대비. 첫 달은 시작 자본(지수 1) 대비.
    return out.map((m) => {
      const start =
        m.startIndex == null ? 1 : 1 + series.cr[m.startIndex] / 100;
      const end = 1 + series.cr[m.endIndex] / 100;
      return {
        key: m.key,
        label: m.label,
        value: start > 0 ? (end / start - 1) * 100 : 0,
      };
    });
  }

  function makeScale(values, top, bottom, padRatio = 0.08) {
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
      min = 0;
      max = 1;
    }
    const spread = Math.max(1, max - min);
    min -= spread * padRatio;
    max += spread * padRatio;
    return {
      min,
      max,
      y: (v) => top + ((max - v) / (max - min)) * (bottom - top),
    };
  }

  function setupCanvas(canvas, width, height) {
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(width));
    const h = Math.max(1, Math.round(height));
    if (
      canvas.width !== Math.round(w * ratio) ||
      canvas.height !== Math.round(h * ratio)
    ) {
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(h * ratio);
    }
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, w, h);
    return context;
  }

  function niceTicks(min, max, count) {
    const ticks = [];
    for (let i = 0; i <= count; i += 1)
      ticks.push(max - ((max - min) * i) / count);
    return ticks;
  }

  /* 선택한 계좌와 차트·종목표는 같은 데이터를 표시한다. */
  function selectedBasket() {
    return (
      (state.baskets || []).find((b) => b.account_key === state.chartAccount) ||
      primaryBasket()
    );
  }
  function updateOverview() {
    const baskets = sortedBaskets();
    const b = selectedBasket();
    const tabs = $("accountTabs");
    const signature = JSON.stringify(
      baskets.map((item) => [
        item.account_key,
        item.display_name,
        item.is_primary,
      ]),
    );
    if (tabs.dataset.signature !== signature) {
      tabs.dataset.signature = signature;
      tabs.innerHTML =
        baskets
          .map(
            (item) =>
              `<button type="button" data-account="${escapeHtml(item.account_key)}" aria-pressed="false"><span class="account-icon" aria-hidden="true">${item.is_primary ? "적립" : "주식"}</span><span>${escapeHtml(item.display_name)}<small>${item.is_primary ? "매월 적립하는 계좌" : "국내 대형주 분산 투자"}</small></span><span class="account-check" aria-hidden="true">✓</span></button>`,
          )
          .join("") || '<p class="loading">등록된 계좌가 없습니다.</p>';
    }
    tabs
      .querySelectorAll("button")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.account === b?.account_key),
        ),
      );
    $("overview").setAttribute("aria-busy", String(!state.baskets));
    const snap = b?.snapshot;
    $("overviewDate").textContent = snap
      ? `${fmtLong(snap.date)} 기준`
      : "자산 기록 없음";
    $("overviewDate").dateTime = snap?.date || "";
    $("overviewTotal").textContent = won(snap?.total_value);
    $("overviewCash").textContent = `현금 ${won(snap?.cash)}`;
    // 평가액은 스냅샷을 찍은 때의 값이라 원금도 그때 기준으로 비교한다. 방금 기록한
    // 적립금까지 원금에 넣으면 다음 실행 전까지 손실이 난 것처럼 보인다.
    const basePrincipal = Number(b?.principal_at_snapshot ?? b?.principal);
    const pending = Number(b?.pending_deposits || 0);
    const pnl = snap ? Number(snap.total_value) - basePrincipal : null;
    $("overviewPnl").textContent = snap
      ? `원금 대비 ${signedWon(pnl)} (${pct(basePrincipal > 0 ? (pnl / basePrincipal) * 100 : null)})`
      : "모의투자를 실행하면 자산 기록이 표시됩니다.";
    $("overviewPnl").className = tone(pnl);
    $("overviewPrincipal").textContent = won(b?.principal);
    $("overviewDeposits").textContent = b?.deposits_total
      ? `적립금 ${won(b.deposits_total)} 포함${pending > 0 ? ` · 최근 적립한 ${won(pending)}은 다음 자동매매 때 반영됩니다` : ""}`
      : "초기 투자금";
    $("overviewReturn").textContent = pct(snap?.cumulative_return);
    $("overviewReturn").className = tone(snap?.cumulative_return);
    const rows = state.series.get(b?.account_key) || [];
    const dd = snap
      ? Math.max(
          Math.abs(Number(snap.mdd) || 0),
          ...rows.map((r) => Math.abs(Number(r.mdd) || 0)),
        )
      : null;
    $("overviewDrawdown").textContent = pct(dd == null ? null : -dd);
    $("overviewDrawdown").className = dd ? "down" : "flat";
    const cashPct =
      snap && snap.total_value > 0
        ? clamp((snap.cash / snap.total_value) * 100, 0, 100)
        : null;
    $("allocationCash").textContent =
      cashPct == null ? "—" : `${cashPct.toFixed(1)}%`;
    $("allocationRing").style.setProperty(
      "--invested",
      `${cashPct == null ? 0 : 100 - cashPct}%`,
    );
    $("allocationRing").setAttribute(
      "aria-label",
      cashPct == null
        ? "자산 구성 기록 없음"
        : `보유 자산 ${(100 - cashPct).toFixed(1)}%, 현금 ${cashPct.toFixed(1)}%`,
    );
    renderRiskExplanation(b);
    $("allocationList").innerHTML =
      `<div><dt><i class="asset-dot"></i>보유 자산</dt><dd>${won(snap ? snap.total_value - snap.cash : null)}</dd></div><div><dt><i class="cash-dot"></i>현금</dt><dd>${won(snap?.cash)}</dd></div>`;
  }

  function renderRiskExplanation(basket) {
    const target = $("riskExplanation");
    const overlay = basket?.overlay;
    if (!basket) {
      target.innerHTML =
        '<p class="loading">계좌 기록을 불러오는 중입니다.</p>';
      return;
    }
    if (!overlay?.enabled) {
      target.innerHTML = `<div class="risk-scale"><strong>${Math.round((basket.design_fraction || 0) * 100)}%</strong><span>보유 자산 목표 비중</span></div><p class="fine">설정한 비중을 유지합니다. 추세와 낙폭에 따른 자동 축소는 사용하지 않습니다.</p>`;
      return;
    }
    const riskWeights = Object.entries(basket.target_weights || {})
      .filter(([symbol]) => symbol !== overlay.defensive_symbol)
      .reduce((sum, [, weight]) => sum + weight, 0);
    const issues = overlay.data_issues || [];
    const trend =
      overlay.trend_below == null
        ? "판단 기록 확인 필요"
        : overlay.trend_below
          ? "장기 추세 아래 · 비중 축소"
          : "장기 추세 기준 유지";
    const dd =
      overlay.drawdown_active == null
        ? "판단 기록 확인 필요"
        : overlay.drawdown_active
          ? "낙폭 기준 도달 · 비중 축소"
          : "낙폭 기준 이내";
    target.innerHTML = `<div class="risk-scale"><strong>${(riskWeights * 100).toFixed(1)}%</strong><span>주식 목표 비중</span></div><dl class="risk-facts"><div><dt>추세 판단</dt><dd>${escapeHtml(trend)}</dd></div><div><dt>손실 관리</dt><dd>${escapeHtml(dd)}</dd></div><div><dt>비중을 줄이면</dt><dd>${overlay.defensive_symbol ? "줄인 금액은 CD금리 ETF에 배분" : "현금으로 보유"}</dd></div></dl>${issues.length ? `<p class="fine warn">${escapeHtml(issues.join(" / "))}</p>` : ""}${overlay.evaluated_at ? `<time datetime="${escapeHtml(overlay.evaluated_at)}">${escapeHtml(fmtDT(overlay.evaluated_at))} 판단</time>` : '<p class="fine">첫 판단 기록을 기다리고 있습니다.</p>'}`;
  }

  function latestSnapshotDate() {
    const b = selectedBasket();
    return b && b.snapshot ? b.snapshot.date : null;
  }
  function currentMonthContributionState(basket) {
    if (!basket || !basket.contribution_plan?.enabled) return "not-planned";
    if (state.flowStatus.get(basket.basket) !== "ready") return "unknown";
    const month = koreaDate(new Date()).slice(0, 7);
    const recorded = (state.flows.get(basket.basket) || []).some((flow) => {
      return koreaDate(flow.occurred_at).slice(0, 7) === month;
    });
    return recorded ? "recorded" : "empty";
  }
  function runtimeAgeMinutes() {
    const parsed = parseDate(
      state.runtime && state.runtime.runtime_file_updated_at,
    );
    return parsed
      ? Math.max(0, Math.round((Date.now() - parsed.getTime()) / 60_000))
      : null;
  }
  function setDecision({
    title,
    description,
    action = null,
    actionLabel = "확인하기",
  }) {
    el.decisionTitle.textContent = title;
    el.decisionDescription.textContent = description;
    el.decisionAction.hidden = !action;
    el.decisionAction.dataset.action = action || "";
    el.decisionAction.textContent = actionLabel;
    const urgent = ["operations", "retry"].includes(action);
    $("today").dataset.state = urgent ? "alert" : action ? "info" : "quiet";
    $("today").querySelector(".notice-symbol").textContent = urgent ? "!" : "·";
    $("priorityNotice").hidden = !urgent;
    $("priorityNotice").textContent = `${title} · ${actionLabel} →`;
  }
  function renderDecision() {
    const baskets = state.baskets;
    const halt = state.runtime && state.runtime.trading_halt;
    const latest = latestSnapshotDate();
    const primary = selectedBasket();
    const evaluation = (state.evaluations || []).find(
      (item) => item.basket === primary?.basket,
    );
    // 검증 중 상시 안내(review_note)는 '확인할 항목'과 섞지 않는다 — 섞여 있으면
    // 이번 달 적립금 미기록 같은 실제 할 일이 가려진다.
    const reviewNote = evaluation?.review_note || null;
    const issues = (evaluation?.issues || []).filter((item) => item !== reviewNote);
    const contribution = currentMonthContributionState(primary);
    el.decisionMeta.textContent = `${modeLabel()}${latest ? ` · 마지막 자산 기록 ${fmtLong(latest)}` : " · 자산 기록 없음"}`;

    if (halt && halt.halted) {
      setDecision({
        title: "거래가 중지된 상태입니다",
        description:
          halt.reason ||
          "체결과 계좌 기록을 대조하기 전까지 신규 주문이 막혀 있습니다.",
        action: "operations",
        actionLabel: "운영 상태 보기",
      });
      return;
    }
    if (state.coreError) {
      setDecision({
        title: "계좌 기록을 읽지 못했습니다",
        description:
          "화면의 숫자는 마지막으로 성공한 조회 결과입니다. 연결을 확인한 뒤 다시 시도하세요.",
        action: "retry",
        actionLabel: "다시 확인",
      });
      return;
    }
    if (state.runtimeStatus === "loading") {
      setDecision({
        title: "운영 상태를 확인하고 있습니다",
        description: "최근 실행 기록과 거래 중지 여부를 조회하고 있습니다.",
      });
      return;
    }
    if (state.runtimeStatus === "error") {
      setDecision({
        title: "운영 상태를 확인할 수 없습니다",
        description:
          "거래 중지 여부를 확인할 수 없어 적립금 기록을 잠시 제한했습니다.",
        action: "retry",
        actionLabel: "다시 확인",
      });
      return;
    }
    if (baskets && !baskets.length) {
      setDecision({
        title: "첫 모의투자 계좌를 켜 주세요",
        description:
          "config/baskets.yaml에서 계좌를 활성화하고 모의투자를 한 번 실행하면 첫 기록이 생깁니다.",
        action: "portfolio",
        actionLabel: "포트폴리오 보기",
      });
      return;
    }
    if (baskets && baskets.length && !latest) {
      setDecision({
        title: "첫 자산 기록을 기다리고 있습니다",
        description:
          "모의투자를 한 번 실행하면 투자원금, 총자산, 수익률이 분리되어 보입니다.",
        action: "portfolio",
        actionLabel: "포트폴리오 보기",
      });
      return;
    }
    // 기록이 빠졌는지는 거래일 기준으로 센다(서버에서 계산). 그냥 날짜로 세면 연휴 뒤에는
    // 잘못된 경보가 뜨고, 평일에 이틀 빠져도 '정상'으로 나온다.
    const stalled = (baskets || []).filter(
      (b) => Number(b.missed_trading_days || 0) >= 1,
    );
    if (stalled.length) {
      const worst = stalled.reduce((a, b) =>
        Number(b.missed_trading_days) > Number(a.missed_trading_days) ? b : a,
      );
      setDecision({
        title: `자산 기록이 ${worst.missed_trading_days}거래일 빠졌습니다`,
        description: `${worst.display_name || worst.basket}의 마지막 기록은 ${fmtLong(worst.snapshot?.date)}입니다. 자동매매가 제대로 실행됐는지 운영 상태부터 확인하세요.`,
        action: "operations",
        actionLabel: "운영 상태 보기",
      });
      return;
    }
    if (state.runtime && state.runtime.scheduler_stale) {
      setDecision({
        title: "장이 열려 있는데 자동매매가 멈춰 있습니다",
        description:
          "스케줄러가 한 시간 넘게 실행되지 않았습니다. 운영 상태를 확인하세요.",
        action: "operations",
        actionLabel: "운영 상태 보기",
      });
      return;
    }
    if (issues.length) {
      setDecision({
        title: `확인이 필요한 항목이 ${issues.length}건 있습니다`,
        description: issues[0],
        action: "review",
        actionLabel: "검토 항목 보기",
      });
      return;
    }
    if (contribution === "unknown") {
      setDecision({
        title: "적립금 기록을 확인할 수 없습니다",
        description:
          "조회가 끝나기 전에는 같은 적립금을 두 번 기록하지 않도록 기다려 주세요.",
        action: "retry",
        actionLabel: "다시 확인",
      });
      return;
    }
    if (contribution === "empty") {
      const planned = Number(primary.contribution_plan?.amount || 0);
      setDecision({
        title: "이번 달 적립금 기록이 없습니다",
        description: `${planned > 0 ? `매월 ${compactWon(planned)}을 적립하는 계좌입니다. ` : ""}${state.mode === "live" ? "입금을 마쳤다면 같은 금액을 기록해 주세요." : "추가할 모의투자금을 기록해 주세요."}`,
        action: "deposit",
        actionLabel: "적립금 기록",
      });
      return;
    }
    if (state.evaluations === null) {
      setDecision({
        title: "검증 결과를 불러오지 못했습니다",
        description:
          "계좌 기록은 정상입니다. 모의투자 검증 결과만 불러오지 못했으니 잠시 후 다시 확인하세요.",
      });
      return;
    }
    if (reviewNote) {
      setDecision({
        title: "새 운용 규칙을 검증하고 있습니다",
        description: reviewNote,
        action: "review",
        actionLabel: "검토 항목 보기",
      });
      return;
    }
    setDecision({
      title: "현재 확인할 항목이 없습니다",
      description: "최근 계좌 기록과 자동매매 상태가 정상입니다.",
    });
  }

  function statusItem(key, value, stateName) {
    return `<li><i class="dot" data-state="${stateName}" aria-hidden="true"></i><span class="k">${escapeHtml(key)}</span><span class="v">${escapeHtml(value)}</span></li>`;
  }

  /* ---------- 포트폴리오 ---------- */

  function sparkline(rows) {
    const values = (rows || [])
      .slice(-60)
      .map((r) => Number(r.total_value || 0))
      .filter(Number.isFinite);
    if (values.length < 2) return "";
    const w = 180;
    const h = 52;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = Math.max(1, max - min);
    const px = (i) => (i / (values.length - 1)) * w;
    const py = (v) => 6 + ((max - v) / span) * (h - 12);
    const d = values
      .map(
        (v, i) =>
          `${i === 0 ? "M" : "L"}${px(i).toFixed(1)} ${py(v).toFixed(1)}`,
      )
      .join(" ");
    const last = values[values.length - 1];
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="최근 ${values.length}거래일 평가금액 추이" role="img"><path d="${d}" fill="none" stroke="#0d0c0b" stroke-width="1.25" vector-effect="non-scaling-stroke"/><circle cx="${w}" cy="${py(last).toFixed(1)}" r="2.5" fill="#0d0c0b"/></svg>`;
  }

  function holdingsTable(basket) {
    const positions = basket.positions || [];
    if (!positions.length)
      return '<p class="flow">아직 보유 종목이 없습니다.</p>';
    const total = basket.snapshot ? Number(basket.snapshot.total_value) : null;
    const targets = basket.target_weights || {};
    const rows = positions.map((p) => ({
      ...p,
      weight: total ? Number(p.invested || 0) / total : null,
      target: targets[p.symbol] == null ? null : Number(targets[p.symbol]),
    }));
    const maxW = Math.max(
      0.01,
      ...rows.map((r) => Math.max(r.weight || 0, r.target || 0)),
    );
    const barWidth = (w) => `${Math.round(((w || 0) / maxW) * 100)}%`;
    const holdingsValue =
      basket.holdings_value == null ? null : Number(basket.holdings_value);
    const holdingsCost =
      basket.holdings_cost == null ? null : Number(basket.holdings_cost);
    const holdingsPnl =
      holdingsValue == null || holdingsCost == null
        ? null
        : holdingsValue - holdingsCost;
    return `<div class="table-scroll" tabindex="0" role="region" aria-label="${escapeHtml(basket.display_name)} 보유 종목"><table class="holdings">
      <caption>${escapeHtml(basket.display_name)} 보유 종목과 비중</caption>
      <thead><tr><th scope="col">종목</th><th scope="col" class="num">수량</th><th scope="col" class="num">평단가</th><th scope="col" class="num">매입금액</th><th scope="col">매입 비중</th><th scope="col">목표 비중</th></tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr>
        <td>${escapeHtml(r.name || r.symbol)}<span class="sym">${escapeHtml(r.symbol)}</span></td>
        <td class="num">${nf0.format(Number(r.quantity || 0))}주</td>
        <td class="num">${won(r.avg_price)}</td>
        <td class="num">${won(r.invested)}</td>
        <td><span class="wbar"><span>${r.weight == null ? "—" : pct(r.weight * 100, { sign: false, digits: 1 })}</span><i style="width:${barWidth(r.weight)}"></i></span></td>
        <td><span class="wbar"><span>${r.target == null ? "—" : pct(r.target * 100, { sign: false, digits: 1 })}</span><i class="t" style="width:${barWidth(r.target)}"></i></span></td>
      </tr>`,
        )
        .join("")}</tbody>
      <tfoot><tr><td colspan="6">보유분 평가금액 ${won(holdingsValue)} · 매입금액 ${won(holdingsCost)} · 평가손익 <span class="${tone(holdingsPnl)}">${signedWon(holdingsPnl)}</span>${holdingsCost && holdingsPnl != null ? ` (${pct((holdingsPnl / holdingsCost) * 100)})` : ""}</td></tr></tfoot>
    </table></div>
    <p class="fine">현재가는 계좌 기록에 저장하지 않으므로 종목별 비중은 매입금액 기준이고, 보유분 평가금액은 총자산에서 현금을 뺀 값입니다.</p>`;
  }

  function renderBasketTracks(data) {
    const baskets = (data && data.baskets) || [];
    state.baskets = baskets;
    el.basketTracks.setAttribute("aria-busy", "false");
    setMode(data && data.mode);
    markCoreSuccess(data && data.timestamp);

    if (!baskets.length) {
      el.basketTracks.innerHTML =
        '<div class="empty"><strong>활성화된 계좌가 없습니다.</strong><span>config/baskets.yaml에서 모의투자 계좌를 먼저 켜 주세요.</span></div>';
      el.portfolioAsOf.textContent = "기준일 없음";
      el.portfolioAsOf.dateTime = "";
      updateOverview();
      return;
    }

    el.basketTracks.innerHTML = sortedBaskets()
      .filter((b) => b.basket === selectedBasket()?.basket)
      .map((b) => {
        const snap = b.snapshot;
        const total = snap ? Number(snap.total_value) : null;
        const principal = Number(b.principal_at_snapshot ?? b.principal ?? 0);
        const profit = total == null ? null : total - principal;
        const cr = snap ? Number(snap.cumulative_return) : null;
        const cashRatio =
          snap && total > 0 ? (Number(snap.cash) / total) * 100 : null;
        const deployment =
          b.deployment_ratio == null ? null : Number(b.deployment_ratio) * 100;
        const target =
          b.design_fraction == null ? null : Number(b.design_fraction) * 100;
        // 계좌 기록의 mdd는 그날의 고점 대비 낙폭이므로, 최대낙폭은 기록 전체에서 가장 깊었던 값을 고른다.
        const maxDD = Math.max(
          Number(snap ? snap.mdd : 0) || 0,
          ...(state.series.get(b.account_key) || []).map(
            (r) => Number(r.mdd) || 0,
          ),
        );
        const gap =
          deployment == null || target == null ? null : target - deployment;
        const primary = Boolean(b.is_primary);
        const plan = b.contribution_plan || {};
        const kind = primary
          ? `<b>주력</b>${plan.enabled && Number(plan.amount) > 0 ? ` · 매월 ${won(plan.amount)} 적립` : ""}`
          : `<b>관찰</b> · ${escapeHtml(b.purpose || "장기 관찰용")}`;
        const flows = state.flows.get(b.basket) || [];
        const latestFlow = flows[0];
        const flowState = state.flowStatus.get(b.basket);
        const note =
          gap != null && gap > 1
            ? primary
              ? `목표보다 ${Math.round(gap)}%p 낮습니다. 1주 단위 매수와 최소 주문금액의 영향을 확인하세요.`
              : `목표보다 ${Math.round(gap)}%p 낮습니다. 설정한 허용 범위를 벗어나면 비중을 조정합니다.`
            : "목표 범위 안에서 운용 중입니다.";
        const overlay = b.overlay;
        const baseTarget =
          b.base_stock_fraction == null
            ? null
            : Number(b.base_stock_fraction) * 100;
        const overlayLine =
          overlay && overlay.enabled
            ? `<p class="overlay ${overlay.data_issues && overlay.data_issues.length ? "warn" : ""}"><b>위험 조절</b> ${escapeHtml(overlay.summary || "")}${baseTarget != null && target != null && Math.abs(baseTarget - target) > 0.5 ? ` · 설계 ${Math.round(baseTarget)}% → 적용 ${Math.round(target)}%` : ""}${overlay.evaluated_at ? ` · ${escapeHtml(fmtDT(overlay.evaluated_at))} 판단` : ""}</p>`
            : "";
        const width = deployment == null ? 0 : clamp(deployment, 0, 100);
        return `<article class="track" data-primary="${primary}">
        <header class="track-head">
          <div>
            <p class="track-kind">${kind}</p>
            <h3 class="track-name">${escapeHtml(b.display_name)}</h3>

          </div>
          ${sparkline(state.series.get(b.account_key))}
        </header>
        <div class="alloc">
          <div class="alloc-line"><span>투자 비중</span><b>${deployment == null ? "—" : `${Math.round(deployment)}%`}</b><span class="target">목표 ${target == null ? "—" : `${Math.round(target)}%`}</span></div>
          <div class="bar" role="meter" aria-label="${escapeHtml(b.display_name)} 투자 비중" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(width)}"><i style="width:${width}%" data-width="${width}"></i>${target == null ? "" : `<em style="left:${clamp(target, 0, 100)}%" aria-hidden="true"></em>`}</div>
          <p class="note">${escapeHtml(note)}</p>
          ${overlayLine}
        </div>
        ${holdingsTable(b)}
        ${latestFlow ? `<p class="flow">최근 적립 ${escapeHtml(fmtMD(latestFlow.occurred_at))} · ${escapeHtml(signedWon(latestFlow.amount))}${latestFlow.note ? ` · ${escapeHtml(latestFlow.note)}` : ""}</p>` : ""}
        ${flowState === "error" ? '<p class="flow error">적립금 기록을 읽지 못했습니다. 다시 확인하기 전까지 새 기록을 추가하지 마세요.</p>' : ""}
      </article>`;
      })
      .join("");

    const reference = selectedBasket();
    const referenceDate =
      reference && reference.snapshot && reference.snapshot.date;
    el.portfolioAsOf.dateTime = referenceDate || "";
    el.portfolioAsOf.textContent = referenceDate
      ? `${fmtLong(referenceDate)} 기록 기준`
      : "첫 기록 대기";
    updateOverview();
  }

  async function refreshFlows() {
    const baskets = state.baskets || [];
    const results = await Promise.all(
      baskets.map(async (b) => {
        state.flowStatus.set(b.basket, "loading");
        try {
          const data = await fetchJson(
            `/api/cash_flows?basket=${encodeURIComponent(b.basket)}`,
            { timeout: 10_000, key: `flows:${b.basket}` },
          );
          state.flows.set(b.basket, (data && data.flows) || []);
          state.flowStatus.set(b.basket, "ready");
          return true;
        } catch {
          state.flowStatus.set(b.basket, "error");
          return false;
        }
      }),
    );
    state.flowError = results.some((ok) => !ok);
    return !state.flowError;
  }

  async function refreshSeries() {
    const baskets = state.baskets || [];
    await Promise.all(
      baskets.map(async (b) => {
        try {
          const data = await fetchJson(
            `/api/snapshots?days=3650&account_key=${encodeURIComponent(b.account_key)}`,
            { timeout: 15_000, key: `series:${b.account_key}` },
          );
          state.series.set(b.account_key, (data && data.snapshots) || []);
          state.seriesStatus.set(b.account_key, "ready");
        } catch {
          state.seriesStatus.set(b.account_key, "error");
        }
      }),
    );
  }

  /* ---------- 성과 ---------- */

  const chart = {
    progress: 1,
    frame: null,
    hover: null,
    geometry: null,
    series: null,
    view: "ribbon",
    perspective: 1,
    selected: null,
    paintFrame: null,
    tableSeries: null,
    tablePage: null,
    tableRenderedPage: null,
  };

  function ensureChartAccountOptions() {
    const wanted = sortedBaskets().map((b) => ({
      value: b.account_key,
      label: b.display_name,
    }));
    const signature = JSON.stringify(wanted);
    if (el.chartAccount.dataset.signature === signature) return;
    const previous = state.chartAccount;
    el.chartAccount.dataset.signature = signature;
    el.chartAccount.innerHTML = wanted
      .map(
        (o) =>
          `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`,
      )
      .join("");
    const exists =
      previous !== null && wanted.some((o) => o.value === previous);
    el.chartAccount.value = exists
      ? previous
      : wanted[0]
        ? wanted[0].value
        : "";
    state.chartAccount = el.chartAccount.value;
  }
  function syncChartQuery() {
    const params = new URLSearchParams(window.location.search);
    params.set("days", String(state.chartDays));
    if (state.chartAccount) params.set("account", state.chartAccount);
    else params.delete("account");
    const qs = params.toString();
    history.replaceState(
      null,
      "",
      `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`,
    );
  }
  function chartBasket() {
    return (
      (state.baskets || []).find((b) => b.account_key === state.chartAccount) ||
      null
    );
  }
  function renderChartMaturity() {
    const basket = chartBasket();
    if (!basket) {
      el.chartMaturity.textContent =
        "이전 기본 계좌는 모의투자 검증과 별개입니다.";
      return;
    }
    const evaluation = (state.evaluations || []).find(
      (item) => item.basket === basket.basket,
    );
    if (!evaluation) {
      el.chartMaturity.textContent = "";
      return;
    }
    const days = Number(evaluation.progress_days || 0);
    const minimum = Math.max(1, Number(evaluation.min_trading_days || 60));
    if (evaluation.paper_only) {
      el.chartMaturity.textContent = `운영 기록 ${days}거래일 · 새 규칙 검증 중`;
      return;
    }
    el.chartMaturity.textContent =
      days < 20
        ? `운용 ${days}거래일째 · 추세를 판단하기엔 이릅니다`
        : days < minimum
          ? `모의투자 ${days} / ${minimum}거래일`
          : `모의투자 ${days}거래일 기록`;
  }

  function renderChartTable(series) {
    if (!$("historyDetails").open || !series.rows.length) return;
    const lastPage = Math.ceil(series.rows.length / 100) - 1;
    const page = clamp(chart.tablePage ?? lastPage, 0, lastPage);
    if (chart.tableSeries === series && chart.tableRenderedPage === page)
      return;
    const start = page * 100;
    const end = Math.min(start + 100, series.rows.length);
    el.chartRows.innerHTML = series.rows
      .slice(start, end)
      .map(
        (row, offset) => `<tr>
      <td><time datetime="${escapeHtml(String(row.date).slice(0, 10))}">${escapeHtml(fmtLong(row.date))}</time></td>
      <td class="num">${won(row.total_value)}</td>
      <td class="num">${won(series.principal[start + offset])}</td>
      <td class="num ${tone(row.cumulative_return)}">${pct(row.cumulative_return)}</td>
    </tr>`,
      )
      .join("");
    chart.tableSeries = series;
    chart.tableRenderedPage = page;
    $("historyTablePosition").textContent =
      `${start + 1}–${end} / ${series.rows.length}개 기록`;
    $("historyTablePrev").disabled = page === 0;
    $("historyTableNext").disabled = page === lastPage;
  }

  function moveHistoryTable(direction) {
    if (!chart.series || $("historyDetails").hidden) return;
    const lastPage = Math.ceil(chart.series.rows.length / 100) - 1;
    const currentPage = clamp(chart.tablePage ?? lastPage, 0, lastPage);
    chart.tablePage = clamp(currentPage + direction, 0, lastPage);
    // 마지막 페이지는 새 기록이 추가돼도 최근 날짜를 계속 보여 준다.
    if (chart.tablePage === lastPage) chart.tablePage = null;
    renderChartTable(chart.series);
    $("historyTableScroll").scrollTop = 0;
  }

  function clearChartDetails() {
    $("historyDetails").hidden = true;
    el.chartRows.innerHTML = "";
    chart.tableSeries = null;
    chart.tableRenderedPage = null;
  }

  function renderMonthStrip(series, history) {
    const visibleMonths = new Set(
      series.dates.map((d) => koreaDate(d).slice(0, 7)),
    );
    // 조회 기간이 월 중간에서 시작해도 전월 말 기준 수익률을 사용한다.
    const months = monthlyReturns(history).filter((month) =>
      visibleMonths.has(month.key),
    );
    el.monthStrip.innerHTML = months
      .map(
        (m) =>
          `<div><b>${escapeHtml(m.label)}</b><span class="${tone(m.value)}">${pct(m.value, { digits: 1 })}</span></div>`,
      )
      .join("");
  }

  // 같은 기록을 입체와 평면으로 표현한다. 깊이는 별도 지표가 아니다.
  // 최대 420개 꼭짓점으로 표현하되 원본 기록·날짜 선택·다운로드는 생략하지 않는다.
  function drawChart() {
    if (!chart.series || el.chartWrap.hidden) return;
    const series = chart.series;
    const n = series.rows.length;
    if (!n) return;
    const width = Math.max(1, el.chartBox.clientWidth);
    const height = Math.max(180, el.chartBox.clientHeight);
    const ctx = setupCanvas(el.chartEquity, width, height);
    const p = chart.perspective;
    const small = width < 480;
    const dx = (small ? 23 : 45) * p;
    const dy = (small ? 42 : 65) * p;
    const left = small ? 37 : 57;
    const right = small ? 26 : 42;
    const plotWidth = Math.max(1, width - left - right - dx);
    const max = Math.max(0, ...series.cr);
    const min = Math.min(0, ...series.cr);
    const spread = Math.max(2, max - min);
    const top = 36 + dy;
    const floor = height - 44;
    const span = Math.max(50, floor - top - 25 * p);
    const x = (i) => left + (plotWidth * i) / Math.max(1, n - 1);
    const y = (v, i = 0) =>
      top +
      ((max + spread * 0.15 - v) / (spread * 1.3)) * span +
      (22 * p * i) / Math.max(1, n - 1);
    chart.geometry = {
      width,
      height,
      n,
      x,
      padding: { left, right: right + dx },
      scale: { y },
      dx,
      dy,
    };
    ctx.font = `400 ${small ? 11 : 12}px ${FONT}`;
    ctx.lineWidth = 1;
    ctx.textBaseline = "middle";
    // 그리드와 기준선도 같은 투영을 사용한다. 0%가 손익의 기준이다.
    const grid = [min, (min + max) / 2, max];
    grid.forEach((v, index) => {
      if (index > 0 && Math.abs(v - grid[index - 1]) < 0.01) return;
      ctx.beginPath();
      ctx.moveTo(left, y(v));
      ctx.lineTo(x(n - 1), y(v, n - 1));
      ctx.lineTo(x(n - 1) + dx, y(v, n - 1) - dy);
      ctx.strokeStyle = "#a6c8ff44";
      ctx.stroke();
      ctx.fillStyle = "#d0e2ff";
      ctx.textAlign = "right";
      ctx.fillText(`${v.toFixed(0)}%`, left - 10, y(v));
    });
    for (let t = 0; t <= 6; t++) {
      const i = ((n - 1) * t) / 6;
      ctx.beginPath();
      ctx.moveTo(x(i), floor);
      ctx.lineTo(x(i) + dx, floor - dy);
      ctx.strokeStyle = "#a6c8ff30";
      ctx.stroke();
    }
    ctx.setLineDash([4, 5]);
    ctx.beginPath();
    ctx.moveTo(x(0), y(0));
    ctx.lineTo(x(n - 1), y(0, n - 1));
    ctx.strokeStyle = "#a6c8ff99";
    ctx.stroke();
    ctx.setLineDash([]);
    const step = Math.max(1, Math.ceil((n - 1) / 420));
    const indices = [];
    for (let i = 0; i < n; i += step) indices.push(i);
    if (indices.at(-1) !== n - 1) indices.push(n - 1);
    const polygon = (points, fill) => {
      ctx.beginPath();
      points.forEach(([a, b], i) => (i ? ctx.lineTo(a, b) : ctx.moveTo(a, b)));
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    };
    if (p > 0.001) {
      // 2D Canvas에 투영한 띠: WebGL 컨텍스트, 텍스처, 3D 모델 다운로드가 없다.
      for (let k = 0; k < indices.length - 1; k++) {
        const a = indices[k],
          b = indices[k + 1];
        const ax = x(a),
          ay = y(series.cr[a], a),
          bx = x(b),
          by = y(series.cr[b], b);
        polygon(
          [
            [ax, ay],
            [bx, by],
            [bx, by + 5 * p],
            [ax, ay + 5 * p],
          ],
          `rgba(15,98,254,${p})`,
        );
        const light = clamp(67 + (ay - by) * 0.6, 55, 81);
        polygon(
          [
            [ax, ay],
            [bx, by],
            [bx + dx, by - dy],
            [ax + dx, ay - dy],
          ],
          `hsla(215,90%,${light}%,${p * 0.96})`,
        );
      }
      ctx.beginPath();
      indices.forEach((i, k) =>
        k
          ? ctx.lineTo(x(i) + dx, y(series.cr[i], i) - dy)
          : ctx.moveTo(x(i) + dx, y(series.cr[i], i) - dy),
      );
      ctx.strokeStyle = `rgba(186,230,255,${p * 0.95})`;
      ctx.stroke();
    }
    ctx.beginPath();
    indices.forEach((i, k) =>
      k
        ? ctx.lineTo(x(i), y(series.cr[i], i))
        : ctx.moveTo(x(i), y(series.cr[i], i)),
    );
    ctx.strokeStyle = "#bae6ff";
    ctx.lineJoin = "round";
    ctx.lineWidth = 1.8;
    ctx.stroke();
    if (n === 1) {
      ctx.beginPath();
      ctx.arc(x(0), y(series.cr[0]), 4, 0, Math.PI * 2);
      ctx.fillStyle = "#bae6ff";
      ctx.fill();
    }
    series.markers.forEach((m) => {
      const mx = x(m.index),
        my = y(series.cr[m.index], m.index);
      ctx.beginPath();
      ctx.moveTo(mx, my - 5);
      ctx.lineTo(mx + 5, my);
      ctx.lineTo(mx, my + 5);
      ctx.lineTo(mx - 5, my);
      ctx.closePath();
      ctx.fillStyle = "#ffffff";
      ctx.fill();
    });
    const chosen = chart.selected ?? n - 1;
    const cx = x(chosen),
      cy = y(series.cr[chosen], chosen);
    polygon(
      [
        [cx, cy],
        [cx + dx, cy - dy],
        [cx + dx, floor - dy],
        [cx, floor],
      ],
      "rgba(255,255,255,.08)",
    );
    ctx.beginPath();
    ctx.moveTo(cx, floor);
    ctx.lineTo(cx, cy);
    ctx.lineTo(cx + dx, cy - dy);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx, cy, 4, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff";
    ctx.fill();
    ctx.strokeStyle = "#002d9c";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = "#ffffff";
    ctx.textAlign = chosen > n * 0.75 ? "right" : "left";
    ctx.fillText(
      pct(series.cr[chosen]),
      cx + (chosen > n * 0.75 ? -9 : 9),
      cy - 15,
    );
    ctx.fillStyle = "#d0e2ff";
    ctx.textAlign = "left";
    ctx.fillText(fmtMD(series.rows[0].date), left, floor + 22);
    ctx.textAlign = "right";
    ctx.fillText(fmtMD(series.rows[n - 1].date), x(n - 1) + dx, floor + 22);
    if ($("historyDetails").open) drawDrawdown(series);
  }

  function scheduleChartPaint() {
    if (chart.paintFrame != null) return;
    chart.paintFrame = requestAnimationFrame(() => {
      chart.paintFrame = null;
      drawChart();
    });
  }

  function setChartView(view) {
    chart.view = view;
    $("performance").dataset.view = view;
    $("chartView")
      .querySelectorAll("button")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.view === view)),
      );
    if (chart.frame != null) cancelAnimationFrame(chart.frame);
    const from = chart.perspective,
      to = view === "ribbon" ? 1 : 0;
    if (reducedMotion.matches || document.hidden) {
      chart.frame = null;
      chart.perspective = to;
      drawChart();
      return;
    }
    const began = performance.now();
    const step = (now) => {
      const t = clamp((now - began) / 360, 0, 1);
      chart.perspective = from + (to - from) * (1 - Math.pow(1 - t, 3));
      drawChart();
      chart.frame = t < 1 ? requestAnimationFrame(step) : null;
    };
    chart.frame = requestAnimationFrame(step);
  }

  function selectHistory(index) {
    const s = chart.series;
    if (!s?.rows.length) return;
    const i = clamp(index, 0, s.rows.length - 1);
    chart.selected = i;
    chart.hover = i;
    const row = s.rows[i];
    $("historyDate").textContent = fmtLong(row.date);
    $("historyDate").dateTime = String(row.date).slice(0, 10);
    $("historyTag").textContent =
      i === s.rows.length - 1 ? "최근 기록" : "선택한 날짜";
    $("historyReturn").textContent = pct(s.cr[i]);
    $("historyReturn").className = `readout-return ${tone(s.cr[i])}`;
    $("historyValue").textContent = won(s.value[i]);
    $("historyPrincipal").textContent = won(s.principal[i]);
    $("historyDrawdown").textContent = pct(s.dd[i] * 100);
    const amount = s.markers
      .filter((m) => m.index === i)
      .reduce((sum, m) => sum + m.amount, 0);
    $("historyEvent").textContent = amount
      ? `적립금 ${signedWon(amount)} 기록`
      : i === s.rows.length - 1
        ? "날짜 눈금을 움직여 기록을 살펴보세요."
        : "이 날짜의 계좌 기록을 보고 있습니다.";
    const cursor = $("historyCursor");
    cursor.value = String(i);
    cursor.setAttribute(
      "aria-valuetext",
      `${fmtLong(row.date)}, 수익률 ${pct(s.cr[i])}, 평가금액 ${won(s.value[i])}`,
    );
    $("historyPosition").textContent = `${i + 1} / ${s.rows.length}개 기록`;
    scheduleChartPaint();
  }

  function exportHistory() {
    const s = chart.series;
    if (!s?.rows.length || state.chartDataAccount !== state.chartAccount)
      return;
    const csv = [
      "날짜,평가금액(원),투자원금(원),누적시간가중수익률(%),고점대비하락률(%)",
      ...s.rows.map((r, i) =>
        [
          String(r.date).slice(0, 10),
          s.value[i],
          s.principal[i],
          s.cr[i],
          (s.dd[i] * 100).toFixed(6),
        ].join(","),
      ),
    ].join("\r\n");
    const url = URL.createObjectURL(
      new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `nungum-${state.chartAccount.replace(/[^a-z0-9_-]/gi, "-")}-${String(s.rows.at(-1).date).slice(0, 10)}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function drawDrawdown(series) {
    const box = $("drawdownBox");
    const width = Math.max(1, box.clientWidth);
    const height = Math.max(80, box.clientHeight);
    const context = setupCanvas(el.chartDrawdown, width, height);
    const padding = {
      top: 10,
      right: 12,
      bottom: 8,
      left: width < 480 ? 52 : 64,
    };
    const n = series.rows.length;
    const x = (i) =>
      padding.left +
      ((width - padding.left - padding.right) * i) / Math.max(1, n - 1);
    const minDD = Math.min(-0.005, ...series.dd);
    const y = (v) =>
      padding.top +
      ((0 - v) / (0 - minDD)) * (height - padding.top - padding.bottom);
    context.font = `400 11.5px ${FONT}`;
    context.textBaseline = "middle";
    context.textAlign = "right";
    context.strokeStyle = inkAlpha(0.34);
    context.beginPath();
    context.moveTo(padding.left, y(0));
    context.lineTo(width - padding.right, y(0));
    context.stroke();
    context.fillStyle = inkAlpha(0.7);
    context.fillText("0%", padding.left - 8, y(0));
    context.fillText(
      pct(minDD * 100, { digits: 1 }),
      padding.left - 8,
      y(minDD),
    );
    const visible = Math.max(1, Math.ceil((n - 1) * chart.progress) + 1);
    context.beginPath();
    context.moveTo(x(0), y(0));
    series.dd.forEach((v, i) => {
      if (i < visible) context.lineTo(x(i), y(v));
    });
    context.lineTo(x(Math.min(n - 1, visible - 1)), y(0));
    context.closePath();
    context.fillStyle = "rgba(37, 87, 201, 0.16)";
    context.fill();
    context.strokeStyle = COLORS.down;
    context.lineWidth = 1.2;
    context.beginPath();
    series.dd.forEach((v, i) => {
      if (i < visible) {
        if (i === 0) context.moveTo(x(i), y(v));
        else context.lineTo(x(i), y(v));
      }
    });
    context.stroke();
    if (chart.hover != null && chart.progress >= 1) {
      const i = chart.hover;
      context.beginPath();
      context.arc(x(i), y(series.dd[i]), 3.5, 0, Math.PI * 2);
      context.fillStyle = COLORS.down;
      context.fill();
    }
  }

  function handleChartPointer(event) {
    const g = chart.geometry;
    if (!chart.series || !g || $("performance").dataset.loading === "true")
      return;
    const px = event.clientX - event.currentTarget.getBoundingClientRect().left;
    const i = clamp(
      Math.round(
        ((px - g.padding.left) / (g.width - g.padding.left - g.padding.right)) *
          Math.max(1, g.n - 1),
      ),
      0,
      g.n - 1,
    );
    if (chart.selected !== i) selectHistory(i);
  }

  function updateChart(snapshots) {
    const rows = Array.isArray(snapshots) ? snapshots : [];
    const basket = chartBasket();
    const initial = basket
      ? Number(basket.initial_capital || 0)
      : Number(state.legacy?.initial_capital || 0);
    const flows = basket ? state.flows.get(basket.basket) || [] : [];
    const series = buildSeries(rows, flows, initial);
    const historyRows = state.series.get(state.chartAccount);
    const history = historyRows?.length
      ? buildSeries(historyRows, flows, initial)
      : series;
    const historicalDD = new Map(
      history.rows.map((row, i) => [row.date, history.dd[i]]),
    );
    series.dd = series.rows.map(
      (row, i) => historicalDD.get(row.date) ?? series.dd[i],
    );
    const wasLatest =
      chart.selected == null ||
      chart.selected === chart.series?.rows.length - 1;
    const previousDate = chart.series?.rows[chart.selected]?.date;
    const sameAccount = state.chartDataAccount === state.chartAccount;
    if (!sameAccount) chart.tablePage = null;
    chart.series = series;
    state.chartDataAccount = state.chartAccount;
    state.chartRows = series.rows;
    chart.hover = null;
    el.chartTip.hidden = true;
    renderChartMaturity();
    const cursor = $("historyCursor");
    cursor.max = String(Math.max(0, series.rows.length - 1));
    cursor.disabled = !series.rows.length;
    $("exportHistory").disabled = !series.rows.length;
    $("historyLatest").disabled = !series.rows.length;
    $("historyStart").textContent = fmtMD(series.rows[0]?.date);
    $("historyEnd").textContent = fmtMD(series.rows.at(-1)?.date);
    // 접힌 표의 수천 개 행을 매 갱신마다 만들지 않는다.
    clearChartDetails();

    if (!series.rows.length) {
      el.chartWrap.hidden = true;
      el.chartEmpty.hidden = false;
      el.chartEmpty.innerHTML =
        "<strong>선택한 기간에 기록이 없습니다.</strong><span>조회 기간을 넓히거나 모의투자 기록을 확인해 주세요.</span>";
      el.chartSummary.textContent = "선택한 기간에 자산 기록이 없습니다.";
      el.chartRows.innerHTML = "";
      el.monthStrip.innerHTML = "";
      return;
    }
    const first = series.rows[0];
    const last = series.rows[series.rows.length - 1];
    const change =
      Number(last.total_value || 0) - Number(first.total_value || 0);
    const deposits = series.principal.at(-1) - series.principal[0];
    el.chartSummary.textContent = `${fmtMD(first.date)} ~ ${fmtMD(last.date)} · 평가금액 ${signedWon(change)}${deposits ? ` (적립 ${won(deposits)} 포함)` : ""} · 누적 수익률 ${pct(last.cumulative_return)}`;
    el.chartWrap.hidden = false;
    el.chartEmpty.hidden = true;
    $("historyDetails").hidden = false;
    renderChartTable(series);
    renderMonthStrip(series, history);
    const previousIndex =
      sameAccount && !wasLatest
        ? series.rows.findIndex((r) => r.date === previousDate)
        : -1;
    selectHistory(previousIndex >= 0 ? previousIndex : series.rows.length - 1);
    drawChart();
  }

  async function refreshChart() {
    ensureChartAccountOptions();
    state.chartAccount = el.chartAccount.value;
    updateOverview();
    syncChartQuery();
    const request = ++state.chartRequest;
    const account = state.chartAccount,
      days = state.chartDays;
    $("performance").dataset.loading = "true";
    $("performance").setAttribute("aria-busy", "true");
    $("exportHistory").disabled = true;
    $("historyCursor").disabled = true;
    $("historyLatest").disabled = true;
    // 계좌가 바뀌면 이전 숫자를 남기지 않는다. 오래 걸린 이전 응답도 폐기한다.
    if (state.chartDataAccount !== account) {
      clearChartDetails();
      el.chartWrap.hidden = true;
      el.chartEmpty.hidden = false;
      el.chartEmpty.innerHTML =
        "<strong>선택한 계좌를 불러오는 중입니다.</strong><span>날짜별 성과 기록을 확인하고 있습니다.</span>";
    }
    try {
      const basket = chartBasket();
      if (
        basket &&
        (state.seriesStatus.get(account) !== "ready" ||
          state.flowStatus.get(basket.basket) !== "ready")
      )
        throw new Error(
          "수익률 계산에 필요한 전체 기록을 확인하지 못했습니다.",
        );
      const cached = state.series.get(account);
      // 전체 기록은 갱신 주기에 한 번 읽는다. 기간·계좌 변경 때 같은 데이터를 다시 받지 않는다.
      const cutoff = new Date(
        parseDate(koreaDate(new Date())).getTime() - days * DAY_MS,
      );
      const data = cached
        ? { snapshots: cached.filter((row) => parseDate(row.date) >= cutoff) }
        : await fetchJson(
            `/api/snapshots?days=${days}&account_key=${encodeURIComponent(account || "")}`,
            { timeout: 15_000, key: "chart" },
          );
      if (
        request !== state.chartRequest ||
        account !== state.chartAccount ||
        days !== state.chartDays
      )
        return;
      updateChart(data?.snapshots || []);
    } catch {
      if (request !== state.chartRequest) return;
      clearChartDetails();
      el.chartSummary.textContent =
        "성과 기록을 읽지 못했습니다. 운영 영역의 다시 확인을 눌러주세요.";
      el.chartWrap.hidden = true;
      el.chartEmpty.hidden = false;
      el.chartEmpty.innerHTML =
        "<strong>성과 기록을 읽지 못했습니다.</strong><span>계좌 기록은 바뀌지 않았습니다. 연결 상태를 확인해 주세요.</span>";
    } finally {
      if (request === state.chartRequest) {
        $("performance").dataset.loading = "false";
        $("performance").setAttribute("aria-busy", "false");
      }
    }
  }

  /* ---------- 검증 ---------- */

  const verdictCopy = {
    PASS_CANDIDATE: ["검토 가능", ""],
    FAIL_REVIEW: ["재점검 필요", "stop"],
    WAIT: ["관찰 중", "warn"],
  };
  function renderEvaluations(evaluations) {
    const items = Array.isArray(evaluations) ? evaluations : [];
    state.evaluations = items;
    el.basketEval.setAttribute("aria-busy", "false");
    if (!items.length) {
      el.basketEval.innerHTML =
        '<p class="loading">검증 중인 계좌가 없습니다.</p>';
      renderDecision();
      return;
    }
    const byName = new Map((state.baskets || []).map((b) => [b.basket, b]));
    const ordered = [...items].sort(
      (a, b) =>
        Number(Boolean(byName.get(b.basket)?.is_primary)) -
        Number(Boolean(byName.get(a.basket)?.is_primary)),
    );
    el.basketEval.innerHTML = ordered
      .map((item) => {
        const basket = byName.get(item.basket);
        const name = basket?.display_name || item.basket;
        const days = Number(item.progress_days || 0);
        const minimum = Math.max(1, Number(item.min_trading_days || 60));
        const progress = Math.min(100, Math.round((days / minimum) * 100));
        const coverage =
          item.snapshot_coverage == null
            ? null
            : Math.round(Number(item.snapshot_coverage) * 100);
        if (item.error) {
          return `<div class="review-item">
        <p class="review-name">${escapeHtml(name)}${basket?.is_primary ? " · 주력" : ""}</p>
        <p class="verdict warn">확인 불가</p>
        <ul class="review-issues"><li>${escapeHtml(item.error)}</li></ul>
      </div>`;
        }
        const [copy, cls] = verdictCopy[item.verdict] || ["관찰 중", "warn"];
        const issues = (item.issues || []).slice(0, 3);
        const restored = Number(item.reconstructed_days || 0);
        const measured =
          item.measured_coverage == null
            ? null
            : Math.round(Number(item.measured_coverage) * 100);
        const coverageText =
          coverage == null
            ? "기록 누락 확인 중"
            : coverage >= 100
              ? restored
                ? `기록 누락 없음 (나중에 채운 ${restored}일 포함, 제때 기록 ${measured}%)`
                : "기록 누락 없음"
              : `기록 누락 ${Math.max(0, 100 - coverage)}%`;
        return `<div class="review-item">
        <p class="review-name">${escapeHtml(name)}${basket?.is_primary ? " · 주력" : ""}</p>
        <div class="review-progress">
          <div class="bar" role="progressbar" aria-label="${escapeHtml(name)} 검증 진행" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><i style="width:${progress}%"></i></div>
          <p><span>${item.paper_only ? `전체 운영 ${days}거래일` : `${days} / ${minimum} 거래일`}</span><span>${coverageText}</span></p>
        </div>
        <p class="verdict ${cls}">${copy}</p>
        ${issues.length ? `<ul class="review-issues">${issues.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>` : ""}
      </div>`;
      })
      .join("");
    renderChartMaturity();
    renderDecision();
  }

  /* ---------- 운영 ---------- */

  function statusRowItem(label, value, detail, stateName) {
    return `<div class="status-item"><dt><i class="dot" data-state="${stateName}" aria-hidden="true"></i>${escapeHtml(label)}</dt><dd><b>${escapeHtml(value)}</b>${detail ? `<span>${escapeHtml(detail)}</span>` : ""}</dd></div>`;
  }
  function marketCopy(regime) {
    const v = String(regime || "").toLowerCase();
    if (v === "bullish") return ["상승 추세", "시장 추세 기준 매수 허용", "ok"];
    if (v === "bearish")
      return ["하락 추세", "시장 추세 기준 매수 제한", "warning"];
    if (v === "caution") return ["주의", "포지션 축소 구간", "warning"];
    if (v === "disabled") return ["사용 안 함", "시장 추세 판단을 꺼 두었습니다", "info"];
    return ["확인 불가", "시장 상태 데이터 없음", "warning"];
  }
  const strategyCopy = (s) =>
    ({ scoring: "종합 점수형", basket_rebalance: "바스켓 리밸런싱" })[
      String(s || "").toLowerCase()
    ] ||
    s ||
    "—";
  const signalCopy = (s) =>
    ({ BUY: "매수", SELL: "매도", HOLD: "보유" })[
      String(s || "").toUpperCase()
    ] ||
    s ||
    "—";
  const signalSourceCopy = (s) =>
    ({ pre_market: "장 시작 전", intraday: "장중", post_market: "장 마감 후" })[
      String(s || "").toLowerCase()
    ] ||
    s ||
    "—";

  function renderRuntime(runtime) {
    state.runtime = runtime || null;
    state.runtimeStatus = runtime && runtime.trading_halt ? "ready" : "error";
    updateSyncIndicator();
    el.runtimeOps.setAttribute("aria-busy", "false");
    if (!runtime) {
      el.haltGuidance.hidden = true;
      el.runtimeOps.innerHTML = [
        statusRowItem(
          "거래 안전",
          "확인 불가",
          "거래 중지 상태를 읽지 못했습니다",
          "warning",
        ),
        statusRowItem("장 상태", "확인 불가", "최근 데이터 없음", "warning"),
        statusRowItem("자동매매", "확인 불가", "스케줄러 기록 없음", "warning"),
        statusRowItem("증권사 연결", "확인 불가", "요청 통계 없음", "warning"),
        statusRowItem(
          "데이터 갱신",
          "확인 불가",
          "잠시 후 다시 확인",
          "warning",
        ),
      ].join("");
      el.runtimeMeta.textContent =
        "운영 정보를 불러오지 못했습니다. 자산 계좌 기록은 바뀌지 않았습니다.";
      el.statusRow.innerHTML =
        statusItem("거래", "확인 불가", "warning") +
        statusItem("장 상태", "확인 불가", "warning") +
        statusItem("자동매매", "확인 불가", "warning") +
        statusItem("데이터", "확인 불가", "warning");
      renderSignals(null, null);
      renderWsGap(null);
      renderDecision();
      return;
    }
    const halt = runtime.trading_halt;
    const haltKnown = Boolean(halt && typeof halt.halted === "boolean");
    const halted = Boolean(halt && halt.halted);
    const [market, marketSupport, marketState] = marketCopy(
      runtime.market_regime && runtime.market_regime.regime,
    );
    const loop = runtime.loop_metrics;
    const kis = runtime.kis_stats;
    const updatedAt = runtime.runtime_file_updated_at;
    const age = runtimeAgeMinutes();
    const fresh = age != null && age <= 720;
    const loopElapsed =
      loop && loop.recent_avg_elapsed_s != null
        ? `${Number(loop.recent_avg_elapsed_s).toFixed(1)}초`
        : null;
    const haltValue = !haltKnown
      ? "확인 불가"
      : halted
        ? "거래 중지"
        : "운용 가능";
    const haltDetail = !haltKnown
      ? "거래 중지 상태를 읽지 못했습니다"
      : halted
        ? halt.reason || "운영자 확인 필요"
        : "거래 중지 없음";
    const haltState = !haltKnown ? "warning" : halted ? "error" : "ok";
    // 자동매매 신선도는 런타임 파일이 아니라 스케줄러 루프의 마지막 성공 시각으로 본다.
    // 수동 리밸런싱 점검만으로도 런타임 파일은 갱신되므로, 그걸 기준으로 삼으면 멈춘 루프가 정상으로 보인다.
    const loopLast =
      parseDate(loop && loop.last_success) ||
      (loop ? parseDate(updatedAt) : null);
    const loopAge = loopLast
      ? Math.max(0, Math.round((Date.now() - loopLast.getTime()) / 60_000))
      : null;
    // 멈춤 판정은 서버가 장 운영 시간 기준으로 한다(밤·주말·휴장일은 멈춘 게 아니다).
    const schedulerUnused = runtime.scheduler_in_use === false;
    const loopFresh = !runtime.scheduler_stale && loopAge != null;
    const autoValue = schedulerUnused
      ? "사용 안 함"
      : loopElapsed
        ? loopFresh
          ? "정상"
          : `${formatAge(loopAge)} 실행`
        : "기록 없음";
    const autoDetail = schedulerUnused
      ? "매매는 평일 오전 10시쯤 한 번 실행됩니다"
      : loopElapsed
        ? `마지막 실행 ${fmtDT(loopLast)} · 루프 ${loopElapsed}`
        : "스케줄러 기록 없음";
    const autoState = schedulerUnused ? "info" : loop && loopFresh ? "ok" : "warning";
    const kisValue =
      kis && kis.minute_utilization_pct != null
        ? `${Number(kis.minute_utilization_pct).toFixed(1)}% 사용`
        : null;
    const kisRow =
      state.mode === "paper"
        ? statusRowItem(
            "증권사 연결",
            "모의투자",
            "실계좌 연결 대상 아님",
            "info",
          )
        : statusRowItem(
            "증권사 연결",
            kisValue ? (fresh ? kisValue : "기록 오래됨") : "기록 없음",
            kisValue
              ? `분당 요청 한도 기준${fresh ? "" : ` · ${formatAge(age)}`}`
              : "요청 통계 없음",
            kis && fresh ? "ok" : "warning",
          );
    const dataState = age == null ? "warning" : age > 30 ? "warning" : "ok";

    el.runtimeOps.innerHTML =
      statusRowItem("거래 안전", haltValue, haltDetail, haltState) +
      statusRowItem("장 상태", market, marketSupport, marketState) +
      statusRowItem("자동매매", autoValue, autoDetail, autoState) +
      kisRow +
      statusRowItem(
        "데이터 갱신",
        formatAge(age),
        updatedAt ? fmtDT(updatedAt) : "스케줄러 데이터 없음",
        dataState,
      );
    el.statusRow.innerHTML =
      statusItem("거래", haltValue, haltState) +
      statusItem("장 상태", market, marketState) +
      statusItem("자동매매", autoValue, autoState) +
      statusItem("데이터", formatAge(age), dataState);

    const meta = [];
    if (runtime.strategy) meta.push(`전략 ${strategyCopy(runtime.strategy)}`);
    if (updatedAt) meta.push(`스케줄러 기록 ${fmtDT(updatedAt)}`);
    el.runtimeMeta.textContent = meta.join(" · ");
    el.haltGuidance.hidden = !halted;
    if (halted)
      $("haltGuidanceReason").textContent =
        halt.reason || "신규 매수는 직접 해제하기 전까지 막혀 있습니다.";
    renderSignals(runtime.signals_today, runtime.signals_date);
    renderWsGap(runtime);
    renderDecision();
  }

  function renderSignals(signals, signalsDate) {
    const table = $("signalsTableWrap");
    const empty = $("signalEmpty");
    const error = $("signalError");
    const count = $("signalCount");
    if (signals == null) {
      table.hidden = true;
      empty.hidden = true;
      error.hidden = false;
      count.textContent = "확인 불가";
      return;
    }
    const isToday = !signalsDate || signalsDate === koreaDate(new Date());
    const rows = isToday && Array.isArray(signals) ? signals : [];
    count.textContent = `${rows.length}건`;
    table.hidden = !rows.length;
    empty.hidden = Boolean(rows.length);
    error.hidden = true;
    empty.textContent = isToday
      ? "오늘 생성된 신호가 없습니다. "
      : `오늘 생성된 신호가 없습니다. 마지막 신호는 ${fmtLong(signalsDate)}입니다.`;
    $("signalRows").innerHTML = rows
      .map(
        (s) => `<tr>
      <td><time datetime="${escapeHtml(s.at || "")}">${escapeHtml(fmtDT(s.at))}</time></td>
      <td>${escapeHtml(s.symbol || "—")}</td>
      <td>${escapeHtml(signalCopy(s.signal))}</td>
      <td class="num">${Number.isFinite(Number(s.score)) ? Number(s.score).toFixed(2) : "—"}</td>
      <td>${escapeHtml(signalSourceCopy(s.source))}</td>
    </tr>`,
      )
      .join("");
  }

  function renderWsGap(runtime) {
    const gap = runtime && runtime.ws_gap;
    const summary = $("wsGapSummary");
    const table = $("wsGapTableWrap");
    const empty = $("wsGapEmpty");
    const na = $("wsGapNA");
    if (!gap || !gap.available) {
      summary.innerHTML = statusRowItem(
        "웹소켓",
        "정보 없음",
        "스케줄러 기록 대기",
        "warning",
      );
      table.hidden = true;
      empty.hidden = true;
      na.hidden = false;
      return;
    }
    const gaps = gap.recent_gaps || [];
    na.hidden = true;
    summary.innerHTML =
      statusRowItem(
        "웹소켓",
        gap.is_connected ? "연결됨" : "연결 끊김",
        "",
        gap.is_connected ? "ok" : "warning",
      ) +
      statusRowItem(
        "최근 끊김",
        `${nf0.format(Number(gap.total_gap_count || 0))}건`,
        "",
        gap.total_gap_count > 0 ? "warning" : "ok",
      );
    table.hidden = !gaps.length;
    empty.hidden = Boolean(gaps.length);
    $("wsGapRows").innerHTML = [...gaps]
      .reverse()
      .map(
        (g) => `<tr>
      <td>${escapeHtml(fmtDT(g.disconnect_at))}</td>
      <td>${escapeHtml(fmtDT(g.reconnect_at))}</td>
      <td class="num">${escapeHtml(`${Number(g.gap_seconds || 0).toFixed(1)}초`)}</td>
      <td>${escapeHtml((g.affected_symbols || []).join(", ") || "—")}</td>
      <td>${g.rest_backfill_performed ? `${nf0.format(Number(g.rest_backfill_count || 0))}건` : "안 함"}</td>
      <td>${g.blackswan_cooldown_triggered ? '<span class="down">안전 정지</span>' : g.blackswan_checked ? "정상" : "—"}</td>
    </tr>`,
      )
      .join("");
  }

  function renderLegacy(portfolio) {
    state.legacy = portfolio || null;
    const legacyEmpty = Boolean(portfolio && portfolio.empty);
    $("legacyHeading").hidden = legacyEmpty;
    $("summary").hidden = legacyEmpty;
    if (legacyEmpty) {
      $("positionsWrap").hidden = true;
      $("noPositions").hidden = true;
      return;
    }
    if (!portfolio) {
      $("summary").innerHTML = statusRowItem(
        "이전 계좌",
        "확인 불가",
        "",
        "warning",
      );
      $("positionsWrap").hidden = true;
      $("noPositions").hidden = false;
      return;
    }
    $("summary").innerHTML =
      statusRowItem("총자산", won(portfolio.total_value), "", "info") +
      statusRowItem("수익률", pct(portfolio.total_return), "", "info") +
      statusRowItem("현금", won(portfolio.cash), "", "info") +
      statusRowItem("실현손익", signedWon(portfolio.realized_pnl), "", "info") +
      statusRowItem(
        "최대낙폭",
        pct(-Math.abs(Number(portfolio.mdd || 0))),
        "",
        "info",
      ) +
      statusRowItem(
        "보유 종목",
        `${nf0.format(Number(portfolio.position_count || 0))}개`,
        "",
        "info",
      );
    const positions = portfolio.positions || [];
    $("positionsWrap").hidden = !positions.length;
    $("noPositions").hidden = Boolean(positions.length);
    $("positions").innerHTML = positions
      .map(
        (p) => `<tr>
      <td>${escapeHtml(p.symbol || "—")}</td>
      <td class="num">${nf0.format(Number(p.quantity || 0))}</td>
      <td class="num">${won(p.avg_price)}</td>
      <td class="num">${won(p.current_price)}</td>
      <td class="num">${won(p.current_value)}</td>
      <td class="num ${tone(p.pnl_rate)}">${pct(p.pnl_rate)}</td>
    </tr>`,
      )
      .join("");
  }

  /* ---------- 적립금 기록 ---------- */

  function updateDepositCopy() {
    const d = $("depositDescription");
    if (!d) return;
    d.textContent =
      state.mode === "live"
        ? "실제 계좌에 입금이 끝난 뒤 같은 금액을 기록하세요. 주문은 나가지 않고 실전 성과 계산에만 반영됩니다."
        : "모의투자 계좌에 추가할 적립금을 입력하세요. 실제 은행 계좌에서 돈이 이동하지 않습니다.";
    $("amountHelp").textContent =
      state.mode === "live"
        ? "실제 입금액과 같은 금액을 입력하세요."
        : "입력한 금액은 모의투자 원금에 더해집니다.";
  }
  function resetDepositForm() {
    state.depositConfirming = false;
    state.depositRequestId = null;
    el.depositForm.reset();
    el.depositFields.hidden = false;
    el.depositConfirm.hidden = true;
    el.depositBack.hidden = true;
    el.depositSubmit.textContent = "내용 확인";
    el.depositSubmit.disabled = false;
    el.depositError.hidden = true;
    el.depositError.textContent = "";
    document
      .querySelectorAll("[data-amount]")
      .forEach((b) => b.setAttribute("aria-pressed", "false"));
  }
  function openDeposit() {
    if (!canRecordDeposit()) {
      showToast("계좌 기록과 거래 상태를 먼저 다시 확인하세요.", "error");
      return;
    }
    resetDepositForm();
    const select = $("depBasket");
    select.innerHTML = sortedBaskets()
      .map(
        (b) =>
          `<option value="${escapeHtml(b.basket)}">${escapeHtml(b.display_name)}</option>`,
      )
      .join("");
    const primary = selectedBasket();
    if (primary) select.value = primary.basket;
    updateDepositCopy();
    if (!el.depositDialog.open) el.depositDialog.showModal();
    window.setTimeout(() => select.focus(), 0);
  }
  function closeDeposit() {
    if (el.depositDialog.open) el.depositDialog.close();
  }
  function showDepositError(message, field = null) {
    el.depositError.textContent = message;
    el.depositError.hidden = false;
    if (field) field.focus();
  }
  function depositValues() {
    return {
      basket: $("depBasket").value,
      amount: Number($("depAmount").value),
      note: $("depNote").value.trim(),
    };
  }
  function showDepositConfirmation(values) {
    state.depositRequestId = window.crypto?.randomUUID
      ? window.crypto.randomUUID()
      : `deposit-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const selected = $("depBasket").selectedOptions[0];
    $("confirmBasket").textContent = selected
      ? selected.textContent
      : values.basket;
    $("confirmAmount").textContent = won(values.amount);
    $("confirmMode").textContent =
      state.mode === "live" ? "실전 투자" : "모의투자";
    el.depositFields.hidden = true;
    el.depositConfirm.hidden = false;
    el.depositBack.hidden = false;
    el.depositSubmit.textContent = "기록하기";
    el.depositError.hidden = true;
    state.depositConfirming = true;
    el.depositBack.focus();
  }
  function showDepositFields() {
    state.depositConfirming = false;
    state.depositRequestId = null;
    el.depositFields.hidden = false;
    el.depositConfirm.hidden = true;
    el.depositBack.hidden = true;
    el.depositSubmit.textContent = "내용 확인";
    $("depBasket").focus();
  }
  async function submitDeposit(values) {
    el.depositSubmit.disabled = true;
    el.depositSubmit.textContent = "기록하는 중";
    el.depositError.hidden = true;
    try {
      const data = await fetchJson("/api/deposit", {
        timeout: 15_000,
        key: "deposit",
        options: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "quant-dashboard",
            "Idempotency-Key": state.depositRequestId,
          },
          body: JSON.stringify(values),
        },
      });
      if (!data || !data.ok)
        throw new Error((data && data.error) || "기록에 실패했습니다.");
      closeDeposit();
      showToast(
        `${state.mode === "live" ? "실전 입금" : "모의투자 적립"} ${won(data.amount)}을 기록했습니다.`,
      );
      // 적립 전에 시작한 조회가 있으면 끝낸 뒤 새 금액을 다시 읽는다.
      await coreRefresh;
      await refreshCore(true);
    } catch (error) {
      showDepositError(
        `기록하지 못했습니다. ${error.message || "연결을 확인한 뒤 다시 시도하세요."}`,
      );
      el.depositSubmit.disabled = false;
      el.depositSubmit.textContent = "기록하기";
    }
  }

  let toastTimer = null;
  function showToast(message, kind = "ok") {
    const toast = $("toast");
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.dataset.kind = kind;
    toast.classList.add("show");
    toastTimer = window.setTimeout(() => toast.classList.remove("show"), 5_000);
  }

  /* ---------- 갱신 주기 ---------- */

  // 주기 갱신은 숨은 탭에서 쉬지만, 첫 로드는 탭이 뒤에 있어도 반드시 한 번 채운다
  // (백그라운드로 연 탭이 빈 화면으로 남던 문제).
  let coreRefresh = null;
  let slowRefresh = null;
  function refreshCore(force = false) {
    if (!force && document.visibilityState === "hidden") return;
    // 타이머·탭 복귀·버튼 클릭이 겹쳐도 진행 중인 조회를 취소하지 않는다.
    if (!coreRefresh)
      coreRefresh = loadCore().finally(() => {
        coreRefresh = null;
      });
    return coreRefresh;
  }

  async function loadCore() {
    state.coreStatus = "loading";
    updateSyncIndicator();
    const basketTask = fetchJson("/api/baskets", {
      timeout: 12_000,
      key: "baskets",
    }).then(async (data) => {
      renderBasketTracks(data);
      await Promise.allSettled([refreshFlows(), refreshSeries()]);
      renderBasketTracks({
        baskets: state.baskets,
        mode: state.mode,
        timestamp: state.lastCoreSuccess,
      });
      await refreshChart();
      renderDecision();
      return data;
    });
    const legacyTask = fetchJson("/api/portfolio", {
      timeout: 15_000,
      key: "legacy",
    }).then(renderLegacy);
    const results = await Promise.allSettled([basketTask, legacyTask]);
    if (results[0].status === "rejected") {
      state.coreError = results[0].reason || new Error("바스켓 조회 실패");
      state.coreStatus = "error";
      el.basketTracks.setAttribute("aria-busy", "false");
      el.basketTracks.innerHTML =
        '<div class="empty"><strong>포트폴리오를 불러오지 못했습니다.</strong><span>이전 화면의 숫자는 최신이 아닐 수 있습니다.</span></div>';
    } else if (
      results.some((r) => r.status === "rejected") ||
      state.flowError ||
      [...state.seriesStatus.values()].some((status) => status === "error")
    ) {
      state.coreStatus = "partial";
    } else {
      state.coreStatus = "ready";
    }
    updateSyncIndicator();
    renderDecision();
  }

  function refreshSlow(force = false) {
    if (!force && document.visibilityState === "hidden") return;
    if (!slowRefresh)
      slowRefresh = loadSlow().finally(() => {
        slowRefresh = null;
      });
    return slowRefresh;
  }

  async function loadSlow() {
    if (!state.runtime) {
      state.runtimeStatus = "loading";
      updateSyncIndicator();
    }
    const evalTask = fetchJson("/api/basket_evaluation", {
      timeout: 30_000,
      key: "evaluation",
    })
      .then((data) => renderEvaluations((data && data.evaluations) || []))
      .catch(() => {
        // 이전에 성공한 결과로 계속 판단하지 않는다(그 사이 생긴 문제를 못 본다)
        state.evaluations = null;
        el.basketEval.setAttribute("aria-busy", "false");
        el.basketEval.innerHTML =
          '<p class="loading">검증 상태를 불러오지 못했습니다. 잠시 후 다시 확인하세요.</p>';
        renderDecision();
      });
    const runtimeTask = fetchJson("/api/runtime", {
      timeout: 30_000,
      key: "runtime",
    })
      .then(renderRuntime)
      .catch(() => renderRuntime(null));
    await Promise.allSettled([evalTask, runtimeTask]);
  }

  async function refreshAll(force = false) {
    await Promise.allSettled([refreshCore(force), refreshSlow(force)]);
  }

  /* ---------- 이벤트 ---------- */

  function initScrollSpy() {
    const links = [...document.querySelectorAll(".nav > a")];
    const targets = links
      .map((a) => document.querySelector(a.getAttribute("href")))
      .filter(Boolean);
    let scheduled = false;
    const update = () => {
      scheduled = false;
      const anchor = window.innerWidth <= 600 ? 140 : 120;
      const passed = targets.filter(
        (target) => target.getBoundingClientRect().top <= anchor,
      );
      const current =
        Math.ceil(window.scrollY + window.innerHeight) >=
        document.documentElement.scrollHeight - 2
          ? "operations"
          : passed
              .sort(
                (a, b) =>
                  a.getBoundingClientRect().top - b.getBoundingClientRect().top,
              )
              .at(-1)?.id || "top";
      links.forEach((a) => {
        if (a.getAttribute("href") === `#${current}`)
          a.setAttribute("aria-current", "location");
        else a.removeAttribute("aria-current");
      });
    };
    // 사용자가 스크롤할 때 한 프레임에 한 번만 현재 위치를 갱신한다.
    window.addEventListener(
      "scroll",
      () => {
        if (!scheduled) {
          scheduled = true;
          requestAnimationFrame(update);
        }
      },
      { passive: true },
    );
    window.addEventListener("resize", update);
    update();
  }

  function wireEvents() {
    $("accountTabs").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-account]");
      if (!button) return;
      state.chartAccount = button.dataset.account;
      ensureChartAccountOptions();
      el.chartAccount.value = state.chartAccount;
      renderBasketTracks({
        baskets: state.baskets,
        mode: state.mode,
        timestamp: state.lastCoreSuccess,
      });
      renderDecision();
      refreshChart();
    });
    el.openDeposit.addEventListener("click", openDeposit);
    $("closeDepositButton").addEventListener("click", closeDeposit);
    $("depositCancelButton").addEventListener("click", closeDeposit);
    el.depositBack.addEventListener("click", showDepositFields);
    $("retryButton").addEventListener("click", () => refreshAll(true));
    $("priorityNotice").addEventListener("click", () =>
      el.decisionAction.click(),
    );

    el.decisionAction.addEventListener("click", () => {
      const action = el.decisionAction.dataset.action;
      if (action === "deposit") openDeposit();
      else if (action === "retry") refreshAll();
      else if (action)
        document.getElementById(action)?.scrollIntoView({
          behavior: reducedMotion.matches ? "auto" : "smooth",
          block: "start",
        });
    });

    el.chartAccount.addEventListener("change", () => {
      state.chartAccount = el.chartAccount.value;
      refreshChart();
    });
    $("chartRange").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-days]");
      if (!button) return;
      state.chartDays = Number(button.dataset.days);
      document
        .querySelectorAll("#chartRange button")
        .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      refreshChart();
    });
    el.chartEquity.addEventListener("pointermove", handleChartPointer);
    el.chartEquity.addEventListener("pointerdown", handleChartPointer);
    $("historyCursor").addEventListener("input", (event) =>
      selectHistory(Number(event.target.value)),
    );
    $("historyLatest").addEventListener("click", () =>
      selectHistory((chart.series?.rows.length || 1) - 1),
    );
    $("exportHistory").addEventListener("click", exportHistory);
    $("historyDetails").addEventListener("toggle", () => {
      if ($("historyDetails").hidden || !chart.series) return;
      if ($("historyDetails").open) {
        renderChartTable(chart.series);
        drawDrawdown(chart.series);
      } else {
        el.chartRows.innerHTML = "";
        chart.tableSeries = null;
      }
    });
    $("historyTablePrev").addEventListener("click", () => moveHistoryTable(-1));
    $("historyTableNext").addEventListener("click", () => moveHistoryTable(1));
    $("chartView").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-view]");
      if (button) setChartView(button.dataset.view);
    });
    reducedMotion.addEventListener("change", () => {
      if (reducedMotion.matches) setChartView(chart.view);
    });

    document.querySelectorAll("[data-amount]").forEach((button) => {
      button.addEventListener("click", () => {
        $("depAmount").value = button.dataset.amount;
        document
          .querySelectorAll("[data-amount]")
          .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
        $("depAmount").focus();
      });
    });

    el.depositForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!canRecordDeposit()) {
        showDepositError(
          "계좌 기록과 거래 상태를 최신으로 확인한 뒤 다시 시도하세요.",
        );
        updateDepositAvailability();
        return;
      }
      const values = depositValues();
      if (!state.depositConfirming) {
        if (!values.basket) {
          showDepositError("계좌를 선택하세요.", $("depBasket"));
          return;
        }
        if (!Number.isFinite(values.amount) || values.amount <= 0) {
          showDepositError("0원보다 큰 금액을 입력하세요.", $("depAmount"));
          return;
        }
        showDepositConfirmation(values);
        return;
      }
      await submitDeposit(values);
    });
    el.depositDialog.addEventListener("close", resetDepositForm);
    el.depositDialog.addEventListener("click", (event) => {
      if (event.target !== el.depositDialog) return;
      const b = el.depositDialog.getBoundingClientRect();
      const inside =
        event.clientX >= b.left &&
        event.clientX <= b.right &&
        event.clientY >= b.top &&
        event.clientY <= b.bottom;
      if (!inside) closeDeposit();
    });

    window.addEventListener("online", () => refreshAll());
    window.addEventListener("offline", () => {
      state.coreError = new Error("오프라인");
      state.coreStatus = "error";
      updateSyncIndicator();
      renderDecision();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshAll();
    });
  }

  async function main() {
    wireEvents();
    initScrollSpy();
    if ("ResizeObserver" in window) {
      new ResizeObserver(scheduleChartPaint).observe(el.chartBox);
    }
    document
      .querySelectorAll("#chartRange button")
      .forEach((b) =>
        b.setAttribute(
          "aria-pressed",
          String(Number(b.dataset.days) === state.chartDays),
        ),
      );
    document.fonts?.ready.then(scheduleChartPaint);
    await refreshAll(true);
    window.setInterval(() => refreshCore(), 30_000);
    window.setInterval(() => refreshSlow(), 60_000);
  }

  main();
})();
