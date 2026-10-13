const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../../monitoring/static/research.js"), "utf8");

function research(fetch) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", value: id === "researchHorizon" ? "60" : "all", dataset: {} });
    return elements.get(id);
  };
  const context = vm.createContext({ URL, Intl, Date, AbortController, fetch, document: { getElementById: element }, window: { setTimeout, clearTimeout } });
  vm.runInContext(source.replace(/\n  main\(\);/, "\n  globalThis.research = { render, renderMarket, load, safeUrl };"), context);
  return { ...context.research, element };
}
function row(symbol, excess, cohort = "sector_etfs") {
  return { symbol, name: symbol, rank: excess == null ? null : 1, cohort, as_of: "2026-10-12", source: { name: "fdr" }, horizons: { "60": excess == null ? null : { excess_pct: excess } } };
}

test("missing prices remain unknown and stale research stays labelled", () => {
  const r = research();
  r.render({ market: { status: "stale", is_stale: true, as_of: "2026-10-12", observations: [row("111111", null)] } });
  assert.equal(r.element("researchStatus").dataset.state, "stale");
  assert.match(r.element("researchRows").innerHTML, /—/);
  assert.doesNotMatch(r.element("researchRows").innerHTML, /0\.0/);
});

test("relative strength sorts available evidence and filters the observation cohort", () => {
  const r = research();
  r.render({ market: { status: "ready", expected_session: "2026-10-12", benchmark: { as_of: "2026-10-12" }, observations: [row("weak", -2), row("unknown", null), row("strong", 5), row("holding", 50, "basket_equities")] } });
  r.element("researchCohort").value = "sector_etfs";
  r.renderMarket();
  const html = r.element("researchRows").innerHTML;
  assert.ok(html.indexOf("strong") < html.indexOf("weak"));
  assert.ok(html.indexOf("weak") < html.indexOf("unknown"));
  assert.doesNotMatch(html, /holding/);
});

test("external evidence and names are escaped and unsafe links never become anchors", () => {
  const r = research();
  r.render({ market: { observations: [row('<img src=x onerror=alert(1)>', 1)] }, evidence: { entries: [{ title: '<script>alert(1)</script>', thesis: '<b>thesis</b>', status: "UNVERIFIED", sources: [{ url: "javascript:alert(1)", title: "bad" }, { url: "https://example.com/source", title: "good" }] }] } });
  assert.doesNotMatch(r.element("researchRows").innerHTML, /<img/);
  const html = r.element("researchTheses").innerHTML;
  assert.doesNotMatch(html, /<script>|href="javascript:/);
  assert.match(html, /href="https:\/\/example.com\/source"/);
  assert.match(html, /확인 부족/);
});

test("refresh explicitly posts once and a failed refresh never shows a success state", async () => {
  let calls = 0;
  const r = research(async (url, options) => { calls++; assert.equal(url, "/api/research/refresh"); assert.equal(options.method, "POST"); return { ok: false, json: async () => ({ error: "failed" }) }; });
  await r.load(true);
  assert.equal(calls, 1);
  assert.equal(r.element("researchStatus").dataset.state, "error");
  assert.equal(r.element("researchRefresh").disabled, false);
});

test("short history can be compared over20days while60day breadth remains unknown", () => {
  const r = research();
  r.element("researchHorizon").value = "20";
  const short = row("new-etf", null);
  short.horizons["20"] = { excess_pct: 2 };
  r.render({ market: { expected_session: "2026-10-12", benchmark: { as_of: "2026-10-12" }, observations: [short], breadth: [{ cohort: "sector_etfs", eligible: 1, sma60_denominator: 0, above_sma60_count: 0 }] } });
  const html = r.element("researchMetrics").innerHTML;
  assert.match(html, /1 \/ 1/);
  assert.match(html, /60일 평균선 위<\/span><strong>—/);
  assert.doesNotMatch(html, /0 \/ 1/);
});

test("past corporate events are shown as results to review, not upcoming events", () => {
  const r = research();
  r.render({ evidence: { entries: [{ title: "earnings", event_at: "2020-10-01T01:00:00Z", event_status: "PASSED" }] } });
  assert.match(r.element("researchTheses").innerHTML, /지난 이벤트 · 결과 검토/);
  assert.doesNotMatch(r.element("researchTheses").innerHTML, /예정 이벤트/);
});

test("a recovered read clears its error but an old report cannot clear a failed collection", async () => {
  const responses = [false, true, false, true];
  const r = research(async () => ({ ok: responses.shift(), json: async () => ({ market: { status: "ready", as_of: "2026-10-12" } }) }));
  await r.load();
  assert.equal(r.element("researchStatus").dataset.state, "error");
  await r.load();
  assert.equal(r.element("researchStatus").dataset.state, "ready");
  await r.load(true);
  await r.load();
  assert.equal(r.element("researchStatus").dataset.state, "error");
});

test("offline replay and configuration changes remain visible", () => {
  const r = research();
  r.render({ market: { status: "stale", replay: true, config_changed: true } });
  assert.match(r.element("researchStatus").textContent, /오프라인 재현 자료/);
  assert.match(r.element("researchStatus").textContent, /관찰 설정 변경됨/);
});
