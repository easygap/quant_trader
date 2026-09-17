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
  function sinceCopy(minutes) {
    if (minutes == null) return "";
    if (minutes < 60) return `${Math.max(1, Math.round(minutes))}분째`;
    if (minutes < 1440) return `${Math.round(minutes / 60)}시간째`;
    return `${Math.round(minutes / 1440)}일째`;
  }
  function parseDate(value) {
    if (!value) return null;
    const text = String(value);
    const normalized = /^\d{4}-\d{2}-\d{2}$/.test(text)
      ? `${text}T00:00:00+09:00`
      : /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(text)
        ? `${text.replace(" ", "T")}+09:00`
        : text;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  const pad2 = (n) => String(n).padStart(2, "0");
  function fmtLong(value) {
    const d = parseDate(value);
    return d
      ? `${d.getFullYear()}년 ${d.getMonth() + 1}월 ${d.getDate()}일`
      : "—";
  }
  function fmtMD(value) {
    const d = parseDate(value);
    return d ? `${d.getMonth() + 1}월 ${d.getDate()}일` : "—";
  }
  function fmtDT(value) {
    const d = parseDate(value);
    return d
      ? `${d.getMonth() + 1}월 ${d.getDate()}일 ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
      : "—";
  }
  function fmtHM(d) {
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }
  function formatAge(minutes) {
    if (minutes == null) return "기록 없음";
    if (minutes < 1) return "방금";
    if (minutes < 60) return `${Math.round(minutes)}분 전`;
    if (minutes < 1440) return `${Math.round(minutes / 60)}시간 전`;
    return `${Math.round(minutes / 1440)}일 전`;
  }
  function localIsoDate(value = new Date()) {
    return `${value.getFullYear()}-${pad2(value.getMonth() + 1)}-${pad2(value.getDate())}`;
  }
  function calendarAgeDays(value) {
    const parsed = parseDate(value);
    if (!parsed) return null;
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const target = new Date(
      parsed.getFullYear(),
      parsed.getMonth(),
      parsed.getDate(),
    );
    return Math.max(0, Math.floor((today - target) / 86_400_000));
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
    ink: "#3157a4",
    up: "#c4383c",
    down: "#2563c7",
    warn: "#976019",
  };
  const inkAlpha = (a) => `rgba(48, 60, 78, ${a})`;

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
    else setSync("ok", "연결 정상");
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
    state.mode = ["paper", "live"].includes(normalized)
      ? normalized
      : "unknown";
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
    if (created && localIsoDate(created) === localIsoDate(d)) return created;
    d.setHours(23, 59, 59, 999);
    return d;
  }

  /** 스냅샷·입금 기록으로 평가금액, 투자원금(계단), 시간가중 자산, 낙폭 시계열을 만든다. */
  function buildSeries(rows, flows, initialCapital) {
    const sorted = [...rows]
      .filter((r) => parseDate(r.date))
      .sort((a, b) => parseDate(a.date) - parseDate(b.date));
    const dates = sorted.map((r) => parseDate(r.date));
    const value = sorted.map((r) => Number(r.total_value || 0));
    const cr = sorted.map((r) => Number(r.cumulative_return || 0));
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
    const principal = instants.map(
      (at) =>
        initialCapital +
        sortedFlows
          .filter((f) => at && f.at <= at)
          .reduce((s, f) => s + f.amount, 0),
    );
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
    series.dates.forEach((d, i) => {
      const key = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
      if (!current || current.key !== key) {
        if (current) out.push(current);
        current = {
          key,
          label: `${d.getFullYear() !== new Date().getFullYear() ? `${String(d.getFullYear()).slice(2)}년 ` : ""}${d.getMonth() + 1}월`,
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
    const pnl = snap ? Number(snap.total_value) - Number(b.principal) : null;
    $("overviewPnl").textContent = snap
      ? `원금 대비 ${signedWon(pnl)} (${pct(b.principal > 0 ? (pnl / b.principal) * 100 : null)})`
      : "모의투자를 실행하면 자산 기록이 표시됩니다.";
    $("overviewPnl").className = tone(pnl);
    $("overviewPrincipal").textContent = won(b?.principal);
    $("overviewDeposits").textContent = b?.deposits_total
      ? `적립금 ${won(b.deposits_total)} 포함`
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
    $("allocationList").innerHTML =
      `<div><dt><i class="asset-dot"></i>보유 자산</dt><dd>${won(snap ? snap.total_value - snap.cash : null)}</dd></div><div><dt><i class="cash-dot"></i>현금</dt><dd>${won(snap?.cash)}</dd></div>`;
  }

  function latestSnapshotDate() {
    const b = selectedBasket();
    return b && b.snapshot ? b.snapshot.date : null;
  }
  function currentMonthContributionState(basket) {
    if (!basket || !basket.contribution_plan?.enabled) return "not-planned";
    if (state.flowStatus.get(basket.basket) !== "ready") return "unknown";
    const now = new Date();
    const recorded = (state.flows.get(basket.basket) || []).some((flow) => {
      const when = parseDate(flow.occurred_at);
      return (
        when &&
        when.getFullYear() === now.getFullYear() &&
        when.getMonth() === now.getMonth()
      );
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
  }
  function renderDecision() {
    const baskets = state.baskets;
    const halt = state.runtime && state.runtime.trading_halt;
    const latest = latestSnapshotDate();
    const ageDays = calendarAgeDays(latest);
    const primary = selectedBasket();
    const issues = (state.evaluations || [])
      .filter((item) => item.basket === primary?.basket)
      .flatMap((item) => item.issues || []);
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
    if (ageDays != null && ageDays > 4) {
      setDecision({
        title: `자산 기록이 ${ageDays}일째 멈춰 있습니다`,
        description: `마지막 기록은 ${fmtLong(latest)}입니다. 자동매매가 멈췄을 수 있으니 운영 상태부터 확인하세요.`,
        action: "operations",
        actionLabel: "운영 상태 보기",
      });
      return;
    }
    const loopMetrics = state.runtime && state.runtime.loop_metrics;
    const loopLastAt =
      parseDate(loopMetrics && loopMetrics.last_success) ||
      parseDate(state.runtime && state.runtime.runtime_file_updated_at);
    const runtimeAge = loopLastAt
      ? Math.max(0, Math.round((Date.now() - loopLastAt.getTime()) / 60_000))
      : null;
    if (runtimeAge == null || runtimeAge > 720) {
      setDecision({
        title:
          runtimeAge == null
            ? "자동매매 실행 기록이 없습니다"
            : `자동매매가 ${sinceCopy(runtimeAge)} 실행되지 않았습니다`,
        description:
          runtimeAge == null
            ? "스케줄러 기록이 보이지 않습니다. 오늘 사이클이 돌았는지 먼저 확인하세요."
            : `마지막 실행은 ${fmtDT(loopLastAt)}입니다. 적립보다 스케줄러 상태를 먼저 확인하세요.`,
        action: "operations",
        actionLabel: "운영 상태 보기",
      });
      return;
    }
    if (issues.length) {
      const paperOnly = (state.evaluations || []).find(
        (item) => item.basket === primary?.basket,
      )?.paper_only;
      setDecision({
        title:
          paperOnly && issues.length === 1
            ? "새 운용 규칙을 검증하고 있습니다"
            : `확인이 필요한 항목이 ${issues.length}건 있습니다`,
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
        const principal = Number(b.principal || 0);
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
    el.chartRows.innerHTML = series.rows
      .map(
        (row, i) => `<tr>
      <td><time datetime="${escapeHtml(String(row.date).slice(0, 10))}">${escapeHtml(fmtLong(row.date))}</time></td>
      <td class="num">${won(row.total_value)}</td>
      <td class="num">${won(series.principal[i])}</td>
      <td class="num ${tone(row.cumulative_return)}">${pct(row.cumulative_return)}</td>
    </tr>`,
      )
      .join("");
  }

  function renderMonthStrip(series, history) {
    const visibleMonths = new Set(
      series.dates.map((d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`),
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

  function equityGeometry(series, width, height) {
    const padding = {
      top: 20,
      right: 12,
      bottom: 28,
      left: width < 480 ? 52 : 64,
    };
    const n = series.rows.length;
    const x = (i) =>
      padding.left +
      ((width - padding.left - padding.right) * i) / Math.max(1, n - 1);
    const scale = makeScale(
      [...series.value, ...series.principal],
      padding.top,
      height - padding.bottom,
      0.08,
    );
    return { padding, x, scale, width, height, n };
  }

  function drawChart(rows) {
    if (!chart.series || el.chartWrap.hidden) return;
    const series = chart.series;
    const width = Math.max(1, el.chartBox.clientWidth);
    const height = Math.max(200, el.chartBox.clientHeight);
    const context = setupCanvas(el.chartEquity, width, height);
    const g = equityGeometry(series, width, height);
    chart.geometry = g;
    const { padding, x, scale, n } = g;
    context.font = `400 11.5px ${FONT}`;
    context.textBaseline = "middle";

    context.textAlign = "right";
    niceTicks(scale.min, scale.max, 4).forEach((v, i) => {
      const y = scale.y(v);
      context.strokeStyle = inkAlpha(i === 4 ? 0.34 : 0.12);
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(padding.left, y);
      context.lineTo(width - padding.right, y);
      context.stroke();
      context.fillStyle = inkAlpha(0.7);
      context.fillText(compactWon(v), padding.left - 8, y);
    });
    const step = Math.max(1, Math.ceil((n - 1) / (width < 480 ? 3 : 6)));
    context.fillStyle = inkAlpha(0.7);
    for (let i = 0; i < n - 1; i += step) {
      if (n > 1 && i > n - 1 - step * 0.5) break;
      context.textAlign = i === 0 ? "left" : "center";
      context.fillText(
        fmtMD(series.rows[i].date),
        x(i),
        height - padding.bottom + 14,
      );
    }
    context.textAlign = n > 1 ? "right" : "left";
    context.fillText(
      fmtMD(series.rows[n - 1].date),
      x(n - 1),
      height - padding.bottom + 14,
    );

    const visible = Math.max(1, Math.ceil((n - 1) * chart.progress) + 1);
    const clipRight = n > 1 ? x(0) + (x(n - 1) - x(0)) * chart.progress : width;
    context.save();
    context.beginPath();
    context.rect(0, 0, clipRight + 1, height);
    context.clip();

    context.save();
    context.setLineDash([3, 4]);
    context.strokeStyle = inkAlpha(0.5);
    context.lineWidth = 1;
    context.beginPath();
    series.principal.forEach((v, i) => {
      if (i >= visible) return;
      const px = x(i);
      const py = scale.y(v);
      if (i === 0) context.moveTo(px, py);
      else {
        context.lineTo(px, scale.y(series.principal[i - 1]));
        context.lineTo(px, py);
      }
    });
    context.stroke();
    context.restore();

    series.markers.forEach((m) => {
      if (m.index >= visible) return;
      const mx = x(m.index);
      context.strokeStyle = inkAlpha(0.28);
      context.beginPath();
      context.moveTo(mx, padding.top);
      context.lineTo(mx, height - padding.bottom);
      context.stroke();
      context.fillStyle = inkAlpha(0.7);
      context.font = `500 11.5px ${FONT}`;
      context.textAlign = mx > width * 0.8 ? "right" : "left";
      context.fillText(
        `${signedWon(m.amount)} 적립`,
        mx + (mx > width * 0.8 ? -5 : 5),
        padding.top - 8,
      );
      context.font = `400 11.5px ${FONT}`;
    });

    context.strokeStyle = COLORS.ink;
    context.lineWidth = 1.6;
    context.lineJoin = "round";
    context.beginPath();
    series.value.forEach((v, i) => {
      if (i >= visible) return;
      const px = x(i);
      const py = scale.y(v);
      if (i === 0) context.moveTo(px, py);
      else context.lineTo(px, py);
    });
    context.stroke();
    if (n <= 14) {
      series.value.forEach((v, i) => {
        if (i >= visible) return;
        context.beginPath();
        context.arc(x(i), scale.y(v), 2.5, 0, Math.PI * 2);
        context.fillStyle = COLORS.ink;
        context.fill();
      });
    }
    context.restore();

    if (chart.hover != null && chart.progress >= 1) {
      const i = chart.hover;
      const px = x(i);
      const py = scale.y(series.value[i]);
      context.save();
      context.setLineDash([2, 3]);
      context.strokeStyle = inkAlpha(0.5);
      context.beginPath();
      context.moveTo(px, padding.top);
      context.lineTo(px, height - padding.bottom);
      context.stroke();
      context.restore();
      context.beginPath();
      context.arc(px, py, 4.5, 0, Math.PI * 2);
      context.fillStyle = COLORS.ink;
      context.fill();
      context.strokeStyle = "#ffffff";
      context.lineWidth = 2;
      context.stroke();
    }
    drawDrawdown(series);
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

  function hideChartTip() {
    if (chart.hover == null) return;
    chart.hover = null;
    el.chartTip.hidden = true;
    drawChart();
  }
  function handleChartPointer(event) {
    const series = chart.series;
    const g = chart.geometry;
    if (!series || !g || chart.progress < 1) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - bounds.left;
    const plotWidth = g.width - g.padding.left - g.padding.right;
    const index = clamp(
      Math.round(((px - g.padding.left) / Math.max(1, plotWidth)) * (g.n - 1)),
      0,
      g.n - 1,
    );
    if (index === chart.hover) return;
    chart.hover = index;
    const row = series.rows[index];
    const tip = el.chartTip;
    tip.querySelector("time").textContent = fmtLong(row.date);
    tip.querySelector("strong").textContent = won(row.total_value);
    const twr = tip.querySelector("span");
    twr.textContent = `투자원금 ${won(series.principal[index])} · 시간가중 ${pct(row.cumulative_return)} · 낙폭 ${pct(series.dd[index] * 100, { digits: 1 })}`;
    twr.className = tone(row.cumulative_return);
    const tipX = clamp(g.x(index), 110, g.width - 110);
    tip.style.transform = `translate(${tipX}px, ${g.scale.y(series.value[index])}px) translate(-50%, calc(-100% - 12px))`;
    tip.hidden = false;
    drawChart();
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
    chart.series = series;
    state.chartRows = series.rows;
    chart.hover = null;
    el.chartTip.hidden = true;
    renderChartMaturity();

    if (!series.rows.length) {
      el.chartWrap.hidden = true;
      el.chartEmpty.hidden = false;
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
    renderChartTable(series);
    renderMonthStrip(series, history);
    drawChart();
  }

  async function refreshChart() {
    ensureChartAccountOptions();
    state.chartAccount = el.chartAccount.value;
    updateOverview();
    syncChartQuery();
    try {
      const data = await fetchJson(
        `/api/snapshots?days=${state.chartDays}&account_key=${encodeURIComponent(state.chartAccount || "")}`,
        { timeout: 15_000, key: "chart" },
      );
      updateChart((data && data.snapshots) || []);
    } catch {
      el.chartSummary.textContent =
        "성과 기록을 불러오지 못했습니다. 연결을 확인한 뒤 다시 시도하세요.";
      el.chartWrap.hidden = true;
      el.chartEmpty.hidden = false;
      el.chartEmpty.innerHTML =
        "<strong>성과 기록을 읽지 못했습니다.</strong><span>계좌 기록은 바뀌지 않았습니다. 잠시 후 다시 확인하세요.</span>";
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
        const [copy, cls] = verdictCopy[item.verdict] || ["관찰 중", "warn"];
        const issues = (item.issues || []).slice(0, 3);
        return `<div class="review-item">
        <p class="review-name">${escapeHtml(name)}${basket?.is_primary ? " · 주력" : ""}</p>
        <div class="review-progress">
          <div class="bar" role="progressbar" aria-label="${escapeHtml(name)} 검증 진행" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><i style="width:${progress}%"></i></div>
          <p><span>${item.paper_only ? `전체 운영 ${days}거래일` : `${days} / ${minimum} 거래일`}</span><span>${coverage == null ? "기록 누락 확인 중" : coverage >= 100 ? "기록 누락 없음" : `기록 누락 ${Math.max(0, 100 - coverage)}%`}</span></p>
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
    const loopFresh = loopAge != null && loopAge <= 720;
    const autoValue = loopElapsed
      ? loopFresh
        ? "정상"
        : `${formatAge(loopAge)} 실행`
      : "기록 없음";
    const autoDetail = loopElapsed
      ? `마지막 실행 ${fmtDT(loopLast)} · 루프 ${loopElapsed}`
      : "스케줄러 기록 없음";
    const autoState = loop && loopFresh ? "ok" : "warning";
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
    const isToday = !signalsDate || signalsDate === localIsoDate();
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
      await refreshCore();
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
  async function refreshCore(force = false) {
    if (!force && document.visibilityState === "hidden") return;
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
      state.flowError
    ) {
      state.coreStatus = "partial";
    } else {
      state.coreStatus = "ready";
    }
    updateSyncIndicator();
    renderDecision();
  }

  async function refreshSlow(force = false) {
    if (!force && document.visibilityState === "hidden") return;
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
        el.basketEval.setAttribute("aria-busy", "false");
        el.basketEval.innerHTML =
          '<p class="loading">검증 상태를 불러오지 못했습니다. 잠시 후 다시 확인하세요.</p>';
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
    el.chartEquity.addEventListener("pointerleave", hideChartTip);

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
      new ResizeObserver(() => drawChart()).observe(el.chartBox);
    }
    document
      .querySelectorAll("#chartRange button")
      .forEach((b) =>
        b.setAttribute(
          "aria-pressed",
          String(Number(b.dataset.days) === state.chartDays),
        ),
      );
    await refreshAll(true);
    window.setInterval(() => refreshCore(), 30_000);
    window.setInterval(() => refreshSlow(), 60_000);
  }

  main();
})();
