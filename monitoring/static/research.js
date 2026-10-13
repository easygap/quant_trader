/* Research has its own freshness and errors; it never grants trading permission. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const numeric = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value, digits = 1) => numeric(value) ? value.toFixed(digits) : "—";
  const signed = (value) => numeric(value) ? `${value > 0 ? "+" : ""}${value.toFixed(1)}` : "—";
  const cohortName = (name) => ({ basket_equities: "바스켓 구성 종목", sector_etfs: "업종 ETF" })[name] || name || "미분류";
  const issueName = (name) => ({ stale_price: "가격 지연", adjustment_unverified: "수정주가 미확인", volume_unavailable: "거래량 없음" })[name] || (/^incomplete_\d+d_window$/.test(name) ? `${name.match(/\d+/)[0]}일 자료 부족` : name);
  const evidenceName = (status) => ({ CURRENT: "검토 중", STALE: "재검토 필요", UPCOMING: "발표 대기", UNVERIFIED: "확인 부족" })[status] || "상태 미확인";
  const evidenceIssue = (issue) => ({ UNKNOWN_PUBLICATION: "출처의 정확한 공개 시각을 확인하지 못했습니다.", REVIEW_DUE: "예정한 검토 시각이 지났습니다.", EXPIRED: "연구 기록의 유효 기한이 지났습니다.", FUTURE_PUBLICATION: "아직 공개되지 않은 자료입니다." })[issue] || issue;
  const eventLabel = (entry) => entry.event_status === "PASSED" || new Date(entry.event_at).valueOf() <= Date.now() ? "지난 이벤트 · 결과 검토" : "예정 이벤트";
  const safeUrl = (value) => {
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  };
  const date = (value) => {
    if (!value) return "미확인";
    const parsed = new Date(value);
    return Number.isFinite(parsed.valueOf()) ? new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(parsed) : "미확인";
  };
  let desk = null;
  let refreshing = false;
  let reading = false;
  let refreshError = null;
  let errorOrigin = null;

  function renderMarket() {
    const market = desk?.market || {};
    const status = $("researchStatus");
    const old = market.is_stale || market.status === "stale";
    const labels = { ready: "관찰 자료 준비됨", partial: "일부 자료 부족", stale: "이전 거래일 자료 — 갱신 필요", unavailable: "관찰 자료 없음" };
    status.dataset.state = refreshError ? "error" : old ? "stale" : market.status;
    status.textContent = refreshing ? "가격 자료를 수집하고 있습니다. 최대 2분 정도 걸릴 수 있습니다." : refreshError || `${market.replay ? "오프라인 재현 자료 · " : ""}${labels[market.status] || "확인 중"}${market.config_changed ? " · 관찰 설정 변경됨" : ""} · 종가 기준 ${market.as_of || market.expected_session || "미확인"} · 수집 ${date(market.generated_at)}`;
    const rows = Array.isArray(market.observations) ? market.observations : [];
    const cohortSelect = $("researchCohort");
    const selected = cohortSelect.value || "all";
    const cohorts = [...new Set(rows.map((row) => row.cohort))];
    cohortSelect.innerHTML = '<option value="all">전체 관찰 목록</option>' + cohorts.map((name) => `<option value="${escape(name)}">${escape(cohortName(name))}</option>`).join("");
    cohortSelect.value = cohorts.includes(selected) ? selected : "all";
    const filtered = rows.filter((row) => cohortSelect.value === "all" || row.cohort === cohortSelect.value);
    const horizon = $("researchHorizon").value || "60";
    const benchmark = market.benchmark || {};
    const comparable = (row) => Boolean(market.expected_session) && row.as_of === market.expected_session && benchmark.as_of === market.expected_session && !(row.issues || []).includes("adjustment_unverified") && !(benchmark.issues || []).includes("adjustment_unverified") && numeric(row.horizons?.[horizon]?.excess_pct);
    filtered.sort((a, b) => Number(comparable(b)) - Number(comparable(a)) || (comparable(a) ? b.horizons[horizon].excess_pct - a.horizons[horizon].excess_pct : String(a.symbol).localeCompare(String(b.symbol))));
    const breadth = (market.breadth || []).filter((row) => cohortSelect.value === "all" || row.cohort === cohortSelect.value);
    const eligible = breadth.reduce((sum, row) => sum + (row.sma60_denominator || 0), 0);
    const above = breadth.reduce((sum, row) => sum + (row.above_sma60_count || 0), 0);
    const metric = (label, value, note) => `<div class="research-metric"><span>${escape(label)}</span><strong>${escape(value)}</strong><small>${escape(note)}</small></div>`;
    $("researchMetrics").innerHTML = metric("비교 가능한 관찰 종목", `${filtered.filter(comparable).length} / ${filtered.length}`, `${horizon}거래일 · 현재 설정 표본`) + metric("60일 평균선 위", eligible ? `${above} / ${eligible}` : "—", "전체 시장의 상승 종목 비율 아님") + metric(`${benchmark.name || "비교지수"} ${horizon}일`, numeric(benchmark.horizons?.[horizon]?.return_pct) ? `${signed(benchmark.horizons[horizon].return_pct)}%` : "—", "공급자 수정가격 · 배당 처리 상이");
    $("researchRows").innerHTML = filtered.length ? filtered.map((row) => {
      const values = [20, 60, 120].map((period) => {
        const value = row.horizons?.[period]?.excess_pct;
        const width = numeric(value) ? Math.min(100, Math.abs(value) * 2) : 0;
        return `<td class="num">${signed(value)}${String(period) === horizon && numeric(value) ? `<span aria-hidden="true" class="research-bar" style="--bar-width:${width}%"></span>` : ""}</td>`;
      }).join("");
      const issues = (row.issues || []).map(issueName).join(" · ");
      return `<tr><td><strong>${escape(row.name || row.symbol)}</strong><small>${escape(row.symbol)} · ${escape(row.group || cohortName(row.cohort))} · ${row.role === "basket_target" ? "바스켓 설정" : "관찰용"}</small><small>${escape(row.as_of || "기준일 없음")} · ${escape(row.source?.name || "출처 미확인")}${issues ? ` · ${escape(issues)}` : ""}</small></td>${values}<td class="num">${number(row.volume_ratio_20d)}${numeric(row.volume_ratio_20d) ? "배" : ""}</td><td class="num">${number(row.volatility_20d_pct)}${numeric(row.volatility_20d_pct) ? "%" : ""}</td></tr>`;
    }).join("") : '<tr><td colspan="6">시장 자료 갱신으로 관찰을 시작하세요. 기존 계좌의 매매 설정은 바뀌지 않습니다.</td></tr>';
    $("researchTableCaption").textContent = `${benchmark.name || "비교지수"} 대비 가격 수익률 차이(%p). 변동성은 최근 20거래일의 연율화 표준편차.`;
    const limitations = [...(market.limitations || []), "거래량 배수는 마지막 거래일 거래량 ÷ 그 이전 20거래일 평균입니다.", "바스켓 설정 표시는 구성 목록 기준이며 실제 보유 수량이나 편입 추천을 뜻하지 않습니다."];
    const changes = (market.lead_changes || []).map((row) => `${row.name || row.symbol}: ${row.previous_rank}위 → ${row.rank}위 (${row.previous_as_of} 비교)`);
    $("researchLimitations").innerHTML = `<ul>${limitations.map((text) => `<li>${escape(text)}</li>`).join("")}</ul>${changes.length ? `<p>이전 관찰 이후 순위 변화: ${escape(changes.join(" · "))}</p>` : ""}`;
  }

  function renderEvidence() {
    const evidence = desk?.evidence || {};
    const entries = Array.isArray(evidence.entries) ? evidence.entries : [];
    const issues = (evidence.issues || []).map((issue) => `<p class="fine">${escape(issue)}</p>`).join("");
    $("researchTheses").innerHTML = issues + (entries.length ? entries.map((entry) => {
      const links = (entry.sources || []).map((source) => {
        const url = safeUrl(source.url);
        return `<li>${url ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(source.title || source.url)}<span class="sr-only">, 새 탭</span></a>` : escape(source.title || "출처 확인 필요")} <span class="fine">발표 ${source.published_at ? escape(String(source.published_at).slice(0, 10)) : source.published_date ? `${escape(source.published_date)} · 시각 미확인` : "일시 미확인"}</span>${source.publication_note ? `<p class="fine">${escape(source.publication_note)}</p>` : ""}</li>`;
      }).join("");
      const symbols = (entry.symbols || []).map((row) => `${row.symbol}: ${row.rationale}`).join(" · ");
      return `<article class="research-thesis"><header><h4>${escape(entry.title || entry.theme)}</h4><span class="research-evidence-status">${escape(evidenceName(entry.status))}</span></header><p>${escape(entry.thesis)}</p><p class="fine"><strong>반증·재검토 조건</strong> ${escape(entry.invalidation_condition)}</p>${symbols ? `<p class="fine">연결 근거: ${escape(symbols)}</p>` : '<p class="fine">개별 종목의 수혜 여부는 아직 연결하지 않았습니다.</p>'}${entry.event_at ? `<p class="fine"><strong>${escape(eventLabel(entry))}</strong> ${escape(date(entry.event_at))} · 원문 일정 재확인 필요</p>` : ""}<p class="fine">검토 ${escape(date(entry.review_at))} · 유효 기한 ${escape(date(entry.expires_at))} · ${escape(entry.horizon)}</p><details><summary>출처와 반대 근거 · 기록 ${escape(date(entry.observed_at))}</summary><ul>${links}</ul>${(entry.counterevidence || []).map((text) => `<p class="fine">반대 근거·위험: ${escape(text)}</p>`).join("")}${(entry.issues || []).map((text) => `<p class="fine">${escape(evidenceIssue(text))}</p>`).join("")}</details></article>`;
    }).join("") : '<p class="fine">등록된 투자 가설이 없습니다. 원문과 반증 조건을 확인한 연구 기록을 등록하면 여기에 표시됩니다.</p>');
  }

  function render(data) { desk = data; renderMarket(); renderEvidence(); }

  async function load(refresh = false) {
    if (refreshing || reading) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), refresh ? 130000 : 15000);
    reading = !refresh;
    refreshing = refresh;
    $("researchRefresh").disabled = true;
    if (refresh) { refreshError = null; renderMarket(); }
    try {
      const response = await fetch(refresh ? "/api/research/refresh" : "/api/research", { method: refresh ? "POST" : "GET", headers: refresh ? { "X-Requested-With": "quant-dashboard" } : {}, signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "자료를 확인하지 못했습니다.");
      if (refresh || errorOrigin === "read") { refreshError = null; errorOrigin = null; }
      refreshing = false;
      render(data);
    } catch (error) {
      errorOrigin = refresh ? "collection" : "read";
      refreshError = error.name === "AbortError" ? "조회 시간이 초과되었습니다. 표시 중인 자료의 기준일을 확인해 주세요." : "자료를 확인하지 못했습니다. 표시 중인 자료는 이전 수집 결과입니다.";
      refreshing = false;
      renderMarket();
    } finally {
      window.clearTimeout(timer);
      reading = false;
      refreshing = false;
      $("researchRefresh").disabled = false;
    }
  }

  function main() {
    $("researchRefresh").addEventListener("click", () => load(true));
    $("researchCohort").addEventListener("change", renderMarket);
    $("researchHorizon").addEventListener("change", renderMarket);
    load();
    window.setInterval(() => { if (document.visibilityState !== "hidden") load(); }, 60000);
  }
  main();
})();
