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
