// Pternas dashboard: the cultural & economic relationship-arbitrage book.
// Renders /api/summary (read-only; no live API calls server-side) and
// refreshes every 60s. Motion via anime.js v4; every animation is skipped
// under prefers-reduced-motion.
import { animate, stagger } from "https://cdn.jsdelivr.net/npm/animejs@4.5.0/+esm";
import { DotField, FiberHelix, Lcd } from "/assets/visuals.js?v=6";

const REFRESH_MS = 60_000;
const STRATEGY = "relation_arb";
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (id) => document.getElementById(id);
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---- formatting ---------------------------------------------------------------
const MINUS = "−";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (x, signed = true) => {
  if (x === null || x === undefined || Number.isNaN(x)) return "--";
  const s = "$" + Math.abs(x).toFixed(2);
  if (!signed) return (x < 0 ? MINUS : "") + s;
  return (x > 0 ? "+" : x < 0 ? MINUS : "±") + s;
};
const pct = (x, d = 1) => (x === null || x === undefined ? "--" : (x * 100).toFixed(d) + "%");
const signClass = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
function ago(iso) {
  if (!iso) return "--";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return Math.max(0, Math.floor(s)) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
// One wording for every empty state, by the owner's choice.
const EMPTY_TEXT = "No data yet, check back later.";
const emptyNote = () => `<div class="empty">${EMPTY_TEXT}</div>`;
const table = (head, rows) =>
  `<table><thead><tr>${head.map(([h, cls]) => `<th class="${cls || ""}">${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;

// ---- motion helpers -------------------------------------------------------------
const shown = new Map(); // element id -> last value shown, so refreshes tween from it

/** Count a number up (or down) to `value` in `el`, formatted by `fmt`. */
function countTo(el, value, fmt, { duration = 900 } = {}) {
  if (!el) return;
  const key = el.id;
  const from = shown.get(key);
  shown.set(key, value);
  // Nothing to tween (or motion off): write it now. A re-render with an
  // unchanged value -- e.g. a filter click rebuilding the stat boxes --
  // otherwise left the "--" placeholder, since anime skips a no-op tween.
  if (value === null || value === undefined || reduce || from === value) { el.textContent = fmt(value); return; }
  const firstShow = from === undefined || from === null;
  const o = { v: firstShow ? 0 : from };
  animate(o, { v: value, duration: firstShow ? duration + 300 : duration, ease: "outExpo", onUpdate: () => (el.textContent = fmt(o.v)) });
}

function revealOnce() {
  if (window.__revealed) return;
  window.__revealed = true;
  if (reduce) return;
  animate("[data-reveal]", { opacity: [0, 1], translateY: [14, 0], duration: 800, ease: "outExpo", delay: stagger(80) });
}

const seenRows = new WeakSet();
function staggerRows(container) {
  if (reduce || !container) return;
  const rows = [...container.querySelectorAll("tbody tr")].filter((r) => !seenRows.has(r));
  rows.forEach((r) => seenRows.add(r));
  if (rows.length) animate(rows, { opacity: [0, 1], translateX: [-6, 0], duration: 500, ease: "outQuad", delay: stagger(22) });
}

// ---- theme ------------------------------------------------------------------------
let dots, helix, lastData = null;
function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(theme, persist) {
  document.documentElement.dataset.theme = theme;
  if (persist) { try { localStorage.setItem("pternas-theme", theme); } catch (e) { /* private mode etc. */ } }
  document.querySelectorAll("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === theme)));
  dots?.refreshColor();
  helix?.refreshColors();
  if (lastData) renderCharts(lastData, { animateLines: false });
}
document.querySelectorAll("[data-theme-set]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.themeSet, true)));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  let saved = null;
  try { saved = localStorage.getItem("pternas-theme"); } catch (e) { /* ignore */ }
  if (!saved) { delete document.documentElement.dataset.theme; applyTheme(currentTheme(), false); }
});

// ---- hero visuals ---------------------------------------------------------------
dots = new DotField($("dots"));
helix = new FiberHelix($("helix"));
const lcd = new Lcd($("pnl-lcd"), { decimals: 2 });
lcd.render(0);
window.pternas = { dots, helix }; // handy for poking at the visuals from devtools

// The helix wanders on its own: scrolling or pointer activity sends it off
// to strata or ribbon, it dwells there a few seconds and comes back, and an
// idle page still takes an excursion every so often.
(() => {
  const AWAY = ["strata", "ribbon"];
  const DWELL_MS = 5200, COOLDOWN_MS = 4000, IDLE_MS = 13000, MORPH_MS = 1700;
  let busy = false, lastChange = performance.now(), returnTimer = 0, activity = 0;

  function go(mode) {
    if (!helix.setMode(mode)) return false;
    busy = true;
    lastChange = performance.now();
    animate(helix, { morph: [0, 1], duration: MORPH_MS, ease: "inOutQuart", onComplete: () => (busy = false) });
    return true;
  }
  function excursion() {
    if (reduce || busy || helix.to !== "helix" || performance.now() - lastChange < COOLDOWN_MS) return;
    if (!go(AWAY[Math.floor(Math.random() * AWAY.length)])) return;
    clearTimeout(returnTimer);
    returnTimer = setTimeout(() => go("helix"), MORPH_MS + DWELL_MS);
  }
  function nudge(amount, threshold) {
    activity += amount;
    if (activity >= threshold) { activity = 0; excursion(); }
  }
  let lastY = window.scrollY, lastPt = null;
  window.addEventListener("scroll", () => { nudge(Math.abs(window.scrollY - lastY), 180); lastY = window.scrollY; }, { passive: true });
  window.addEventListener("pointermove", (e) => {
    if (lastPt) nudge(Math.hypot(e.clientX - lastPt.x, e.clientY - lastPt.y), 1400);
    lastPt = { x: e.clientX, y: e.clientY };
  }, { passive: true });
  setInterval(() => { if (performance.now() - lastChange > IDLE_MS) excursion(); }, 1500);
})();

setInterval(() => { $("clock").textContent = new Date().toISOString().slice(11, 19) + " UTC"; }, 1000);

// ---- data shaping ---------------------------------------------------------------
/** This strategy's slice of the book (totals and per-position rows). */
function book(d) {
  const s = d.paper_trading.pnl_summary || {};
  const t = (s.by_strategy || {})[STRATEGY] || {};
  return {
    total: t.total_pnl ?? 0, realized: t.realized_pnl ?? 0, unrealized: t.unrealized_pnl ?? 0, capital: t.open_cost ?? 0,
    open: t.open_count ?? 0, closed: t.closed_count ?? 0, wins: t.wins ?? 0,
    positions: (s.positions || []).filter((p) => p.strategy === STRATEGY),
    scoredAt: s.generated_at,
  };
}
/** Equity points for this strategy. Rows written before per-strategy detail
 *  existed only carry its total, so realized/capital fall back to null. */
function curve(d) {
  return (d.paper_trading.equity_curve || [])
    .map((r) => {
      const det = (r.by_strategy_detail || {})[STRATEGY];
      const total = det ? det.total_pnl : (r.by_strategy || {})[STRATEGY];
      return { ts: r.ts, total: total ?? null, realized: det ? det.realized_pnl : null, capital: det ? det.open_cost : null };
    })
    .filter((p) => p.total !== null);
}

// ---- renderers --------------------------------------------------------------------
function renderHero(d) {
  const b = book(d), rel = d.relations || {};
  const sign = $("pnl-sign");
  sign.textContent = b.total > 0 ? "+" : b.total < 0 ? MINUS : "±";
  sign.className = "lcd-sign num " + signClass(b.total);
  const from = shown.get("lcd") ?? 0;
  shown.set("lcd", b.total);
  if (reduce) lcd.render(b.total);
  else { const o = { v: from }; animate(o, { v: b.total, duration: 1400, ease: "outExpo", onUpdate: () => lcd.render(o.v) }); }

  countTo($("h-realized"), b.realized, usd);
  countTo($("h-unrealized"), b.unrealized, usd);
  countTo($("h-capital"), b.capital, (x) => usd(x, false));
  $("h-realized").className = "v " + signClass(b.realized);
  $("h-unrealized").className = "v " + signClass(b.unrealized);

  $("hero-foot").innerHTML =
    `<span><b>${(rel.pairs_judged ?? 0).toLocaleString()}</b> market pairs judged</span>` +
    `<span><b>${(rel.opportunities_logged ?? 0).toLocaleString()}</b> violations priced</span>` +
    `<span><b>${rel.traded ?? 0}</b> paper-traded</span>` +
    `<span><b>${b.open}</b> open positions</span>`;
}

function renderBook(d) {
  const b = book(d), p = d.paper_trading;
  const rows = [
    ["Entries / exits", `${(p.fills_by_strategy || {})[STRATEGY] ?? 0} / ${(p.exits_by_strategy || {})[STRATEGY] ?? 0}`],
    ["Open positions", b.open],
    ["Closed", b.closed],
    ["Win rate (closed)", b.closed ? pct(b.wins / b.closed, 0) : "--"],
    ["Last scoring pass", ago(b.scoredAt)],
  ];
  $("book-table").innerHTML = rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join("");
}

let equityChart, capitalChart;
function chartTheme() {
  return {
    s1: cssVar("--series-1"), s2: cssVar("--series-2"), ink: cssVar("--ink"), ink2: cssVar("--ink-2"),
    ink3: cssVar("--ink-3"), grid: cssVar("--grid-line"), rule: cssVar("--rule-strong"), bg: cssVar("--bg"), font: cssVar("--sans"),
  };
}
function baseOptions(t) {
  const tick = { color: t.ink3, font: { family: t.font, size: 10.5 } };
  return {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: t.ink, titleColor: t.bg, bodyColor: t.bg, borderWidth: 0, cornerRadius: 0, padding: 10,
        titleFont: { family: t.font, size: 11 }, bodyFont: { family: t.font, size: 11 }, boxWidth: 8, boxHeight: 2,
        callbacks: { label: (c) => ` ${c.dataset.label}  ${usd(c.parsed.y)}` },
      },
    },
    scales: {
      x: { ticks: { ...tick, maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false }, border: { color: t.rule } },
      y: { ticks: { ...tick, callback: (v) => usd(v) }, grid: { color: t.grid }, border: { display: false }, grace: "12%" },
    },
  };
}

function renderCharts(d, { animateLines = true } = {}) {
  const t = chartTheme(), pts = curve(d);
  const labels = pts.map((p) => new Date(p.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }));
  const has = pts.length > 0;
  $("equity-empty").hidden = has;
  $("equity-empty").textContent = EMPTY_TEXT;
  document.querySelectorAll("#equityChart, #capitalChart").forEach((c) => (c.parentElement.hidden = !has));
  const last = pts[pts.length - 1] || {};
  $("equity-legend").innerHTML =
    `<span><i style="background:${t.s1}"></i>Total ${esc(usd(last.total ?? null))}</span>` +
    `<span><i style="background:${t.s2}"></i>Realized ${esc(usd(last.realized ?? null))}</span>`;

  const anim = reduce || !animateLines ? false : { duration: 1100, easing: "easeOutQuart" };
  const point = pts.length > 40 ? 0 : 3;
  const line = (label, key, color) => ({
    label, data: pts.map((p) => p[key]), borderColor: color, backgroundColor: color, borderWidth: 2,
    pointRadius: point, pointHoverRadius: 5, pointBorderColor: t.bg, pointBorderWidth: 2, tension: 0.12, spanGaps: true,
  });

  equityChart?.destroy();
  capitalChart?.destroy();
  if (!has) return;
  equityChart = new Chart($("equityChart"), {
    type: "line",
    data: { labels, datasets: [line("Total", "total", t.s1), line("Realized", "realized", t.s2)] },
    options: { ...baseOptions(t), animation: anim },
  });
  const opts = baseOptions(t);
  opts.scales.y.beginAtZero = true;
  opts.scales.y.ticks.callback = (v) => usd(v, false);
  opts.plugins.tooltip.callbacks = { label: (c) => ` Capital at risk  ${usd(c.parsed.y, false)}` };
  capitalChart = new Chart($("capitalChart"), {
    type: "line",
    data: { labels, datasets: [{ label: "Capital at risk", data: pts.map((p) => p.capital), borderColor: t.ink2, borderWidth: 1.5, stepped: true, pointRadius: 0, pointHoverRadius: 4, spanGaps: true }] },
    options: { ...opts, animation: anim },
  });
}

function renderRelations(d) {
  const r = d.relations || { recent: [] };
  const stats = [
    ["Pairs judged", r.pairs_judged],
    ["Violations priced", r.opportunities_logged],
    ["Tradeable", r.tradeable_logged],
    ["Paper-traded", r.traded],
  ];
  $("rel-stats").innerHTML = stats.map(([k], i) => `<div class="kv"><div class="k">${esc(k)}</div><div class="v num" id="rel-stat-${i}">--</div></div>`).join("");
  stats.forEach(([, v], i) => countTo($(`rel-stat-${i}`), v ?? 0, (x) => Math.round(x ?? 0).toLocaleString()));

  const el = $("rel-table");
  if (!r.recent?.length) { el.innerHTML = emptyNote(); return; }
  el.innerHTML = table(
    [["Relation"], ["Legs"], ["Edge / set", "num"], ["Qty", "num"], ["Status"], ["When", "num"]],
    r.recent.map((a) => `<tr>
      <td><span class="tag">${esc((a.kind || "").replaceAll("_", " "))}</span></td>
      <td class="tk">${a.legs.map(esc).join("<br>")}</td>
      <td class="num ${signClass(a.edge_per_set)}">${esc(usd(a.edge_per_set))}</td>
      <td class="num">${esc(a.qty)}</td>
      <td class="wrap">${a.traded ? '<span class="tag accent">paper-traded</span>' : esc(a.note || "")}</td>
      <td class="num">${esc(ago(a.logged_at))}</td></tr>`),
  );
  staggerRows(el);
}

// ---- comparisons: every judged pair, violation or not --------------------------------
// Plain words, not arrows: "B ⇒ A" read as jargon on the page.
const REL_LABEL = { a_implies_b: "If A, then B", b_implies_a: "If B, then A", mutually_exclusive: "Not both", exhaustive: "At least one" };
const STATUS_TAG = {
  violation: '<span class="tag accent">violation</span>',
  consistent: '<span class="tag good">consistent</span>',
  unpriced: '<span class="tag">unpriced</span>',
  "near miss": '<span class="tag">near miss</span>',
  unrelated: '<span class="tag muted">unrelated</span>',
};
const PAGE = 50;
const cmpState = { filter: "all", limit: PAGE };

function relationLabel(r) {
  if (r.relations.length === 2 && r.relations.includes("a_implies_b") && r.relations.includes("b_implies_a")) return "Same outcome";
  if (r.relations.length) return r.relations.map((x) => REL_LABEL[x] || x).join(" + ");
  return "none";
}
function marketCell(m) {
  return `<td class="mkt"><div class="t">${esc(m.title)}</div><div class="m">${esc(m.venue)} · ${esc(m.topic || "")} · ${esc(m.ticker)}</div></td>`;
}

function renderComparisons(d) {
  const c = (d.relations || {}).comparisons;
  const el = $("cmp-table");
  if (!c) {
    $("cmp-stats").innerHTML = "";
    el.innerHTML = emptyNote();
    $("cmp-more").hidden = true;
    return;
  }
  const n = c.counts || {};
  const related = (n.violation || 0) + (n.consistent || 0) + (n.unpriced || 0);
  const markets = Object.values(c.universe || {}).flatMap((v) => Object.values(v)).reduce((a, b) => a + b, 0);
  const topicCount = (t) => Object.values(c.universe || {}).reduce((a, v) => a + (v[t] || 0), 0);
  const stats = [
    ["Pairs compared (live)", c.live_pairs],
    ["Judged all-time", c.total_judged],
    ["Relations confirmed", related],
    ["Kalshi × Polymarket", n.cross_venue || 0],
    ["Near misses", n["near miss"] || 0],
    ["Markets in scope", markets],
  ];
  $("cmp-stats").innerHTML = stats.map(([k], i) => `<div class="kv"><div class="k">${esc(k)}</div><div class="v num" id="cmp-stat-${i}">--</div></div>`).join("");
  stats.forEach(([, v], i) => countTo($(`cmp-stat-${i}`), v ?? 0, (x) => Math.round(x ?? 0).toLocaleString()));
  $("cmp-tag").textContent = `${topicCount("cultural").toLocaleString()} cultural · ${topicCount("economic").toLocaleString()} economic · ${topicCount("geopolitical").toLocaleString()} geopolitical markets · scanned ${ago(c.generated_at)}`;

  const f = cmpState.filter;
  const match = { all: () => true, related: (r) => r.relations.length > 0, cross: (r) => r.a.venue !== r.b.venue };
  const rows = (c.rows || []).filter(match[f] || ((r) => r.status === f));
  const shownRows = rows.slice(0, cmpState.limit);
  el.innerHTML = shownRows.length
    ? table(
        [["Status"], ["Market A"], ["Relation"], ["Market B"], ["Same thing", "num"], ["Price check", "num"], ["Judged", "num"]],
        shownRows.map((r) => `<tr>
          <td>${STATUS_TAG[r.status] || esc(r.status)}</td>
          ${marketCell(r.a)}
          <td class="rel">${esc(relationLabel(r))}<span class="p">${r.relations.length ? "" : "closest: " + esc(REL_LABEL[r.best.relation] || r.best.relation) + " "}${esc(pct(r.best.prob, 0))}</span></td>
          ${marketCell(r.b)}
          <td class="num">${esc(pct(r.gate, 0))}</td>
          <td class="num ${r.status === "violation" ? "pos" : ""}" title="best set's guaranteed profit at current prices; negative = how far from a violation">${r.edge === null || r.edge === undefined ? "—" : esc(usd(r.edge)) + "/set"}${r.status === "unpriced" && r.note ? `<span class="why">${esc(r.note)}</span>` : ""}</td>
          <td class="num">${esc(ago(r.asked_at))}</td></tr>`),
      )
    : emptyNote();
  const more = $("cmp-more");
  more.hidden = rows.length <= cmpState.limit;
  more.textContent = `Show more (${(rows.length - cmpState.limit).toLocaleString()} left)`;
  staggerRows(el);
}
document.querySelectorAll("[data-filter]").forEach((b) =>
  b.addEventListener("click", () => {
    cmpState.filter = b.dataset.filter;
    cmpState.limit = PAGE;
    document.querySelectorAll("[data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    if (lastData) renderComparisons(lastData);
  }),
);
$("cmp-more").addEventListener("click", () => { cmpState.limit += PAGE; if (lastData) renderComparisons(lastData); });

function renderPositions(d) {
  const b = book(d);
  const open = b.positions.filter((x) => x.status === "open");
  const pos = $("positions-table");
  pos.innerHTML = open.length
    ? table(
        [["Ticker"], ["Side"], ["Qty", "num"], ["Avg cost", "num"], ["Mark", "num"], ["Unrealized", "num"], ["Opened", "num"]],
        open.map((x) => `<tr><td class="tk">${esc(x.ticker)}</td><td class="tk">${esc((x.side || "").toUpperCase())}</td>
          <td class="num">${esc(x.qty_open)}</td><td class="num">${esc(pct(x.avg_cost))}</td><td class="num">${esc(pct(x.mark))}</td>
          <td class="num ${signClass(x.unrealized_pnl)}">${esc(usd(x.unrealized_pnl))}</td><td class="num">${esc(ago(x.opened_at))}</td></tr>`),
      )
    : emptyNote();
  staggerRows(pos);

  const fills = (d.paper_trading.recent_fills || []).filter((f) => f.strategy === STRATEGY).slice(0, 30);
  const tr = $("paper-table");
  tr.innerHTML = fills.length
    ? table(
        [["Action"], ["Ticker"], ["Side"], ["Qty", "num"], ["Price", "num"], ["Cost / PnL", "num"], ["Reason"], ["When", "num"]],
        fills.map((f) => {
          const sell = f.action === "sell";
          const money = sell ? `<span class="${signClass(f.pnl_usd)}">${esc(usd(f.pnl_usd))}</span>` : esc(usd(f.cost_usd, false));
          return `<tr><td><span class="tag ${sell ? "solid" : ""}">${sell ? "exit" : "enter"}</span></td>
            <td class="tk">${esc(f.ticker)}</td><td class="tk">${esc((f.side || "").toUpperCase())}</td>
            <td class="num">${esc(f.qty ?? "")}</td><td class="num">${esc(pct(f.price))}</td><td class="num">${money}</td>
            <td class="wrap">${esc(f.reason || "")}</td><td class="num">${esc(ago(f.ts))}</td></tr>`;
        }),
      )
    : emptyNote();
  staggerRows(tr);
}

function renderStatus(d) {
  const h = d.loop_health;
  const live = h.last_line_at && !h.stale;
  $("loop-status").classList.toggle("stale", !live);
  $("loop-status-text").textContent = !h.last_line_at ? "no loop" : live ? "live" : "stale";
}

// ---- refresh loop -------------------------------------------------------------------
async function refresh() {
  try {
    const res = await fetch("/api/summary", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    const first = !lastData;
    lastData = d;
    renderHero(d);
    renderBook(d);
    renderRelations(d);
    renderComparisons(d);
    renderPositions(d);
    renderStatus(d);
    renderCharts(d, { animateLines: first });
    $("footer-updated").textContent = `DATA ${new Date(d.generated_at).toISOString().slice(0, 19).replace("T", " ")} UTC · REFRESH 60S`;
  } catch (err) {
    $("loop-status-text").textContent = "offline";
    $("loop-status").classList.add("stale");
    $("footer-updated").textContent = `REFRESH FAILED (${err.message}) · RETRYING`;
  } finally {
    revealOnce();
  }
}

applyTheme(currentTheme(), false);
refresh();
setInterval(refresh, REFRESH_MS);
