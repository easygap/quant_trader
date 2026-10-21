const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../../monitoring/static/operations.js"), "utf8");
function panel(fetch) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", dataset: {} });
    return elements.get(id);
  };
  const context = vm.createContext({ URL, Intl, Date, AbortController, fetch, document: { getElementById: element }, window: { setTimeout, clearTimeout } });
  vm.runInContext(source.replace(/\n  main\(\);/, "\n  globalThis.panel = {render, load};"), context);
  return { ...context.panel, element };
}
test("missing data and interrupted collection never look healthy", () => {
  const p = panel(); p.render({ operations: { status: "interrupted" }, intelligence: { disclosures: { status: "unavailable" } } });
  assert.match(p.element("operationsStatus").textContent, /수집 중단/);
  assert.match(p.element("intelligenceSummary").innerHTML, /자료 없음/);
});
test("external disclosures are escaped and unsafe links have no anchor", () => {
  const p = panel(); p.render({ intelligence: { disclosures: { records: [{ title: "<script>bad</script>", url: "javascript:alert(1)" }] } } });
  assert.doesNotMatch(p.element("intelligenceRecords").innerHTML, /<script>|href="javascript/);
  assert.match(p.element("intelligenceRecords").innerHTML, /&lt;script&gt;/);
});
test("zero new observations and unknown returns stay distinct", () => {
  const p = panel(); p.render({ validation: { cohorts: [{ active: true, basket: "paper", return_observations: 0, minimum_observations: 120 }] } });
  assert.match(p.element("paperValidation").innerHTML, /0 \/ 120거래일/);
  assert.match(p.element("paperValidation").innerHTML, /수익률 —%/);
});
test("failed reads show a stale warning", async () => {
  const p = panel(async () => ({ ok: false })); await p.load();
  assert.equal(p.element("operationsStatus").dataset.state, "error");
});

test("close recording is distinguished from collection and pre-close waiting", () => {
  const p = panel();
  p.render({ daily_observation: { status: "attention", jobs: { close: { status: "awaiting_close" } } } });
  assert.match(p.element("dailyObservationStatus").textContent, /종가 기록 장마감 종가 대기/);
  assert.match(p.element("dailyObservationStatus").textContent, /통합 확인 필요/);
  assert.equal(p.element("dailyObservationStatus").dataset.state, "stale");
});
test("provider shares, authentication failure and broker environments are explicit", () => {
  const p = panel();
  p.render({ intelligence: { disclosures: { symbols: [{ issues: ["dart_status_010"] }] }, investor_flows: { records: [
    { symbol: "005930", trade_date: "2026-10-12", foreign_net_buy_shares: 12345, institution_net_buy_shares: -100, individual_net_buy_shares: 0 },
    { symbol: "005930", trade_date: "2026-10-09", foreign_net_buy_shares: 999999 }
  ] } }, execution: { summary_by_basis: [{ fill_price_basis: "broker_observed", fees_basis: "estimated", broker_environment: "vts", currency: "KRW", attempts: 1, filled_orders: 1, price_shortfall_bps: 3, price_plus_fees_bps: 5 }] } });
  const html = p.element("intelligenceRecords").innerHTML;
  assert.match(html, /12,345/); assert.doesNotMatch(html, /999,999/);
  assert.match(html, /DART 인증키가 등록되지 않았습니다/);
  const costs = p.element("executionSummary").innerHTML;
  assert.match(costs, /증권사 모의 서버/); assert.match(costs, /추정값/);
});

function history(overrides = {}) {
  return {
    basket: "장기 보유", account_key: "paper:hold", status: "observed", observations: 8,
    return_observations: 7, version_count: 2,
    performance: { start_date: "2026-10-13", end_date: "2026-10-22", twr_pct: 4.25, benchmark_return_pct: 3, excess_return_pct: 1.25, drawdown_pct: -0.5 },
    coverage: { missing_sessions: [], benchmark_missing_sessions: [] }, issues: [],
    versions: [
      { cohort_id: "old", active: false, started_at: "2026-10-13T00:00:00Z", ended_at: "2026-10-21T00:00:00Z", fingerprint: { code: "abcdef123456789" }, registration_reason: ["first_registration"], observations: 6, return_observations: 5, performance: { start_date: "2026-10-13", end_date: "2026-10-20", twr_pct: 2 }, issues: [] },
      { cohort_id: "new", active: true, started_at: "2026-10-21T00:00:00Z", fingerprint: { code: "1234567890abcd" }, registration_reason: ["code_changed", "config_changed"], observations: 2, return_observations: 1, performance: { start_date: "2026-10-21", end_date: "2026-10-22", twr_pct: 1 }, issues: [] }
    ], comparisons: [], segments: [], ...overrides
  };
}

test("cumulative history retains ended versions without inflating current validation samples", () => {
  const p = panel();
  p.render({ validation: { performance_history: [history()], cohorts: [{ active: true, basket: "장기 보유", return_observations: 1, minimum_observations: 120 }] } });
  const html = p.element("paperValidation").innerHTML;
  assert.match(html, /전체 누적 성과/); assert.match(html, /종가 8개 \/ 수익 구간 7개/);
  assert.match(html, /입출금 조정 수익률 4.25%/);
  assert.match(html, /버전 1 · 종료/); assert.match(html, /버전 2 · 현재/);
  assert.match(html, /abcdef1234/); assert.doesNotMatch(html, /abcdef123456789/);
  assert.match(html, /운용 코드 변경 · 운용 설정 변경/);
  assert.match(html, /1 \/ 120거래일/); assert.doesNotMatch(html, /7 \/ 120거래일/);
});

test("unverified continuity suppresses aggregate values and shows verified segments", () => {
  const p = panel();
  p.render({ validation: { performance_history: [history({ status: "continuity_unverified", performance: { twr_pct: 999 }, coverage: { missing_sessions: ["2026-10-16"], benchmark_missing_sessions: ["2026-10-21"] }, segments: [{ observations: 3, return_observations: 2, performance: { start_date: "2026-10-13", end_date: "2026-10-15", twr_pct: 1.75 } }] })] } });
  const html = p.element("paperValidation").innerHTML;
  assert.match(html, /전체 기간 연결 미확인/); assert.doesNotMatch(html, /999%/);
  assert.match(html, /누적 수익률을 보류/); assert.match(html, /연속 구간 1/);
  assert.match(html, /입출금 조정 수익률 1.75%/);
  assert.match(html, /종가 결측 1일 · 비교지수 결측 1일/);
});

test("matched comparison displays equal windows and keeps transition separate", () => {
  const p = panel();
  p.render({ validation: { performance_history: [history({ comparisons: [{ before_cohort_id: "old", after_cohort_id: "new", changed_at: "2026-10-21T00:00:00Z", change_reasons: ["code_changed"], transition: { status: "linked", start_date: "2026-10-20", end_date: "2026-10-21", twr_pct: 1.2, benchmark_return_pct: 0.7, excess_return_pct: 0.5 }, matched_window: { status: "observed", intervals: 1, before: { start_date: "2026-10-19", end_date: "2026-10-20", twr_pct: 0.5 }, after: { start_date: "2026-10-21", end_date: "2026-10-22", twr_pct: 1 }, twr_change_pp: 0.5, excess_change_pp: -0.3 } }] })] } });
  const html = p.element("paperValidation").innerHTML;
  assert.match(html, /버전 1 → 버전 2/); assert.match(html, /변경 경계 구간: 수익률 연결 확인/);
  assert.match(html, /동일 길이 비교: 관측 성과 · 각 1개 수익 구간/);
  assert.match(html, /2026-10-19 ~ 2026-10-20/); assert.match(html, /2026-10-21 ~ 2026-10-22/);
  assert.match(html, /변경 경계 구간 제외/);
  assert.match(html, /변경 후 − 변경 전: 수익률 0.5%p · 비교지수 대비 차이 -0.3%p/);
  assert.match(html, /코드 변경의 효과나 우열을 뜻하지 않습니다/);
});

test("first observation and disconnected comparisons never turn missing returns into zero", () => {
  const p = panel();
  p.render({ validation: { performance_history: [history({ status: "insufficient_sample", observations: 1, return_observations: 0, performance: null, versions: [], comparisons: [{ transition: { status: "unlinked", twr_pct: 777 }, matched_window: { status: "insufficient_sample", intervals: 0, before: { twr_pct: 888 } } }] })] } });
  const html = p.element("paperValidation").innerHTML;
  assert.match(html, /입출금 조정 수익률 — · 비교지수 —/);
  assert.match(html, /변경 경계 구간: 연결 보류/);
  assert.match(html, /동일 길이 비교: 관측 부족 · 각 0개 수익 구간/);
  assert.doesNotMatch(html, /777%|888%|입출금 조정 수익률 0%/);
});

test("historical names, dates, reasons and issue text are escaped", () => {
  const p = panel();
  const unsafe = "<img src=x onerror=alert(1)>";
  p.render({ validation: { performance_history: [history({ basket: unsafe, performance: { start_date: unsafe }, issues: [unsafe], versions: [{ cohort_id: "x", fingerprint: { code: unsafe }, registration_reason: [unsafe], performance: { end_date: unsafe }, issues: [unsafe] }], comparisons: [{ change_reasons: [unsafe], transition: { status: unsafe, start_date: unsafe, issues: [unsafe] }, matched_window: { status: unsafe, issues: [unsafe] } }] })] } });
  const html = p.element("paperValidation").innerHTML;
  assert.doesNotMatch(html, /<img/); assert.match(html, /&lt;img/);
});

test("refresh preserves expanded history by account and section when cards reorder or versions grow", () => {
  const p = panel();
  const element = p.element("paperValidation");
  let html = "";
  let details = [];
  // Replacing innerHTML creates new closed nodes, as it does in the browser.
  Object.defineProperty(element, "innerHTML", {
    get: () => html,
    set: (value) => {
      html = value;
      details = [...value.matchAll(/<details data-validation-detail="([^"]+)">/g)].map((match) => ({
        open: false,
        dataset: { validationDetail: match[1].replace(/&(quot|amp|lt|gt|#39);/g, (_, name) => ({ quot: '"', amp: "&", lt: "<", gt: ">", "#39": "'" })[name]) }
      }));
    }
  });
  element.querySelectorAll = () => details;
  const key = (account, section) => JSON.stringify([account, section]);
  const node = (account, section) => details.find((item) => item.dataset.validationDetail === key(account, section));
  p.render({ validation: { performance_history: [history(), history({ account_key: "paper:pocket", basket: "적립" })] } });
  node("paper:hold", "versions").open = true;
  node("paper:hold", "comparisons").open = true;
  node("paper:pocket", "limitations").open = true;
  const previous = node("paper:hold", "versions");
  p.render({ validation: { performance_history: [
    history({ account_key: "paper:new", basket: "새 계좌" }),
    history({ account_key: "paper:pocket", basket: "적립" }),
    history({ basket: "이름 변경", version_count: 3, versions: [...history().versions, { cohort_id: "third", active: true }] })
  ] } });
  assert.notEqual(node("paper:hold", "versions"), previous);
  assert.equal(node("paper:hold", "versions").open, true);
  assert.equal(node("paper:hold", "comparisons").open, true);
  assert.equal(node("paper:pocket", "limitations").open, true);
  assert.equal(node("paper:pocket", "versions").open, false);
  assert.equal(node("paper:new", "versions").open, false);
  node("paper:hold", "versions").open = false;
  p.render({ validation: { performance_history: [history()] } });
  assert.equal(node("paper:hold", "versions").open, false);
  assert.equal(node("paper:hold", "comparisons").open, true);
});

test("history calculation scope exposes saved limitations with escaped text", () => {
  const p = panel();
  p.render({ validation: { performance_history: [history({ limitations: [
    "저장 당시 검증된 관측이며 현재 DB를 다시 감사한 결과가 아닙니다.",
    "버전 변경 경계의 수익률은 계좌 누적 성과에만 포함합니다.",
    "비용은 기존 TWR에 반영되어 다시 차감하지 않습니다.",
    "<script>unsafe</script>"
  ] })] } });
  const html = p.element("paperValidation").innerHTML;
  assert.match(html, /누적 성과 계산 범위/);
  assert.match(html, /저장 당시 검증된 관측/);
  assert.match(html, /버전 변경 경계의 수익률/);
  assert.match(html, /비용은 기존 TWR에 반영되어 다시 차감하지 않습니다/);
  assert.doesNotMatch(html, /<script>/); assert.match(html, /&lt;script&gt;/);
});
