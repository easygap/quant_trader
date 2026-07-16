"use strict";

const $ = (id) => document.getElementById(id);
const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
const compactWon = new Intl.NumberFormat("ko-KR", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const dateOnly = new Intl.DateTimeFormat("ko-KR", {
  year: "numeric",
  month: "long",
  day: "numeric",
});
const dateShort = new Intl.DateTimeFormat("ko-KR", {
  month: "short",
  day: "numeric",
});
const dateTime = new Intl.DateTimeFormat("ko-KR", {
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

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

const elements = {
  basketTracks: $("basketTracks"),
  portfolioSummary: $("portfolioSummary"),
  portfolioAsOf: $("portfolioAsOf"),
  chartAccount: $("chartAccount"),
  chartSummary: $("chartSummary"),
  chartMaturity: $("chartMaturity"),
  chartWrap: $("chartWrap"),
  chartEmpty: $("chartEmpty"),
  chartRows: $("chartDataRows"),
  basketEval: $("basketEval"),
  runtimeOps: $("runtimeOps"),
  runtimeMeta: $("runtimeMeta"),
  syncMark: $("syncMark"),
  syncStatus: $("syncStatus"),
  lastUpdate: $("lastUpdate"),
  modeBadge: $("modeBadge"),
  decisionTitle: $("decisionTitle"),
  decisionDescription: $("decisionDescription"),
  decisionMeta: $("decisionMeta"),
  decisionAction: $("decisionAction"),
  openDeposit: $("openDepositButton"),
  depositAvailability: $("depositAvailability"),
  depositDialog: $("depositDialog"),
  depositForm: $("depositForm"),
  depositFields: $("depositFields"),
  depositConfirm: $("depositConfirm"),
  depositError: $("depositError"),
  depositSubmit: $("depositSubmitButton"),
  depositBack: $("depositBackButton"),
  haltGuidance: $("haltGuidance"),
};

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value == null ? "" : String(value);
  return node.innerHTML
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatWon(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${won.format(number)}원` : "—";
}

function formatPercent(value, { sign = true } = {}) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const prefix = sign && number > 0 ? "+" : "";
  return `${prefix}${number.toFixed(2)}%`;
}

function toneFor(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "muted";
  return number > 0 ? "positive" : "negative";
}

function parseDate(value) {
  if (!value) return null;
  const text = String(value);
  const normalized = /^\d{4}-\d{2}-\d{2}$/.test(text)
    ? `${text}T00:00:00+09:00`
    : text;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatDate(value, formatter = dateOnly) {
  const parsed = parseDate(value);
  return parsed ? formatter.format(parsed) : "—";
}

function localIsoDate(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function calendarAgeDays(value) {
  const parsed = parseDate(value);
  if (!parsed) return null;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(parsed.getFullYear(), parsed.getMonth(), parsed.getDate());
  return Math.max(0, Math.floor((today - target) / 86_400_000));
}

async function fetchJson(url, { timeout = 15_000, options = {}, key = url } = {}) {
  const previous = state.activeRequests.get(key);
  if (previous) previous.abort();

  const controller = new AbortController();
  state.activeRequests.set(key, controller);
  const timer = window.setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const reason = payload && payload.error ? payload.error : `HTTP ${response.status}`;
      throw new Error(reason);
    }
    return payload;
  } finally {
    window.clearTimeout(timer);
    if (state.activeRequests.get(key) === controller) {
      state.activeRequests.delete(key);
    }
  }
}

function setSyncState(kind, label) {
  elements.syncMark.dataset.state = kind;
  elements.syncStatus.textContent = label;
}

function updateSyncIndicator() {
  if (state.coreStatus === "loading") {
    setSyncState("loading", "장부 데이터 확인 중…");
  } else if (state.coreStatus === "error") {
    setSyncState("error", "장부 연결 실패");
  } else if (state.runtimeStatus === "error") {
    setSyncState("partial", "장부 정상 · 안전 상태 확인 불가");
  } else if (state.coreStatus === "partial") {
    setSyncState("partial", "일부 장부 데이터 지연");
  } else {
    setSyncState("ok", "장부 연결 정상");
  }
  updateDepositAvailability();
}

function canRecordDeposit() {
  const baskets = state.baskets;
  const halt = state.runtime && state.runtime.trading_halt;
  return (
    state.coreStatus === "ready"
    && state.runtimeStatus === "ready"
    && ["paper", "live"].includes(state.mode)
    && Array.isArray(baskets)
    && baskets.length > 0
    && baskets.every((basket) => state.flowStatus.get(basket.basket) === "ready")
    && halt
    && halt.halted === false
  );
}

function updateDepositAvailability() {
  const available = canRecordDeposit();
  elements.openDeposit.disabled = !available;
  elements.depositAvailability.textContent = available
    ? "적립금을 기록할 수 있습니다."
    : "장부와 거래 안전 상태가 정상으로 확인된 뒤 적립금을 기록할 수 있습니다.";
}

function markCoreSuccess(timestamp) {
  const parsed = parseDate(timestamp) || new Date();
  state.lastCoreSuccess = parsed;
  state.coreError = null;
  elements.lastUpdate.dateTime = parsed.toISOString();
  elements.lastUpdate.textContent = `마지막 성공 ${dateTime.format(parsed)}`;
}

function setMode(mode) {
  const normalized = String(mode || "unknown").toLowerCase();
  state.mode = ["paper", "live"].includes(normalized) ? normalized : "unknown";
  elements.modeBadge.dataset.mode = state.mode;
  if (state.mode === "paper") {
    elements.modeBadge.textContent = "모의 운용 · 실제 주문 없음";
  } else if (state.mode === "live") {
    elements.modeBadge.textContent = "실전 운용 · 실계좌 주문 가능";
  } else {
    elements.modeBadge.textContent = "운용 모드 확인 불가";
  }
  updateDepositCopy();
}

function sortedBaskets() {
  return [...(state.baskets || [])].sort((a, b) => {
    const primaryDelta = Number(Boolean(b.is_primary)) - Number(Boolean(a.is_primary));
    if (primaryDelta) return primaryDelta;
    return String(a.basket).localeCompare(String(b.basket));
  });
}

function primaryBasket() {
  return sortedBaskets().find((basket) => basket.is_primary) || null;
}

function renderPortfolioSummary() {
  const basket = primaryBasket() || sortedBaskets().find((item) => item.snapshot) || null;
  const assetLabel = state.mode === "live" ? "현재 실전 자산" : "현재 모의 자산";
  if (!basket || !basket.snapshot) {
    elements.portfolioSummary.innerHTML = [
      [assetLabel, "기록 없음"],
      ["누적 원금", "기록 없음"],
      ["원금 대비 손익", "기록 없음"],
      ["현금 비중", "기록 없음"],
    ].map(([label, value]) => `<div><dt>${label}</dt><dd class="muted">${value}</dd></div>`).join("");
    return;
  }

  const totalValue = Number(basket.snapshot.total_value || 0);
  const principal = Number(basket.principal || 0);
  const cash = Number(basket.snapshot.cash || 0);
  const profit = totalValue - principal;
  const cashRatio = totalValue > 0 ? (cash / totalValue) * 100 : null;
  elements.portfolioSummary.setAttribute(
    "aria-label",
    `${basket.display_name} 주력 포트폴리오 요약`,
  );

  const rows = [
    [assetLabel, formatWon(totalValue), ""],
    ["누적 원금", formatWon(principal), ""],
    ["원금 대비 손익", `${profit > 0 ? "+" : ""}${formatWon(profit)}`, toneFor(profit)],
    ["현금 비중", cashRatio == null ? "—" : formatPercent(cashRatio, { sign: false }), ""],
  ];
  elements.portfolioSummary.innerHTML = rows.map(([label, value, tone]) => (
    `<div><dt>${label}</dt><dd class="${tone}">${escapeHtml(value)}</dd></div>`
  )).join("");
}

function positionLabel(position) {
  const name = position.name ? `${position.name} · ` : "";
  return `${name}${position.symbol} ${won.format(Number(position.quantity || 0))}주`;
}

function renderBasketTracks(data) {
  const baskets = (data && data.baskets) || [];
  state.baskets = baskets;
  elements.basketTracks.setAttribute("aria-busy", "false");
  setMode(data && data.mode);
  markCoreSuccess(data && data.timestamp);

  if (!baskets.length) {
    elements.basketTracks.innerHTML = `
      <div class="empty-state">
        <strong>활성화된 포트폴리오가 없습니다.</strong>
        <span><span translate="no">config/baskets.yaml</span>에서 모의 운용 포트폴리오를 먼저 선택하세요.</span>
      </div>`;
    renderPortfolioSummary();
    elements.portfolioAsOf.textContent = "기준일 없음";
    elements.portfolioAsOf.dateTime = "";
    return;
  }

  elements.basketTracks.innerHTML = sortedBaskets().map((basket) => {
    const snapshot = basket.snapshot;
    const twr = snapshot ? Number(snapshot.cumulative_return) : null;
    const profit = basket.profit_vs_principal == null ? null : Number(basket.profit_vs_principal);
    const deployment = basket.deployment_ratio == null ? null : Number(basket.deployment_ratio) * 100;
    const target = basket.design_fraction == null ? null : Number(basket.design_fraction) * 100;
    const primary = Boolean(basket.is_primary);
    const flowItems = state.flows.get(basket.basket) || [];
    const flowState = state.flowStatus.get(basket.basket);
    const latestFlow = flowItems[0];
    const holdings = (basket.positions || []).length
      ? `<ul class="holdings" aria-label="보유 종목">${basket.positions.map((position) => `<li>${escapeHtml(positionLabel(position))}</li>`).join("")}</ul>`
      : '<p class="recent-flow">아직 보유 종목이 없습니다.</p>';
    const allocationWidth = deployment == null ? 0 : Math.max(0, Math.min(100, deployment));
    const role = basket.purpose || (primary ? "월 적립 중심" : "장기 관찰용");
    const plan = basket.contribution_plan || {};
    const planCopy = primary && plan.enabled && Number(plan.amount) > 0
      ? `운용 기준 · 월 ${formatWon(plan.amount)} 적립 · 큰 비중 이탈 때만 리밸런싱`
      : "";
    const allocationGap = deployment == null || target == null ? null : target - deployment;
    const allocationNote = allocationGap != null && allocationGap > 1
      ? (primary
        ? `목표보다 ${Math.round(allocationGap)}%p 낮음 · ETF 1주 단위라 다음 적립 때 조정될 수 있습니다.`
        : `목표보다 ${Math.round(allocationGap)}%p 낮습니다. 관찰 기준에 따라 큰 이탈만 점검합니다.`)
      : "목표 범위에 가깝게 운용 중입니다.";

    return `
      <article class="track-card" data-primary="${primary}">
        <div>
          <div class="track-role">
            <span class="role-label ${primary ? "primary" : ""}">${primary ? "주력 포트폴리오" : "관찰 포트폴리오"}</span>
            <span class="status-label">${escapeHtml(role)}</span>
          </div>
          <h3 class="track-title">${escapeHtml(basket.display_name)}</h3>
          <span class="track-id" translate="no">${escapeHtml(basket.basket)}</span>
          <div class="track-value">${snapshot ? won.format(Number(snapshot.total_value)) : "기록 없음"}${snapshot ? "<small>원</small>" : ""}</div>
          <p class="track-principal">누적 원금 ${formatWon(basket.principal)}${basket.deposits_total > 0 ? ` · 적립 ${formatWon(basket.deposits_total)}` : ""}</p>
          <p class="track-date">${snapshot ? `자산 기준 ${formatDate(snapshot.date)}` : "모의 운용을 실행하면 첫 기록이 만들어집니다."}</p>
          ${planCopy ? `<p class="track-plan">${escapeHtml(planCopy)}</p>` : ""}
        </div>
        <div>
          <dl class="track-metrics">
            <div><dt>입출금 제외 수익 (TWR)</dt><dd class="${toneFor(twr)}">${twr == null ? "—" : formatPercent(twr)}</dd></div>
            <div><dt>원금 대비 손익</dt><dd class="${toneFor(profit)}">${profit == null ? "—" : `${profit > 0 ? "+" : ""}${formatWon(profit)}`}</dd></div>
            <div><dt>고점 대비 최대 하락 (MDD)</dt><dd class="${snapshot ? "negative" : "muted"}">${snapshot ? formatPercent(-Math.abs(Number(snapshot.mdd || 0)), { sign: false }) : "—"}</dd></div>
            <div><dt>현금</dt><dd>${snapshot ? formatWon(snapshot.cash) : "—"}</dd></div>
          </dl>
          <div class="allocation">
            <div class="allocation-copy"><span>투자 배치율</span><strong>${deployment == null ? "—" : `${Math.round(deployment)}%`} / 목표 ${target == null ? "—" : `${Math.round(target)}%`}</strong></div>
            <div class="allocation-bar" role="meter" aria-label="${escapeHtml(basket.display_name)} 투자 배치율" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(allocationWidth)}"><span style="width:${allocationWidth}%"></span></div>
            <p class="allocation-note">${escapeHtml(allocationNote)}</p>
          </div>
          ${holdings}
          ${latestFlow ? `<p class="recent-flow">최근 적립 · ${formatDate(latestFlow.occurred_at, dateShort)} · +${formatWon(latestFlow.amount)}${latestFlow.note ? ` · ${escapeHtml(latestFlow.note)}` : ""}</p>` : ""}
          ${flowState === "error" ? '<p class="recent-flow negative">적립 기록을 확인할 수 없습니다. 새 기록을 추가하지 말고 다시 확인하세요.</p>' : ""}
        </div>
      </article>`;
  }).join("");

  const referenceBasket = primaryBasket() || sortedBaskets().find((basket) => basket.snapshot);
  const referenceDate = referenceBasket && referenceBasket.snapshot && referenceBasket.snapshot.date;
  elements.portfolioAsOf.dateTime = referenceDate || "";
  elements.portfolioAsOf.textContent = referenceDate
    ? `주력 기준 ${formatDate(referenceDate)}`
    : "첫 기록 대기 중";
  renderPortfolioSummary();
}

async function refreshFlows() {
  const baskets = state.baskets || [];
  const tasks = baskets.map(async (basket) => {
    state.flowStatus.set(basket.basket, "loading");
    try {
      const data = await fetchJson(`/api/cash_flows?basket=${encodeURIComponent(basket.basket)}`, {
        timeout: 10_000,
        key: `flows:${basket.basket}`,
      });
      state.flows.set(basket.basket, (data && data.flows) || []);
      state.flowStatus.set(basket.basket, "ready");
      return true;
    } catch {
      state.flowStatus.set(basket.basket, "error");
      return false;
    }
  });
  const results = await Promise.all(tasks);
  state.flowError = results.some((ok) => !ok);
  if (state.baskets) renderBasketTracks({ baskets: state.baskets, mode: state.mode, timestamp: state.lastCoreSuccess });
  renderDecision();
  return !state.flowError;
}

function ensureChartAccountOptions() {
  const wanted = sortedBaskets().map((basket) => ({
    value: basket.account_key,
    label: basket.display_name,
  }));
  wanted.push({ value: "", label: "레거시 기본 계정" });
  const signature = JSON.stringify(wanted);
  if (elements.chartAccount.dataset.signature === signature) return;

  const previous = state.chartAccount;
  elements.chartAccount.dataset.signature = signature;
  elements.chartAccount.innerHTML = wanted.map((option) => (
    `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
  )).join("");
  const exists = previous !== null && wanted.some((option) => option.value === previous);
  elements.chartAccount.value = exists ? previous : (wanted[0] ? wanted[0].value : "");
  state.chartAccount = elements.chartAccount.value;
}

function syncChartQuery() {
  const params = new URLSearchParams(window.location.search);
  params.set("days", String(state.chartDays));
  if (state.chartAccount) params.set("account", state.chartAccount);
  else params.delete("account");
  const queryString = params.toString();
  history.replaceState(null, "", `${window.location.pathname}${queryString ? `?${queryString}` : ""}${window.location.hash}`);
}

function renderChartTable(snapshots) {
  elements.chartRows.innerHTML = snapshots.map((snapshot) => `
    <tr>
      <td><time datetime="${escapeHtml(String(snapshot.date).slice(0, 10))}">${escapeHtml(formatDate(snapshot.date))}</time></td>
      <td class="num">${escapeHtml(formatWon(snapshot.total_value))}</td>
      <td class="num ${toneFor(snapshot.cumulative_return)}">${escapeHtml(formatPercent(snapshot.cumulative_return))}</td>
    </tr>`).join("");
}

function renderChartMaturity() {
  const basket = (state.baskets || []).find((item) => item.account_key === state.chartAccount);
  if (!basket) {
    elements.chartMaturity.textContent = "레거시 기본 계정은 모의 운용 검증 트랙과 분리됩니다.";
    return;
  }
  const evaluation = (state.evaluations || []).find((item) => item.basket === basket.basket);
  if (!evaluation) {
    elements.chartMaturity.textContent = "운용 기록 성숙도를 확인하고 있습니다.";
    return;
  }
  const days = Number(evaluation.progress_days || 0);
  const minimum = Math.max(1, Number(evaluation.min_trading_days || 60));
  if (days < 20) {
    elements.chartMaturity.textContent = `운용 ${days}영업일차 · 장기 추세를 판단하기 전입니다.`;
  } else if (days < minimum) {
    elements.chartMaturity.textContent = `운용 ${days}영업일차 · ${minimum}영업일까지 기록 무결성을 우선 확인합니다.`;
  } else {
    elements.chartMaturity.textContent = `운용 ${days}영업일차 · 장기 기록 검토가 가능한 구간입니다.`;
  }
}

function drawChart(rows) {
  if (!rows.length || elements.chartWrap.hidden) return;
  const canvas = $("chartEquity");
  const width = Math.max(280, elements.chartWrap.clientWidth);
  const height = Math.max(280, elements.chartWrap.clientHeight);
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;

  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const padding = { top: 22, right: 18, bottom: 38, left: width < 480 ? 58 : 72 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const values = rows.map((row) => Number(row.total_value || 0));
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  const spread = Math.max(1, maximum - minimum);
  minimum -= spread * 0.08;
  maximum += spread * 0.08;

  const x = (index) => padding.left + (plotWidth * index) / Math.max(1, rows.length - 1);
  const y = (value) => padding.top + ((maximum - value) / (maximum - minimum)) * plotHeight;

  context.font = '10px "IBM Plex Mono", monospace';
  context.fillStyle = "#5d6966";
  context.strokeStyle = "#e5e1d8";
  context.lineWidth = 1;
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let index = 0; index <= 4; index += 1) {
    const value = maximum - ((maximum - minimum) * index) / 4;
    const lineY = padding.top + (plotHeight * index) / 4;
    context.beginPath();
    context.moveTo(padding.left, lineY);
    context.lineTo(width - padding.right, lineY);
    context.stroke();
    context.fillText(`${compactWon.format(value)}원`, padding.left - 9, lineY);
  }

  context.strokeStyle = "#18201f";
  context.lineWidth = 2;
  context.beginPath();
  rows.forEach((row, index) => {
    const pointX = x(index);
    const pointY = y(values[index]);
    if (index === 0) context.moveTo(pointX, pointY);
    else context.lineTo(pointX, pointY);
  });
  context.stroke();

  const tickStep = Math.max(1, Math.ceil((rows.length - 1) / 5));
  context.textAlign = "center";
  context.textBaseline = "top";
  rows.forEach((row, index) => {
    if (index % tickStep !== 0 && index !== rows.length - 1) return;
    context.fillStyle = "#5d6966";
    context.fillText(formatDate(row.date, dateShort), x(index), height - padding.bottom + 12);
  });

  if (rows.length <= 14) {
    rows.forEach((row, index) => {
      context.beginPath();
      context.arc(x(index), y(values[index]), 3, 0, Math.PI * 2);
      context.fillStyle = "#b84a2a";
      context.fill();
      context.strokeStyle = "#fffdf8";
      context.lineWidth = 1;
      context.stroke();
    });
  }
}

function updateChart(snapshots) {
  const rows = Array.isArray(snapshots) ? snapshots : [];
  state.chartRows = rows;
  renderChartTable(rows);
  renderChartMaturity();

  if (!rows.length) {
    elements.chartWrap.hidden = true;
    elements.chartEmpty.hidden = false;
    elements.chartSummary.textContent = "아직 선택한 기간의 자산 기록이 없습니다.";
    return;
  }

  const first = rows[0];
  const last = rows.at(-1);
  const change = Number(last.total_value || 0) - Number(first.total_value || 0);
  elements.chartSummary.textContent = `${formatDate(first.date, dateShort)}부터 ${formatDate(last.date, dateShort)}까지 ${change >= 0 ? "+" : ""}${formatWon(change)} · 최근 TWR ${formatPercent(last.cumulative_return)}`;
  elements.chartWrap.hidden = false;
  elements.chartEmpty.hidden = true;
  drawChart(rows);
}

async function refreshChart() {
  ensureChartAccountOptions();
  state.chartAccount = elements.chartAccount.value;
  syncChartQuery();
  try {
    const data = await fetchJson(`/api/snapshots?days=${state.chartDays}&account_key=${encodeURIComponent(state.chartAccount || "")}`, {
      timeout: 15_000,
      key: "chart",
    });
    updateChart((data && data.snapshots) || []);
  } catch {
    elements.chartSummary.textContent = "성과 기록을 불러오지 못했습니다. 연결을 확인한 뒤 다시 시도하세요.";
    elements.chartWrap.hidden = true;
    elements.chartEmpty.hidden = false;
    elements.chartEmpty.innerHTML = "<strong>성과 데이터를 확인할 수 없습니다.</strong><span>기존 장부는 변경되지 않았습니다. 잠시 후 다시 확인하세요.</span>";
  }
}

const verdictCopy = {
  PASS_CANDIDATE: ["검토 준비", "positive"],
  FAIL_REVIEW: ["재점검 필요", "negative"],
  WAIT: ["관찰 중", "muted"],
};

function renderEvaluations(evaluations) {
  const items = Array.isArray(evaluations) ? evaluations : [];
  state.evaluations = items;
  elements.basketEval.setAttribute("aria-busy", "false");
  if (!items.length) {
    elements.basketEval.innerHTML = '<p class="empty-inline">검토 중인 포트폴리오가 없습니다.</p>';
    renderDecision();
    return;
  }

  const basketByName = new Map((state.baskets || []).map((basket) => [basket.basket, basket]));
  const ordered = [...items].sort((left, right) => (
    Number(Boolean(basketByName.get(right.basket)?.is_primary))
    - Number(Boolean(basketByName.get(left.basket)?.is_primary))
  ));
  elements.basketEval.innerHTML = `<ol class="review-list">${ordered.map((item) => {
    const basket = basketByName.get(item.basket);
    const displayName = basket?.display_name || item.basket;
    const isPrimary = Boolean(basket?.is_primary);
    const progressDays = Number(item.progress_days || 0);
    const minimumDays = Math.max(1, Number(item.min_trading_days || 60));
    const progress = Math.min(100, Math.round((progressDays / minimumDays) * 100));
    const coverage = item.snapshot_coverage == null ? null : Math.round(Number(item.snapshot_coverage) * 100);
    const [copy, tone] = verdictCopy[item.verdict] || ["관찰 중", "muted"];
    const issues = (item.issues || []).slice(0, 3);
    return `<li class="review-item">
      <div class="review-head"><strong>${escapeHtml(displayName)}${isPrimary ? '<small class="primary-inline">주력</small>' : ""}</strong><span class="review-state ${tone}">${copy}</span></div>
      <div class="review-progress" role="progressbar" aria-label="${escapeHtml(displayName)} 모의 운용 기록 진행률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><span style="width:${progress}%"></span></div>
      <div class="review-detail"><span>${progressDays}/${minimumDays} 영업일</span><span>${coverage == null ? "기록 누락 확인 중" : (coverage >= 100 ? "기록 누락 없음" : `기록 누락 ${Math.max(0, 100 - coverage)}%`)}</span></div>
      ${issues.length ? `<ul class="review-issues">${issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : ""}
    </li>`;
  }).join("")}</ol>`;
  renderChartMaturity();
  renderDecision();
}

function trustCard(label, value, stateName = "info", support = "") {
  return `<div class="trust-card" data-state="${stateName}"><span class="label">${escapeHtml(label)}</span><strong class="value">${value}</strong>${support ? `<span class="support">${escapeHtml(support)}</span>` : ""}</div>`;
}

function marketCopy(regime) {
  const value = String(regime || "").toLowerCase();
  if (value === "bullish") return ["안정", "ok", "신규 매수 허용 구간"];
  if (value === "bearish") return ["방어", "warning", "신규 매수 제한 가능"];
  if (value === "caution") return ["주의", "warning", "포지션 규모 축소 구간"];
  return ["확인 불가", "warning", "시장 상태 데이터 없음"];
}

function strategyCopy(strategy) {
  const value = String(strategy || "").toLowerCase();
  if (value === "scoring") return "종합 점수형";
  if (value === "basket_rebalance") return "포트폴리오 리밸런싱";
  return strategy || "—";
}

function signalCopy(signal) {
  const value = String(signal || "").toUpperCase();
  return ({ BUY: "매수", SELL: "매도", HOLD: "대기" })[value] || signal || "—";
}

function signalSourceCopy(source) {
  const value = String(source || "").toLowerCase();
  return ({ pre_market: "장 시작 전", intraday: "장중", post_market: "장 마감 후" })[value] || source || "—";
}

function formatAgeMinutes(minutes) {
  if (minutes == null) return "기록 없음";
  if (minutes < 60) return `${minutes}분 전`;
  if (minutes < 1_440) return `${Math.round(minutes / 60)}시간 전`;
  return `${Math.round(minutes / 1_440)}일 전`;
}

function renderRuntime(runtime) {
  state.runtime = runtime || null;
  state.runtimeStatus = runtime && runtime.trading_halt ? "ready" : "error";
  updateSyncIndicator();
  elements.runtimeOps.setAttribute("aria-busy", "false");
  if (!runtime) {
    elements.haltGuidance.hidden = true;
    elements.runtimeOps.innerHTML = [
      trustCard("거래 안전 상태", "확인 불가", "warning", "거래 중지 상태를 읽지 못함"),
      trustCard("시장 환경", "확인 불가", "warning", "최근 데이터 없음"),
      trustCard("자동 운용", "확인 불가", "warning", "스케줄러 상태 없음"),
      trustCard("증권사 연결", "확인 불가", "warning", "요청 통계 없음"),
      trustCard("데이터 기준", "확인 불가", "warning", "잠시 후 다시 확인"),
    ].join("");
    elements.runtimeMeta.textContent = "일부 운영 정보를 불러오지 못했습니다. 자산 장부는 변경되지 않았습니다.";
    renderSignals(null, null);
    renderWsGap(null);
    renderDecision();
    return;
  }

  const halt = runtime.trading_halt;
  const haltKnown = Boolean(halt && typeof halt.halted === "boolean");
  const halted = Boolean(halt && halt.halted);
  const [market, marketState, marketSupport] = marketCopy(runtime.market_regime && runtime.market_regime.regime);
  const loop = runtime.loop_metrics;
  const kis = runtime.kis_stats;
  const updatedAt = runtime.runtime_file_updated_at;
  const ageMinutes = updatedAt ? Math.max(0, Math.round((Date.now() - (parseDate(updatedAt)?.getTime() || Date.now())) / 60_000)) : null;
  const freshnessState = ageMinutes == null ? "warning" : (ageMinutes > 30 ? "warning" : "ok");
  const runtimeIsFresh = ageMinutes != null && ageMinutes <= 720;
  const loopElapsed = loop && loop.recent_avg_elapsed_s != null
    ? `${Number(loop.recent_avg_elapsed_s).toFixed(1)}초`
    : null;
  const loopValue = loopElapsed
    ? (runtimeIsFresh ? loopElapsed : "기록 오래됨")
    : "기록 없음";
  const loopSupport = loopElapsed
    ? (runtimeIsFresh ? "최근 루프 평균" : `최근 루프 ${loopElapsed} · ${formatAgeMinutes(ageMinutes)}`)
    : "스케줄러 상태 없음";
  const kisValue = kis && kis.minute_utilization_pct != null
    ? `${Number(kis.minute_utilization_pct).toFixed(1)}%`
    : null;
  const kisCard = state.mode === "paper"
    ? trustCard("증권사 연결", "모의 운용", "info", "실계좌 연결 대상 아님")
    : trustCard("증권사 연결", kisValue ? (runtimeIsFresh ? kisValue : "기록 오래됨") : "기록 없음", kis && runtimeIsFresh ? "info" : "warning", kisValue ? (runtimeIsFresh ? "분당 요청 한도 사용률" : `최근 사용률 ${kisValue} · ${formatAgeMinutes(ageMinutes)}`) : "요청 통계 없음");

  elements.runtimeOps.innerHTML =
    trustCard("거래 안전 상태", !haltKnown ? "확인 불가" : (halted ? "거래 중지" : "운용 가능"), !haltKnown ? "warning" : (halted ? "error" : "ok"), !haltKnown ? "거래 중지 상태를 읽지 못함" : (halted ? (halt.reason || "운영자 확인 필요") : "거래 중지 없음")) +
    trustCard("시장 환경", market, marketState, marketSupport) +
    trustCard("자동 운용", loopValue, loop && runtimeIsFresh ? "ok" : "warning", loopSupport) +
    kisCard +
    trustCard("데이터 기준", formatAgeMinutes(ageMinutes), freshnessState, updatedAt ? formatDate(updatedAt, dateTime) : "스케줄러 데이터 없음");

  const meta = [];
  if (runtime.strategy) meta.push(`운용 전략 ${strategyCopy(runtime.strategy)}`);
  if (updatedAt) meta.push(`스케줄러 마지막 기록 ${formatDate(updatedAt, dateTime)}`);
  elements.runtimeMeta.textContent = meta.join(" · ");
  elements.haltGuidance.hidden = !halted;
  if (halted) {
    $("haltGuidanceReason").textContent = halt.reason || "신규 매수는 명시적으로 해제하기 전까지 차단됩니다.";
  }
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
  count.textContent = `${won.format(rows.length)}건`;
  table.hidden = !rows.length;
  empty.hidden = Boolean(rows.length);
  error.hidden = true;
  empty.textContent = isToday
    ? "오늘 기록된 신호가 없습니다. 이상 상태가 아닙니다."
    : `오늘 기록된 신호가 없습니다. 마지막 신호 기록은 ${formatDate(signalsDate)}입니다.`;
  $("signalRows").innerHTML = rows.map((signal) => `
    <tr>
      <td><time datetime="${escapeHtml(signal.at || "")}">${escapeHtml(formatDate(signal.at, dateTime))}</time></td>
      <td translate="no">${escapeHtml(signal.symbol || "—")}</td>
      <td>${escapeHtml(signalCopy(signal.signal))}</td>
      <td class="num">${Number.isFinite(Number(signal.score)) ? Number(signal.score).toFixed(2) : "—"}</td>
      <td>${escapeHtml(signalSourceCopy(signal.source))}</td>
    </tr>`).join("");
}

function renderWsGap(runtime) {
  const gap = runtime && runtime.ws_gap;
  const summary = $("wsGapSummary");
  const table = $("wsGapTableWrap");
  const empty = $("wsGapEmpty");
  const unavailable = $("wsGapNA");
  if (!gap || !gap.available) {
    summary.innerHTML = trustCard("웹소켓", "정보 없음", "warning", "스케줄러 기록 대기");
    table.hidden = true;
    empty.hidden = true;
    unavailable.hidden = false;
    return;
  }

  const gaps = gap.recent_gaps || [];
  unavailable.hidden = true;
  summary.innerHTML =
    trustCard("웹소켓 상태", gap.is_connected ? "연결됨" : "연결 끊김", gap.is_connected ? "ok" : "error") +
    trustCard("최근 공백", `${won.format(Number(gap.total_gap_count || 0))}건`, gap.total_gap_count > 0 ? "warning" : "ok");
  table.hidden = !gaps.length;
  empty.hidden = Boolean(gaps.length);
  $("wsGapRows").innerHTML = [...gaps].reverse().map((item) => `
    <tr>
      <td>${escapeHtml(formatDate(item.disconnect_at, dateTime))}</td>
      <td>${escapeHtml(formatDate(item.reconnect_at, dateTime))}</td>
      <td class="num">${escapeHtml(`${Number(item.gap_seconds || 0).toFixed(1)}초`)}</td>
      <td>${escapeHtml((item.affected_symbols || []).join(", ") || "—")}</td>
      <td>${item.rest_backfill_performed ? `${won.format(Number(item.rest_backfill_count || 0))}건` : "미수행"}</td>
      <td>${item.blackswan_cooldown_triggered ? '<span class="negative">안전 정지</span>' : (item.blackswan_checked ? "정상" : "—")}</td>
    </tr>`).join("");
}

function renderLegacy(portfolio) {
  state.legacy = portfolio || null;
  if (!portfolio) {
    $("summary").innerHTML = trustCard("레거시 계정", "확인 불가", "warning");
    $("positionsWrap").hidden = true;
    $("noPositions").hidden = false;
    return;
  }
  $("summary").innerHTML =
    trustCard("총 평가금", formatWon(portfolio.total_value), "info") +
    trustCard("총 수익률", formatPercent(portfolio.total_return), Number(portfolio.total_return) >= 0 ? "ok" : "error") +
    trustCard("현금", formatWon(portfolio.cash), "info") +
    trustCard("실현 손익", formatWon(portfolio.realized_pnl), Number(portfolio.realized_pnl) >= 0 ? "ok" : "error") +
    trustCard("최대 낙폭", formatPercent(-Math.abs(Number(portfolio.mdd || 0)), { sign: false }), "warning") +
    trustCard("보유 종목", `${won.format(Number(portfolio.position_count || 0))}개`, "info");

  const positions = portfolio.positions || [];
  $("positionsWrap").hidden = !positions.length;
  $("noPositions").hidden = Boolean(positions.length);
  $("positions").innerHTML = positions.map((position) => `
    <tr>
      <td translate="no">${escapeHtml(position.symbol || "—")}</td>
      <td class="num">${won.format(Number(position.quantity || 0))}</td>
      <td class="num">${escapeHtml(formatWon(position.avg_price))}</td>
      <td class="num">${escapeHtml(formatWon(position.current_price))}</td>
      <td class="num">${escapeHtml(formatWon(position.current_value))}</td>
      <td class="num ${toneFor(position.pnl_rate)}">${escapeHtml(formatPercent(position.pnl_rate))}</td>
    </tr>`).join("");
}

function latestSnapshotDate() {
  const basket = primaryBasket() || sortedBaskets().find((item) => item.snapshot);
  return basket && basket.snapshot ? basket.snapshot.date : null;
}

function currentMonthContributionState(basket) {
  if (!basket || !basket.contribution_plan?.enabled) return "not-planned";
  if (state.flowStatus.get(basket.basket) !== "ready") return "unknown";
  const now = new Date();
  const recorded = (state.flows.get(basket.basket) || []).some((flow) => {
    const when = parseDate(flow.occurred_at);
    return when && when.getFullYear() === now.getFullYear() && when.getMonth() === now.getMonth();
  });
  return recorded ? "recorded" : "empty";
}

function runtimeAgeMinutes() {
  const updatedAt = state.runtime && state.runtime.runtime_file_updated_at;
  const parsed = parseDate(updatedAt);
  return parsed ? Math.max(0, Math.round((Date.now() - parsed.getTime()) / 60_000)) : null;
}

function setDecision({ title, description, meta = "", action = null, actionLabel = "확인하기" }) {
  elements.decisionTitle.textContent = title;
  elements.decisionDescription.textContent = description;
  elements.decisionMeta.innerHTML = meta;
  elements.decisionAction.hidden = !action;
  elements.decisionAction.dataset.action = action || "";
  elements.decisionAction.textContent = actionLabel;
}

function renderDecision() {
  const baskets = state.baskets;
  const halt = state.runtime && state.runtime.trading_halt;
  const latest = latestSnapshotDate();
  const ageDays = calendarAgeDays(latest);
  const issues = (state.evaluations || []).flatMap((item) => item.issues || []);
  const primary = primaryBasket();
  const contributionState = currentMonthContributionState(primary);
  const modeCopy = state.mode === "live" ? "실전 운용" : (state.mode === "paper" ? "모의 운용" : "모드 확인 불가");
  const meta = `<strong>${escapeHtml(modeCopy)}</strong>${latest ? ` · 최근 자산 기록 ${escapeHtml(formatDate(latest))}` : " · 자산 기록 없음"}`;

  if (halt && halt.halted) {
    setDecision({
      title: "거래가 안전하게 중지되어 있습니다",
      description: halt.reason || "체결 또는 장부 상태를 확인하기 전까지 신규 주문을 막고 있습니다.",
      meta,
      action: "operations",
      actionLabel: "운용 상태 보기",
    });
    return;
  }
  if (state.coreError) {
    setDecision({
      title: "자산 데이터를 확인할 수 없습니다",
      description: "이전 화면을 최신 데이터로 표시하지 않았습니다. 연결을 확인한 뒤 다시 시도하세요.",
      meta: state.lastCoreSuccess ? `마지막 성공 ${escapeHtml(dateTime.format(state.lastCoreSuccess))}` : "아직 성공한 갱신이 없습니다.",
      action: "retry",
      actionLabel: "지금 다시 확인",
    });
    return;
  }
  if (state.runtimeStatus === "loading") {
    setDecision({
      title: "거래 안전 상태를 확인하고 있습니다",
      description: "거래 중지 여부와 자동 운용 기록을 확인한 뒤 오늘의 판단을 표시합니다.",
      meta,
    });
    return;
  }
  if (state.runtimeStatus === "error") {
    setDecision({
      title: "거래 안전 상태를 확인할 수 없습니다",
      description: "거래 중지 여부가 확인되기 전에는 적립 기록이나 운용 판단을 진행하지 마세요.",
      meta,
      action: "retry",
      actionLabel: "안전 상태 다시 확인",
    });
    return;
  }
  if (baskets && !baskets.length) {
    setDecision({
      title: "첫 모의 운용 포트폴리오를 연결하세요",
      description: "안전 기본값을 유지한 채 포트폴리오를 활성화하고 첫 모의 운용 기록을 만들어야 합니다.",
      meta: "설정 → 모의 운용 1회 → 첫 자산 기록 확인",
      action: "portfolio",
      actionLabel: "시작 순서 보기",
    });
    return;
  }
  if (baskets && baskets.length && !latest) {
    setDecision({
      title: "첫 모의 운용 기록을 기다리고 있습니다",
      description: "모의 운용을 한 번 실행하면 원금, 자산, 운용 수익률을 분리해 볼 수 있습니다.",
      meta,
      action: "portfolio",
      actionLabel: "포트폴리오 확인",
    });
    return;
  }
  if (ageDays != null && ageDays > 4) {
    setDecision({
      title: "자산 기록이 오래되었습니다",
      description: `마지막 자산 기록이 ${ageDays}일 전입니다. 자동 운용이 중단됐을 수 있으니 상태를 확인하세요.`,
      meta,
      action: "operations",
      actionLabel: "운용 상태 확인",
    });
    return;
  }
  const runtimeAge = runtimeAgeMinutes();
  if (runtimeAge == null || runtimeAge > 720) {
    setDecision({
      title: "자동 운용 기록이 오래되었습니다",
      description: runtimeAge == null
        ? "최근 스케줄러 기록이 없습니다. 오늘 사이클이 실행됐는지 먼저 확인하세요."
        : `마지막 자동 운용 기록이 ${formatAgeMinutes(runtimeAge)}입니다. 적립보다 실행 상태를 먼저 확인하세요.`,
      meta,
      action: "operations",
      actionLabel: "운용 기록 확인",
    });
    return;
  }
  if (issues.length) {
    setDecision({
      title: "확인할 운영 항목이 있습니다",
      description: `${issues.length}개 항목을 검토해야 합니다. 실전 전환은 계속 잠긴 상태입니다.`,
      meta,
      action: "review",
      actionLabel: "검토 항목 보기",
    });
    return;
  }
  if (contributionState === "unknown") {
    setDecision({
      title: "적립 기록을 확인할 수 없습니다",
      description: "조회 상태가 확인되기 전에는 같은 적립금을 다시 기록하지 마세요.",
      meta,
      action: "retry",
      actionLabel: "적립 기록 다시 확인",
    });
    return;
  }
  if (contributionState === "empty") {
    const plannedAmount = Number(primary.contribution_plan?.amount || 0);
    setDecision({
      title: "이번 달 실제 입금 여부를 확인하세요",
      description: `${plannedAmount > 0 ? `운용 기준은 월 ${formatWon(plannedAmount)}입니다. ` : ""}실제 입금 또는 모의 적립이 완료된 경우에만 장부에 기록하세요.`,
      meta,
      action: "deposit",
      actionLabel: "적립금 기록",
    });
    return;
  }
  setDecision({
    title: "오늘은 할 일이 없습니다",
    description: "계획을 유지하며 기록을 더 쌓는 중입니다. 매일 시세를 확인하거나 전략을 바꿀 필요가 없습니다.",
    meta,
  });
}

function updateDepositCopy() {
  const description = $("depositDescription");
  if (!description) return;
  description.textContent = state.mode === "live"
    ? "실제 계좌에 입금이 완료된 뒤 같은 금액을 장부에 기록하세요. 주문은 실행되지 않지만 실전 성과 계산에 반영됩니다."
    : "모의 적립금은 수익이 아니므로 입출금 제외 수익률 계산에서 분리됩니다. 기록 전 포트폴리오와 금액을 다시 살펴보세요.";
}

function resetDepositForm() {
  state.depositConfirming = false;
  state.depositRequestId = null;
  elements.depositForm.reset();
  elements.depositFields.hidden = false;
  elements.depositConfirm.hidden = true;
  elements.depositBack.hidden = true;
  elements.depositSubmit.textContent = "내용 확인";
  elements.depositSubmit.disabled = false;
  elements.depositError.hidden = true;
  elements.depositError.textContent = "";
  document.querySelectorAll("[data-amount]").forEach((button) => button.setAttribute("aria-pressed", "false"));
}

function openDeposit() {
  if (!canRecordDeposit()) {
    showToast("장부와 거래 안전 상태를 먼저 다시 확인하세요.", "error");
    return;
  }
  resetDepositForm();
  const select = $("depBasket");
  const baskets = sortedBaskets();
  select.innerHTML = baskets.map((basket) => `<option value="${escapeHtml(basket.basket)}">${escapeHtml(basket.display_name)}</option>`).join("");
  const primary = primaryBasket();
  if (primary) select.value = primary.basket;
  updateDepositCopy();
  if (!elements.depositDialog.open) elements.depositDialog.showModal();
  window.setTimeout(() => select.focus(), 0);
}

function closeDeposit() {
  if (elements.depositDialog.open) elements.depositDialog.close();
}

function showDepositError(message, field = null) {
  elements.depositError.textContent = message;
  elements.depositError.hidden = false;
  if (field) field.focus();
}

function depositValues() {
  const basket = $("depBasket").value;
  const amount = Number($("depAmount").value);
  const note = $("depNote").value.trim();
  return { basket, amount, note };
}

function showDepositConfirmation(values) {
  state.depositRequestId = window.crypto?.randomUUID
    ? window.crypto.randomUUID()
    : `deposit-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const selected = $("depBasket").selectedOptions[0];
  $("confirmBasket").textContent = selected ? selected.textContent : values.basket;
  $("confirmAmount").textContent = formatWon(values.amount);
  $("confirmMode").textContent = state.mode === "live" ? "실전 장부" : "모의 장부";
  elements.depositFields.hidden = true;
  elements.depositConfirm.hidden = false;
  elements.depositBack.hidden = false;
  elements.depositSubmit.textContent = state.mode === "live" ? "실전 입금 기록" : "모의 적립금 기록";
  elements.depositError.hidden = true;
  state.depositConfirming = true;
  elements.depositBack.focus();
}

function showDepositFields() {
  state.depositConfirming = false;
  state.depositRequestId = null;
  elements.depositFields.hidden = false;
  elements.depositConfirm.hidden = true;
  elements.depositBack.hidden = true;
  elements.depositSubmit.textContent = "내용 확인";
  $("depBasket").focus();
}

async function submitDeposit(values) {
  elements.depositSubmit.disabled = true;
  elements.depositSubmit.textContent = "기록하는 중…";
  elements.depositError.hidden = true;
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
    if (!data || !data.ok) throw new Error((data && data.error) || "기록에 실패했습니다.");
    closeDeposit();
    showToast(`${state.mode === "live" ? "실전 입금" : "모의 적립금"} ${formatWon(data.amount)}을 기록했습니다.`);
    await refreshCore();
  } catch (error) {
    showDepositError(`기록하지 못했습니다. ${error.message || "연결을 확인한 뒤 다시 시도하세요."}`);
    elements.depositSubmit.disabled = false;
    elements.depositSubmit.textContent = state.mode === "live" ? "실전 입금 기록" : "모의 적립금 기록";
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

async function refreshCore() {
  if (document.visibilityState === "hidden") return;
  state.coreStatus = "loading";
  updateSyncIndicator();
  const basketTask = fetchJson("/api/baskets", { timeout: 12_000, key: "baskets" })
    .then(async (data) => {
      renderBasketTracks(data);
      await Promise.allSettled([refreshFlows(), refreshChart()]);
      return data;
    });
  const legacyTask = fetchJson("/api/portfolio", { timeout: 15_000, key: "legacy" })
    .then(renderLegacy);

  const results = await Promise.allSettled([basketTask, legacyTask]);
  const basketResult = results[0];
  if (basketResult.status === "rejected") {
    state.coreError = basketResult.reason || new Error("바스켓 조회 실패");
    state.coreStatus = "error";
    elements.basketTracks.setAttribute("aria-busy", "false");
    elements.basketTracks.innerHTML = '<div class="error-inline">포트폴리오를 불러오지 못했습니다. 이전 데이터를 최신으로 표시하지 않았습니다.</div>';
  } else if (results.some((result) => result.status === "rejected") || state.flowError) {
    state.coreStatus = "partial";
  } else {
    state.coreStatus = "ready";
  }
  updateSyncIndicator();
  renderDecision();
}

async function refreshSlow() {
  if (document.visibilityState === "hidden") return;
  if (!state.runtime) {
    state.runtimeStatus = "loading";
    updateSyncIndicator();
  }
  const evalTask = fetchJson("/api/basket_evaluation", { timeout: 30_000, key: "evaluation" })
    .then((data) => renderEvaluations((data && data.evaluations) || []))
    .catch(() => {
      elements.basketEval.setAttribute("aria-busy", "false");
      elements.basketEval.innerHTML = '<p class="error-inline">모의 운용 검증 상태를 불러오지 못했습니다. 잠시 후 다시 확인하세요.</p>';
    });
  const runtimeTask = fetchJson("/api/runtime", { timeout: 30_000, key: "runtime" })
    .then(renderRuntime)
    .catch(() => renderRuntime(null));
  await Promise.allSettled([evalTask, runtimeTask]);
}

async function refreshAll() {
  await Promise.allSettled([refreshCore(), refreshSlow()]);
}

function wireEvents() {
  elements.openDeposit.addEventListener("click", openDeposit);
  $("closeDepositButton").addEventListener("click", closeDeposit);
  $("depositCancelButton").addEventListener("click", closeDeposit);
  elements.depositBack.addEventListener("click", showDepositFields);
  $("retryButton").addEventListener("click", refreshAll);

  elements.decisionAction.addEventListener("click", () => {
    const action = elements.decisionAction.dataset.action;
    if (action === "deposit") openDeposit();
    else if (action === "retry") refreshAll();
    else if (action) document.getElementById(action)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  elements.chartAccount.addEventListener("change", () => {
    state.chartAccount = elements.chartAccount.value;
    refreshChart();
  });

  $("chartRange").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-days]");
    if (!button) return;
    state.chartDays = Number(button.dataset.days);
    document.querySelectorAll("#chartRange button").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
    refreshChart();
  });

  document.querySelectorAll("[data-amount]").forEach((button) => {
    button.addEventListener("click", () => {
      $("depAmount").value = button.dataset.amount;
      document.querySelectorAll("[data-amount]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      $("depAmount").focus();
    });
  });

  elements.depositForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!canRecordDeposit()) {
      showDepositError("장부와 거래 안전 상태를 최신으로 확인한 뒤 다시 시도하세요.");
      updateDepositAvailability();
      return;
    }
    const values = depositValues();
    if (!state.depositConfirming) {
      if (!values.basket) {
        showDepositError("포트폴리오를 선택하세요.", $("depBasket"));
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

  elements.depositDialog.addEventListener("close", resetDepositForm);
  elements.depositDialog.addEventListener("click", (event) => {
    if (event.target !== elements.depositDialog) return;
    const bounds = elements.depositDialog.getBoundingClientRect();
    const inside = event.clientX >= bounds.left && event.clientX <= bounds.right && event.clientY >= bounds.top && event.clientY <= bounds.bottom;
    if (!inside) closeDeposit();
  });

  window.addEventListener("online", refreshAll);
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

async function boot() {
  wireEvents();
  if ("ResizeObserver" in window) {
    new ResizeObserver(() => drawChart(state.chartRows)).observe(elements.chartWrap);
  }
  document.querySelectorAll("#chartRange button").forEach((button) => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.days) === state.chartDays));
  });
  await refreshAll();
  window.setInterval(refreshCore, 30_000);
  window.setInterval(refreshSlow, 60_000);
}

boot();
