/* Saved observations only: this panel never starts orders or initializes a ledger. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  const num = (v, digits = 1) => finite(v) ? v.toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "—";
  const status = (v) => ({ ready: "자료 준비됨", completed: "수집 완료", running: "수집 중", partial: "일부 자료 부족", attention: "확인 필요", unavailable: "자료 없음", stale: "갱신 지연", not_started: "시작 기록 없음", interrupted: "수집 중단", disabled: "자동 수집 꺼짐", collecting: "관측 누적 중", insufficient_sample: "관측 부족", observing: "관측 누적 중", awaiting_forward: "새 종가 관측 대기", ready_for_review: "표본 요건 충족 · 성과 검토 필요", data_gaps: "관측 결측", corrupt: "기록 확인 필요", disk_pressure: "저장 공간 부족", failed: "수집 실패", timeout: "수집 시간 초과", inactive: "활성 검증 없음" })[String(v).toLowerCase()] || v || "미확인";
  const when = (v) => {
    if (!v) return "미확인";
    const d = new Date(v);
    return Number.isFinite(d.valueOf()) ? new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(d) : "미확인";
  };
  const url = (value) => {
    try { const u = new URL(value); return ["https:", "http:"].includes(u.protocol) && !u.username && !u.password ? u.href : null; } catch { return null; }
  };
  const metric = (label, value, note = "") => `<div class="research-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const issueLabel = (value) => ({ dart_status_010: "DART 인증키가 등록되지 않았습니다. 키를 갱신해야 공시를 수집할 수 있습니다.", dart_api_key_missing: "DART 인증키 설정이 필요합니다.", close_sessions_missing: "예상 거래일의 종가 기록이 누락됐습니다.", benchmark_missing: "동일 날짜의 비교지수 자료가 없습니다.", benchmark_source_changed: "비교지수 공급자가 변경되어 비교를 보류합니다.", assessment_stale: "최신 거래일까지 평가가 갱신되지 않았습니다.", collection_stale: "자동 수집 완료 기록이 오래됐습니다.", execution_observation_schema_missing: "새 체결 기록 구조가 아직 적용되지 않았습니다. 다음 운영 시작 시 적용됩니다.", no_new_execution_observations: "아직 새 체결 관측 기록이 없습니다." })[value] || value;
  const issues = (values) => (values || []).map((value) => `<p class="fine">${esc(issueLabel(typeof value === "string" ? value : value.message || value.code || "자료 확인 필요"))}</p>`).join("");

  function render(data) {
    const ops = data.operations || {};
    const daily = data.daily_observation || {};
    const closeJob = daily.jobs?.close || {};
    $("dailyObservationStatus").dataset.state = daily.status === "completed" ? "ready" : "stale";
    const closeStatus = (value) => ({ awaiting_close: "장마감 종가 대기", previous_close_missing: "직전 거래일 종가 누락" })[value] || status(value);
    $("dailyObservationStatus").textContent = `종가 기록 ${closeStatus(closeJob.data_status || closeJob.status || daily.status)} · 종가·수집 통합 ${closeStatus(daily.status)} · 최근 실행 ${when(daily.updated_at)}${daily.expected_close_session ? ` · 확인 대상 ${daily.expected_close_session}` : ""}`;
    $("operationsStatus").dataset.state = ["completed", "running"].includes(ops.status) ? "ready" : "stale";
    const jobs = Object.entries(ops.jobs || {}).map(([key, value]) => `${({ market: "가격", intelligence: "공시·수급", paper_validation: "모의 검증" })[key] || key}: ${status(value.status)}`).join(" · ");
    $("operationsStatus").textContent = `${status(ops.status)} · 최근 실행 ${when(ops.updated_at)}${jobs ? ` · ${jobs}` : ""}${ops.automatic_enabled === false ? " · 자동 실행 꺼짐" : ""}`;
    const intel = data.intelligence || {};
    const disclosures = intel.disclosures || {};
    const flows = intel.investor_flows || {};
    $("intelligenceSummary").innerHTML = metric("공시 원문", status(disclosures.status), `OpenDART · ${num(disclosures.coverage?.ready, 0)}/${num(disclosures.coverage?.requested, 0)}종목 조회 완료`) + metric("투자자별 순매수", status(flows.status), `NAVER · ${num(flows.coverage?.ready, 0)}/${num(flows.coverage?.requested, 0)}종목 · 단위 주`) + metric("수급 기준일", intel.expected_session || intel.as_of || "—", `관측 ${when(intel.generated_at || intel.observed_at)}`);
    const disclosureRows = [...(disclosures.records || [])].sort((a, b) => String(b.published_date).localeCompare(String(a.published_date))).slice(0, 20).map((row) => {
      const link = url(row.url || row.source_url);
      const title = row.report_nm || row.title || row.report_name || "공시";
      return `<li>${esc(row.published_date || row.rcept_dt || "공개일 미확인")} · ${esc(row.corp_name || row.name || row.symbol || "")} · ${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>` : esc(title)}${row.is_correction ? " · 정정" : ""}</li>`;
    }).join("");
    const recentFlows = [...(flows.records || [])].sort((a, b) => String(b.trade_date).localeCompare(String(a.trade_date)) || String(a.symbol).localeCompare(String(b.symbol)));
    const seen = new Set();
    const flowRows = recentFlows.filter((row) => !seen.has(row.symbol) && seen.add(row.symbol)).map((row) => `<tr><td>${esc(row.name || row.symbol)}</td><td>${esc(row.trade_date)}</td><td class="num">${num(row.foreign_net_buy_shares, 0)}</td><td class="num">${num(row.institution_net_buy_shares, 0)}</td><td class="num">${num(row.individual_net_buy_shares, 0)}</td></tr>`).join("");
    const providerIssues = [...new Set([...(disclosures.symbols || []), ...(flows.symbols || [])].flatMap((row) => row.issues || []))];
    $("intelligenceRecords").innerHTML = issues(intel.issues) + issues(providerIssues) + `<p class="fine">공시 ${num((disclosures.records || []).length, 0)}건 · 조회 ${esc(intel.disclosure_window?.start || "—")} ~ ${esc(intel.disclosure_window?.end || "—")} · 정확한 공개 시각이 없는 공시는 일중 분석에 사용할 수 없습니다.</p>${disclosureRows ? `<ul class="operations-disclosures">${disclosureRows}</ul>` : '<p class="fine">표시할 공시가 없습니다. 수집 상태와 조회 범위를 확인하세요.</p>'}<div class="table-scroll" tabindex="0" role="region" aria-label="투자자별 순매수 수량"><table class="research-table"><caption>NAVER 제공 종목별 최근 순매수 수량(주) · 거래소별 집계 범위 미확인 · 거래대금 또는 시장 전체 수급이 아닙니다.</caption><thead><tr><th scope="col">종목</th><th scope="col">기준일</th><th scope="col" class="num">외국인</th><th scope="col" class="num">기관</th><th scope="col" class="num">개인</th></tr></thead><tbody>${flowRows || '<tr><td colspan="5">확인 가능한 수급 자료가 없습니다.</td></tr>'}</tbody></table></div>`;
    renderExecution(data.execution || {});
    renderValidation(data.validation || {});
  }

  function renderExecution(execution) {
    const coverage = execution.coverage || {};
    const groups = Array.isArray(execution.summary_by_basis) ? execution.summary_by_basis : [];
    const brokerLabel = (group) => ({ paper_simulation: "장부 모의", vts: "증권사 모의 서버", production: "증권사 실거래 서버", unknown: "증권사 환경 미확인" })[group.broker_environment] || "환경 미확인";
    $("executionSummary").innerHTML = `<p class="fine">${esc(status(execution.status))} · 조회 ${esc(when(execution.as_of))} · 이전 주문 중 측정 근거 없음 ${num(coverage.legacy_unmeasured_orders, 0)}건${coverage.truncated ? " · 최근 관측 일부만 표시" : ""}</p>` + (groups.length ? groups.map((group) => `<article class="research-thesis"><h4>${esc(({ modeled: "모의 체결", broker_observed: "증권사 확인 체결" })[group.fill_price_basis] || "체결 기준 미확인")} · ${esc(brokerLabel(group))} · ${esc(group.currency)}</h4><p>주문 시도 ${num(group.attempts, 0)}건 · 체결 ${num(group.filled_orders, 0)}건 · 가격 차이 ${num(group.price_shortfall_bps)} bp · 비용 포함 ${num(group.price_plus_fees_bps)} bp</p><p class="fine">기준가격: ${esc(({ basket_plan: "바스켓 계획 시점", decision: "의사결정 시점", executor_input: "주문 실행부에 전달된 가격" })[group.reference_kind] || "미확인")} · 수수료·세금: ${esc(({ modeled: "모의 계산", estimated: "추정값", broker_observed: "증권사 확인", unavailable: "미확인" })[group.fees_basis] || "미확인")} · 1 bp = 0.01%p</p></article>`).join("") : '<p class="fine">새 주문의 기준가격·체결 기록이 쌓이면 비용을 계산합니다. 과거 주문의 기준가격을 추정하지 않습니다.</p>') + issues(execution.reason ? [execution.reason] : []) + issues(execution.issues) + `<details><summary>측정 범위</summary>${issues(execution.limitations)}</details>`;
  }

  function renderValidation(validation) {
    const cohorts = (validation.cohorts || []).filter((row) => row.active !== false);
    $("paperValidation").innerHTML = issues(validation.issues) + (cohorts.length ? cohorts.map((row) => {
      const performance = row.performance || {};
      const regime = (key, label) => { const r = row.regimes?.[key] || {}; return `<p class="fine">${label} ${num(r.observations, 0)} / ${num(row.minimum_regime_observations, 0)}일 · 계좌 ${num(r.portfolio_return_pct)}% · 비교지수 ${num(r.benchmark_return_pct)}%</p>`; };
      return `<article class="research-thesis"><header><h4>${esc(row.basket || row.account_key)}</h4><span class="research-evidence-status">${esc(status(row.status))}</span></header><p>새 관측 ${num(row.return_observations ?? row.observations, 0)} / ${num(row.minimum_observations, 0)}거래일 · 기간 ${num(row.calendar_days, 0)} / ${num(row.minimum_calendar_days, 0)}일 · 시작 ${esc(when(row.started_at))}</p><p class="fine">입출금 조정 수익률 ${num(performance.twr_pct)}% · ${esc(row.benchmark?.name || "비교지수")} ${num(performance.benchmark_return_pct)}% · 차이 ${num(performance.excess_return_pct)}%p</p>${regime("up", "비교지수 상승일")}${regime("down", "비교지수 하락일")}<p class="fine">결측 ${num(row.coverage?.missing_sessions?.length, 0)}일 · 비용 ${num(row.costs?.explicit_cost, 0)}원 · 총 매매금액 / 평균 자산 ${num(row.costs?.gross_turnover_ratio, 2)}배</p>${issues(row.issues)}<details><summary>검증 식별자와 범위</summary><p class="fine operations-wrap">${esc(row.cohort_id)} · ${esc(row.account_key)}</p><p class="fine">입출금을 구간 시작으로 간주한 장부 TWR 근사치입니다. 비용은 자산 평가에 이미 반영되어 다시 차감하지 않습니다. 기존 과거 기록은 새 전진 검증 일수에 포함하지 않습니다. 관측 기간 충족이 실계좌 승인이나 미래 수익 보증을 뜻하지 않습니다.</p></details></article>`;
    }).join("") : '<p class="fine">검증 기준 등록 또는 첫 종가 기록을 기다리고 있습니다. 자동 수집 실행 상태를 확인하세요.</p>');
  }
  let reading = false;
  async function load() {
    if (reading) return;
    reading = true;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/api/research/operations", { signal: controller.signal });
      if (!response.ok) throw new Error("read failed");
      render(await response.json());
    } catch {
      $("operationsStatus").dataset.state = "error";
      $("operationsStatus").textContent = "운용 관측 자료를 읽지 못했습니다. 표시 중인 자료는 이전 조회 결과입니다.";
    } finally { window.clearTimeout(timer); reading = false; }
  }
  function main() { load(); window.setInterval(() => { if (document.visibilityState !== "hidden") load(); }, 60000); }
  main();
})();
