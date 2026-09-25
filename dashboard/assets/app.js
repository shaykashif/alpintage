// Pternas dashboard: the cultural & economic relationship-arbitrage book.
// Renders /api/summary (read-only; no live API calls server-side) and
// refreshes every 60s; the comparisons table's price checks also update
// every few seconds from /api/live (the relation watcher's latest pass). Motion via anime.js v4; every animation is skipped
// under prefers-reduced-motion.
import { animate, stagger } from "https://cdn.jsdelivr.net/npm/animejs@4.5.0/+esm";
import { DotField, FiberHelix, Lcd } from "/assets/visuals.js?v=6";

const REFRESH_MS = 60_000;
const LIVE_MS = 3_000;
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

// ---- first-load draw-in: every box's rules are traced as if drawn by hand,
// then its contents fade in. Every [data-reveal] group draws once, all on
// first load (top to bottom, lightly staggered), not as each scrolls in. Strokes sit exactly on the real rules (1px gaps
// between cells, outer borders, section underlines), which replace them
// when done. Web Animations API, so it doesn't depend on anime.js loading.
const DRAW_MS = 750, DRAW_STAGGER = 45, FADE_MS = 450;
function drawPaths(group) {
  const g = group.getBoundingClientRect();
  const paths = [];
  const rect = (el, grow) => {
    const r = el.getBoundingClientRect();
    const x = r.left - g.left - grow, y = r.top - g.top - grow, w = r.width + 2 * grow, h = r.height + 2 * grow;
    paths.push({ d: `M${x} ${y}H${x + w}V${y + h}H${x}Z` });
  };
  if (group.classList.contains("section-head")) {
    const y = g.height - 0.5;
    paths.push({ d: `M0 ${y}H${g.width}`, ink: true });
    return paths;
  }
  rect(group, -0.5); // outer border, centered on its 1px
  if (group.classList.contains("hero")) {
    const v = group.querySelector(".hero-visual");
    const cs = v && getComputedStyle(v);
    if (cs && parseFloat(cs.borderLeftWidth)) { const x = v.getBoundingClientRect().left - g.left + 0.5; paths.push({ d: `M${x} 0V${g.height}` }); }
    else if (cs && parseFloat(cs.borderTopWidth)) { const y = v.getBoundingClientRect().top - g.top + 0.5; paths.push({ d: `M0 ${y}H${g.width}` }); }
    return paths;
  }
  // Grid cells: their 1px gaps are the inner rules, so trace each cell grown half a pixel.
  group.querySelectorAll(":scope > *, .stats-row > *").forEach((el) => rect(el, 0.5));
  return paths;
}
function drawGroup(group, delay = 0) {
  const g = group.getBoundingClientRect();
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("draw-svg");
  svg.setAttribute("aria-hidden", "true");
  Object.assign(svg.style, { left: `${g.left + scrollX}px`, top: `${g.top + scrollY}px`, width: `${g.width}px`, height: `${g.height}px` });
  const anims = [];
  drawPaths(group).forEach((p, i) => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", p.d);
    path.setAttribute("pathLength", "1");
    path.setAttribute("stroke-dasharray", "1");
    path.setAttribute("stroke-dashoffset", "1");
    if (p.ink) path.classList.add("ink");
    svg.appendChild(path);
    anims.push(path.animate({ strokeDashoffset: [1, 0] }, { duration: DRAW_MS, delay: delay + Math.min(i, 14) * DRAW_STAGGER, easing: "cubic-bezier(.65,0,.35,1)", fill: "forwards" }));
  });
  document.body.appendChild(svg);
  const lineMs = DRAW_MS + Math.min(anims.length - 1, 14) * DRAW_STAGGER;
  const content = group.querySelectorAll(".section-head > *, .hero > *, .cell > *, .stats-row > * > *");
  const targets = group.matches(".section-head, .hero") ? group.children : content;
  const fades = [...targets].map((el, i) => el.animate({ opacity: [0, 1] }, { duration: FADE_MS, delay: delay + lineMs * 0.55 + Math.min(i, 10) * 30, fill: "forwards" }));
  Promise.all([...anims, ...fades].map((a) => a.finished)).then(() => {
    group.classList.add("drawn"); // real rules and contents take over, at the same pixels
    svg.remove();
    fades.forEach((a) => a.cancel());
  });
}
const GROUP_STAGGER = 70;
function revealOnce() {
  if (window.__revealed) return;
  window.__revealed = true; // tells index.html's fallback not to un-hide everything
  const groups = document.querySelectorAll("[data-reveal]");
  if (reduce) { groups.forEach((g) => g.classList.add("drawn")); return; }
  groups.forEach((g, i) => drawGroup(g, i * GROUP_STAGGER));
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
  return "light"; // default regardless of the OS setting
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
    // Everything this strategy has paid into positions (open and closed): the base for the hero's return %.
    deployed: (s.positions || []).filter((p) => p.strategy === STRATEGY).reduce((a, p) => a + (p.cost || 0), 0),
    sets: s.arb_sets || [],
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
/** Nothing traded yet: every PnL figure would read $0.00, so the page leads
 *  with the comparisons instead (hero numbers + section order). The first
 *  trade switches back to the PnL layout. */
const cents = (x) => Math.round((x ?? 0) * 100);
function bookEmpty(b) {
  return !b.open && !b.closed && !cents(b.total) && !cents(b.realized) && !cents(b.unrealized) && !cents(b.capital);
}
function comparisonCounts(d) {
  const c = (d.relations || {}).comparisons || {};
  const n = c.counts || {};
  const markets = Object.values(c.universe || {}).flatMap((v) => Object.values(v)).reduce((a, x) => a + x, 0);
  return {
    live: c.live_pairs ?? 0,
    confirmed: (n.violation || 0) + (n.consistent || 0) + (n.unpriced || 0),
    cross: n.cross_venue || 0,
    markets,
  };
}

let heroMode = null;
function setHeroMode(mode) {
  if (mode === heroMode) return;
  heroMode = mode;
  const labels = mode === "comparisons"
    ? ["PAIRS COMPARED", "Relations confirmed", "Kalshi × Polymarket", "Markets in scope"]
    : ["TOTAL PNL", "Realized", "Unrealized", "Capital at risk"];
  $("lcd-unit").textContent = labels[0];
  labels.slice(1).forEach((t, i) => ($(`h-k${i}`).textContent = t));
  $("pnl-sign").hidden = mode === "comparisons";
  $("pnl-pct").hidden = mode === "comparisons";
  $("pnl-lcd").setAttribute("aria-label", mode === "comparisons" ? "market pairs compared" : "total PnL");
  lcd.decimals = mode === "comparisons" ? 0 : 2;
  ["lcd", "h-realized", "h-unrealized", "h-capital"].forEach((k) => shown.delete(k)); // don't tween across units
  // Section order: comparisons first while there's no book to show.
  const main = $("top"), cmp = $("comparisons");
  if (mode === "comparisons") main.insertBefore(cmp, $("book")); // second, after Relationship arbitrage
  else main.insertBefore(cmp, $("positions"));
  main.querySelectorAll(":scope > .section .section-index").forEach((el, i) => (el.textContent = String(i + 1).padStart(2, "0")));
}

function renderHero(d) {
  const b = book(d), rel = d.relations || {};
  if (bookEmpty(b)) {
    setHeroMode("comparisons");
    const c = comparisonCounts(d);
    const from = shown.get("lcd") ?? 0;
    shown.set("lcd", c.live);
    if (reduce) lcd.render(c.live);
    else { const o = { v: from }; animate(o, { v: c.live, duration: 1400, ease: "outExpo", onUpdate: () => lcd.render(Math.round(o.v)) }); }
    const int = (x) => Math.round(x ?? 0).toLocaleString();
    countTo($("h-realized"), c.confirmed, int);
    countTo($("h-unrealized"), c.cross, int);
    countTo($("h-capital"), c.markets, int);
    ["h-realized", "h-unrealized"].forEach((id) => ($(id).className = "v"));
  } else {
    setHeroMode("pnl");
    renderPnlHero(b);
  }
  $("hero-foot").innerHTML =
    `<span><b>${(rel.pairs_judged ?? 0).toLocaleString()}</b> market pairs judged</span>` +
    `<span><b>${(rel.opportunities_logged ?? 0).toLocaleString()}</b> violations priced</span>` +
    `<span><b>${rel.traded ?? 0}</b> paper-traded</span>` +
    `<span><b>${b.open}</b> open positions</span>`;
}

function renderPnlHero(b) {
  const sign = $("pnl-sign");
  sign.textContent = b.total > 0 ? "+" : b.total < 0 ? MINUS : "±";
  sign.className = "lcd-sign num " + signClass(b.total);
  const from = shown.get("lcd") ?? 0;
  shown.set("lcd", b.total);
  if (reduce) lcd.render(b.total);
  else { const o = { v: from }; animate(o, { v: b.total, duration: 1400, ease: "outExpo", onUpdate: () => lcd.render(o.v) }); }

  // Return on capital deployed: total PnL over everything paid into positions.
  const ret = b.deployed > 0 ? b.total / b.deployed : null;
  const pctEl = $("pnl-pct");
  pctEl.hidden = ret === null;
  if (ret !== null) {
    pctEl.textContent = (ret > 0 ? "+" : ret < 0 ? MINUS : "±") + Math.abs(ret * 100).toFixed(2) + "%";
    pctEl.className = "lcd-pct num " + signClass(ret);
    pctEl.title = `Return on ${usd(b.deployed, false)} deployed`;
  }

  countTo($("h-realized"), b.realized, usd);
  countTo($("h-unrealized"), b.unrealized, usd);
  countTo($("h-capital"), b.capital, (x) => usd(x, false));
  $("h-realized").className = "v " + signClass(b.realized);
  $("h-unrealized").className = "v " + signClass(b.unrealized);
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

// Live overlay: pair id ("A|B") -> the watcher's latest {status, edge, note}.
// Only confirmed relations are watched; other rows keep their scan values.
let live = { running: false, byPair: new Map(), at: null };
const pairId = (r) => `${r.a.ticker}|${r.b.ticker}`;
function current(r) {
  const l = r.relations.length && live.running ? live.byPair.get(pairId(r)) : null;
  return l ? { ...r, status: l.status, edge: l.edge, note: l.note, live: true } : r;
}
/** Green for a violation (profit), red for a negative edge (how far from one). */
const priceClass = (r) => (r.status === "violation" ? "pos" : typeof r.edge === "number" && r.edge < 0 ? "neg" : "");
function priceCell(r) {
  const val = r.edge === null || r.edge === undefined ? "—" : esc(usd(r.edge)) + "/set";
  const why = r.status === "unpriced" && r.note ? `<span class="why">${esc(r.note)}</span>` : "";
  return `${val}${why}`;
}
function liveTag() {
  return live.running ? ` · <span class="live-dot" aria-hidden="true"></span>prices live, checked ${esc(ago(live.at))}` : "";
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
  $("cmp-tag").innerHTML = esc(`${topicCount("cultural").toLocaleString()} cultural · ${topicCount("economic").toLocaleString()} economic · ${topicCount("geopolitical").toLocaleString()} geopolitical markets · scanned ${ago(c.generated_at)}`) + liveTag();

  const f = cmpState.filter;
  const match = { all: () => true, related: (r) => r.relations.length > 0, cross: (r) => r.a.venue !== r.b.venue };
  const rows = (c.rows || []).map(current).filter(match[f] || ((r) => r.status === f));
  const shownRows = rows.slice(0, cmpState.limit);
  el.innerHTML = shownRows.length
    ? table(
        [["Status"], ["Market A"], ["Relation"], ["Market B"], ["Same thing", "num"], ["Price check", "num"], ["Judged", "num"]],
        shownRows.map((r) => `<tr data-pair="${esc(pairId(r))}">
          <td class="st">${STATUS_TAG[r.status] || esc(r.status)}</td>
          ${marketCell(r.a)}
          <td class="rel">${esc(relationLabel(r))}<span class="p">${r.relations.length ? "" : "closest: " + esc(REL_LABEL[r.best.relation] || r.best.relation) + " "}${esc(pct(r.best.prob, 0))}</span></td>
          ${marketCell(r.b)}
          <td class="num">${esc(pct(r.gate, 0))}</td>
          <td class="num pc ${priceClass(r)}" title="best set's guaranteed profit at current prices; negative = how far from a violation">${priceCell(r)}</td>
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
  // Hedged sets first (one row per set, its legs beneath), then any leg
  // valued on its own. A set's PnL is marked as a whole -- the larger of
  // its guaranteed payout and its sale value -- so legs show only prices.
  const setOf = new Map(b.sets.map((x) => [x.arb_group, x]));
  const inSet = (x) => x.arb_group && setOf.has(x.arb_group);
  const legRow = (x, cls = "") => `<tr class="${cls}"><td class="tk">${esc(x.ticker)}</td><td class="tk">${esc((x.side || "").toUpperCase())}</td>
    <td class="num">${esc(x.qty_open)}</td><td class="num">${esc(pct(x.avg_cost))}</td><td class="num">${esc(pct(x.mark))}</td>
    <td class="num ${cls ? "" : signClass(x.unrealized_pnl)}">${cls ? "" : esc(usd(x.unrealized_pnl))}</td><td class="num">${cls ? "" : esc(ago(x.opened_at))}</td></tr>`;
  const setRows = b.sets
    .filter((x) => open.some((p) => p.arb_group === x.arb_group))
    .map((x) => `<tr class="set-row"><td class="rel" colspan="5">${esc(REL_LABEL[x.relation] || x.relation || "Hedged set")} &middot; ${x.tickers.length} legs
        <span class="p">Pays at least ${esc(usd(x.guaranteed_payout, false))} at resolution &middot; ${esc(usd(x.at_risk, false))} at risk if the relation is wrong</span></td>
        <td class="num ${signClass(x.unrealized_pnl)}">${esc(usd(x.unrealized_pnl))}</td><td class="num">${esc(ago(x.opened_at))}</td></tr>`
      + open.filter((p) => p.arb_group === x.arb_group).map((p) => legRow(p, "leg-row")).join(""));
  const rows = [...setRows, ...open.filter((x) => !inSet(x)).map((x) => legRow(x))];
  pos.innerHTML = rows.length
    ? table([["Ticker"], ["Side"], ["Qty", "num"], ["Avg cost", "num"], ["Mark", "num"], ["Unrealized", "num"], ["Opened", "num"]], rows)
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

// ---- live prices: patch the visible rows in place (no re-render, so row
// animations don't replay every 3 s) --------------------------------------------------
function applyLive() {
  if (!lastData) return;
  const rows = ((lastData.relations || {}).comparisons || {}).rows || [];
  const byId = new Map(rows.map((r) => [pairId(r), r]));
  document.querySelectorAll("#cmp-table tr[data-pair]").forEach((tr) => {
    const base = byId.get(tr.dataset.pair);
    if (!base) return;
    const r = current(base);
    const pc = tr.querySelector(".pc");
    const html = priceCell(r);
    if (pc.innerHTML !== html) {
      pc.innerHTML = html;
      pc.classList.toggle("pos", priceClass(r) === "pos");
      pc.classList.toggle("neg", priceClass(r) === "neg");
      if (!reduce) animate(pc, { opacity: [0.35, 1], duration: 500 });
    }
    tr.querySelector(".st").innerHTML = STATUS_TAG[r.status] || esc(r.status);
  });
  const tag = $("cmp-tag");
  tag.innerHTML = tag.innerHTML.replace(/ · <span class="live-dot".*$/, "") + liveTag();
}

async function refreshLive() {
  if (document.hidden) return; // no polling from a background tab
  try {
    const res = await fetch("/api/live", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    live = { running: !!d.running, byPair: new Map((d.rows || []).map((r) => [r.id, r])), at: d.generated_at };
  } catch {
    live = { ...live, running: false };
  }
  applyLive();
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
  } catch (err) {
    $("loop-status-text").textContent = "offline";
    $("loop-status").classList.add("stale");
  } finally {
    revealOnce();
  }
}

applyTheme(currentTheme(), false);
refresh().then(refreshLive);
setInterval(refresh, REFRESH_MS);
setInterval(refreshLive, LIVE_MS);
