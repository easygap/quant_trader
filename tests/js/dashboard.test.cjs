const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// 실제 배포 스크립트를 실행한다. main의 자동 조회만 빼고 DOM과 통신을 대체한다.
const source = fs.readFileSync(
  path.join(__dirname, "../../monitoring/static/dashboard.js"),
  "utf8",
);
function dashboard({ now = "2026-10-01T00:15:00+09:00", fetch } = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id))
      elements.set(id, {
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: true,
        open: false,
        dataset: {},
        style: { setProperty() {} },
        attributes: {},
        clientWidth: 960,
        clientHeight: 380,
        classList: { add() {}, remove() {}, toggle() {} },
        setAttribute(k, v) {
          this.attributes[k] = v;
        },
        removeAttribute(k) {
          delete this.attributes[k];
        },
        querySelectorAll() {
          return [];
        },
        querySelector() {
          return element(`${id}:child`);
        },
        getContext() {
          return new Proxy({}, { get: () => () => {} });
        },
        addEventListener() {},
      });
    return elements.get(id);
  };
  class Clock extends Date {
    constructor(...args) {
      super(...(args.length ? args : [now]));
    }
    static now() {
      return Date.parse(now);
    }
  }
  const context = vm.createContext({
    Intl,
    URLSearchParams,
    AbortController,
    console,
    setTimeout,
    clearTimeout,
    Date: Clock,
    fetch:
      fetch ||
      (() => {
        throw Error("예상하지 않은 통신");
      }),
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    window: {
      location: { search: "", pathname: "/", hash: "" },
      matchMedia: () => ({ matches: true }),
      devicePixelRatio: 1,
      setTimeout,
      clearTimeout,
    },
    document: {
      getElementById: element,
      visibilityState: "visible",
      querySelectorAll: () => [],
    },
    history: { replaceState() {} },
  });
  const exports =
    "parseDate, fmtLong, fmtDT, calendarAgeDays, rowInstant, buildSeries, monthlyReturns, currentMonthContributionState, renderSignals, refreshCore, refreshSlow, refreshChart, updateChart, renderChartTable, moveHistoryTable, state, chart";
  assert.match(source, /\n  main\(\);\s*\}\)\(\);\s*$/);
  vm.runInContext(
    source.replace(/\n  main\(\);/, `\n  globalThis.dashboard = {${exports}};`),
    context,
  );
  return { ...context.dashboard, element };
}

for (const timezone of ["Asia/Seoul", "America/Los_Angeles", "Europe/London"]) {
  test(`${timezone}: 날짜와 시각은 한국 시간으로 표시`, () => {
    process.env.TZ = timezone;
    const d = dashboard();
    assert.equal(d.fmtLong("2026-09-22"), "2026년 9월 22일");
    assert.equal(d.fmtDT("2026-09-22T10:30:00"), "9월 22일 10:30");
    assert.equal(d.fmtDT("2026-09-22 01:30:00+00:00"), "9월 22일 10:30");
    assert.equal(
      d.parseDate("2026-09-22T10:30:00").toISOString(),
      "2026-09-22T01:30:00.000Z",
    );
    assert.equal(d.calendarAgeDays("2026-09-30"), 1);
  });

  test(`${timezone}: 월말과 월초의 수익률을 다른 달로 묶지 않음`, () => {
    process.env.TZ = timezone;
    const d = dashboard();
    const series = d.buildSeries(
      [
        { date: "2026-07-31", total_value: 110000, cumulative_return: 10 },
        { date: "2026-08-01", total_value: 132000, cumulative_return: 32 },
      ],
      [],
      100000,
    );
    const months = d.monthlyReturns(series);
    assert.equal(months.length, 2);
    assert.equal(months[0].key, "2026-07");
    assert.equal(months[1].key, "2026-08");
    assert.ok(Math.abs(months[0].value - 10) < 1e-8);
    assert.ok(Math.abs(months[1].value - 20) < 1e-8);
  });

  test(`${timezone}: 저장 이후 입금과 복원된 과거 기록을 구분`, () => {
    process.env.TZ = timezone;
    const d = dashboard();
    const rows = [
      {
        date: "2026-09-30",
        created_at: "2026-09-30T15:00:00",
        total_value: 100000,
        cumulative_return: 0,
      },
      {
        date: "2026-10-01",
        created_at: "2026-10-10T15:00:00",
        total_value: 130000,
        cumulative_return: 0,
      },
    ];
    const series = d.buildSeries(
      rows,
      [
        { occurred_at: "2026-09-30T16:00:00", amount: 10000 },
        { occurred_at: "2026-10-01 23:00:00", amount: 20000 },
        { occurred_at: "2026-10-02T00:00:00+09:00", amount: 50000 },
      ],
      100000,
    );
    assert.deepEqual(Array.from(series.principal), [100000, 130000]);
    assert.equal(
      d.rowInstant(rows[1]).toISOString(),
      "2026-10-01T14:59:59.999Z",
    );
  });

  test(`${timezone}: 한국 날짜로 오늘 신호와 이번 달 적립을 확인`, () => {
    process.env.TZ = timezone;
    const d = dashboard();
    d.state.flowStatus.set("test", "ready");
    d.state.flows.set("test", [
      { occurred_at: "2026-09-30T23:30:00", amount: 10000 },
    ]);
    assert.equal(
      d.currentMonthContributionState({
        basket: "test",
        contribution_plan: { enabled: true },
      }),
      "empty",
    );
    d.renderSignals(
      [{ symbol: "069500", at: "2026-10-01T00:01:00" }],
      "2026-10-01",
    );
    assert.equal(d.element("signalCount").textContent, "1건");
  });
}

function prepareChart(d, rows) {
  d.state.baskets = [
    {
      basket: "test",
      account_key: "test",
      display_name: "테스트 계좌",
      initial_capital: 100000,
    },
  ];
  d.state.chartAccount = "test";
  d.state.series.set("test", rows);
  d.state.seriesStatus.set("test", "ready");
  d.state.flowStatus.set("test", "ready");
}

test("조회 시작일도 한국 날짜를 사용", async () => {
  process.env.TZ = "America/Los_Angeles";
  const d = dashboard();
  const rows = ["2026-08-31", "2026-09-01", "2026-09-02"].map((date) => ({
    date,
    total_value: 100000,
    cumulative_return: 0,
  }));
  prepareChart(d, rows);
  d.state.chartDays = 30;
  await d.refreshChart();
  assert.deepEqual(
    Array.from(d.chart.series.rows, (r) => r.date),
    ["2026-09-01", "2026-09-02"],
  );
});

test("접어 둔 일별 표는 만들지 않고 열었을 때 최신 기록으로 생성", () => {
  const d = dashboard();
  const rows = [
    { date: "2026-09-22", total_value: 100000, cumulative_return: 0 },
  ];
  prepareChart(d, rows);
  d.updateChart(rows);
  assert.equal(d.element("chartDataRows").innerHTML, "");
  d.element("historyDetails").open = true;
  d.renderChartTable(d.chart.series);
  assert.match(d.element("chartDataRows").innerHTML, /2026-09-22/);
});

test("계좌 조회 실패 시 이전 계좌의 표와 낙폭을 숨김", async () => {
  const d = dashboard();
  const rows = [
    { date: "2026-09-22", total_value: 100000, cumulative_return: 0 },
  ];
  prepareChart(d, rows);
  d.element("historyDetails").open = true;
  d.element("historyDetails").hidden = false;
  d.updateChart(rows);
  assert.equal(d.element("historyDetails").hidden, false);
  d.state.seriesStatus.set("test", "error");
  await d.refreshChart();
  assert.equal(d.element("historyDetails").hidden, true);
  assert.equal(d.element("chartDataRows").innerHTML, "");
  assert.equal(d.element("exportHistory").disabled, true);
  assert.equal(d.element("historyLatest").disabled, true);
});

test("일별 표는 100개씩 넘기되 차트 원본 기록은 모두 유지", () => {
  const d = dashboard();
  const rows = Array.from({ length: 250 }, (_, i) => ({
    date: new Date(Date.UTC(2026, 0, 1 + i)).toISOString().slice(0, 10),
    total_value: 100000 + i,
    cumulative_return: i / 1000,
  }));
  prepareChart(d, rows);
  d.element("historyDetails").open = true;
  d.updateChart(rows);
  assert.equal(d.chart.series.rows.length, 250);
  assert.equal(
    d.element("historyTablePosition").textContent,
    "201–250 / 250개 기록",
  );
  assert.equal(
    (d.element("chartDataRows").innerHTML.match(/<tr>/g) || []).length,
    50,
  );
  assert.equal(d.element("historyTableNext").disabled, true);
  d.moveHistoryTable(-1);
  assert.equal(
    d.element("historyTablePosition").textContent,
    "101–200 / 250개 기록",
  );
  assert.equal(
    (d.element("chartDataRows").innerHTML.match(/<tr>/g) || []).length,
    100,
  );
  assert.match(
    d.element("chartDataRows").innerHTML,
    new RegExp(rows[100].date),
  );
  d.moveHistoryTable(-1);
  assert.equal(
    d.element("historyTablePosition").textContent,
    "1–100 / 250개 기록",
  );
  assert.equal(d.element("historyTablePrev").disabled, true);
});

test("누락된 시각은 오늘 날짜로 대신 채우지 않음", () => {
  const d = dashboard();
  assert.equal(d.fmtLong(undefined), "—");
  assert.equal(d.calendarAgeDays(undefined), null);
  d.state.flowStatus.set("test", "ready");
  d.state.flows.set("test", [{ amount: 100000 }]);
  assert.equal(
    d.currentMonthContributionState({
      basket: "test",
      contribution_plan: { enabled: true },
    }),
    "empty",
  );
});

test("계좌 새로고침이 겹쳐도 진행 중인 요청을 취소하거나 중복 전송하지 않음", async () => {
  const calls = [];
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const d = dashboard({
    fetch: async (url, options) => {
      calls.push({ url, signal: options.signal });
      await gate;
      return {
        ok: true,
        json: async () =>
          url === "/api/baskets"
            ? { baskets: [], mode: "paper" }
            : { snapshots: [] },
      };
    },
  });
  const first = d.refreshCore(true);
  const second = d.refreshCore(true);
  release();
  await Promise.all([first, second]);
  assert.equal(calls.filter((c) => c.url === "/api/baskets").length, 1);
  assert.equal(calls.filter((c) => c.url === "/api/portfolio").length, 1);
  assert.ok(calls.every((c) => !c.signal.aborted));
});

test("운영 상태 새로고침도 진행 중인 조회 결과를 함께 기다림", async () => {
  const calls = [];
  const d = dashboard({
    fetch: async (url, options) => {
      calls.push({ url, signal: options.signal });
      return { ok: true, json: async () => null };
    },
  });
  await Promise.all([d.refreshSlow(true), d.refreshSlow(true)]);
  assert.equal(calls.length, 2);
  assert.ok(calls.every((c) => !c.signal.aborted));
});
