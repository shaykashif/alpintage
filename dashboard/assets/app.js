// Pternas dashboard: renders /api/summary (read-only, no live API
// calls server-side) and refreshes every 60s. Motion via anime.js v4;
// every animation is skipped under prefers-reduced-motion.
import { animate, stagger } from "https://cdn.jsdelivr.net/npm/animejs@4.5.0/+esm";
import { DotField, FiberHelix, Lcd } from "/assets/visuals.js?v=2";

const REFRESH_MS = 60_000;
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
const emptyNote = (text) => `<div class="empty">${esc(text)}</div>`;
const table = (head, rows) =>
  `<table><thead><tr>${head.map(([h, cls]) => `<th class="${cls || ""}">${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;

// ---- motion helpers -------------------------------------------------------------
const shown = new Map(); // element id -> last value shown, so refreshes tween from it

/** Count a number up (or down) to `value` in `el`, formatted by `fmt`. */
function countTo(el, value, fmt, { duration = 900 } = {}) {
  if (!el) return;
  const key = el.id || el.dataset.key;
  const from = shown.get(key);
  shown.set(key, value);
  if (value === null || value === undefined || reduce) { el.textContent = fmt(value); return; }
  const firstShow = from === undefined || from === null;
  const o = { v: firstShow ? 0 : from };
  animate(o, { v: value, duration: firstShow ? duration + 300 : duration, ease: "outExpo", onUpdate: () => (el.textContent = fmt(o.v)) });
}

function revealOnce() {
  if (window.__revealed) return;
  window.__revealed = true;
  if (reduce) return;
  animate("[data-reveal]", {
    opacity: [0, 1], translateY: [14, 0], duration: 800, ease: "outExpo", delay: stagger(70),
  });
}

const seenRows = new WeakSet();
function staggerRows(container) {
  if (reduce || !container) return;
  const rows = [...container.querySelectorAll("tbody tr")].filter((r) => !seenRows.has(r));
  rows.forEach((r) => seenRows.add(r));
  if (rows.length) animate(rows, { opacity: [0, 1], translateX: [-6, 0], duration: 500, ease: "outQuad", delay: stagger(22) });
}

// ---- theme ------------------------------------------------------------------------
let dots, helix;
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
window.pternas = { dots, helix }; // handy for poking at the visuals from devtools
lcd.render(0);

document.querySelectorAll(".chip[data-mode]").forEach((chip) =>
  chip.addEventListener("click", () => {
    if (!helix.setMode(chip.dataset.mode)) return;
    document.querySelectorAll(".chip[data-mode]").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    if (reduce) { helix.morph = 1; helix.draw(); return; }
    animate(helix, { morph: [0, 1], duration: 1400, ease: "inOutQuart", onUpdate: () => helix.frozen && helix.draw() });
  }),
);
$("freeze").addEventListener("click", (e) => {
  helix.frozen = !helix.frozen;
  e.currentTarget.setAttribute("aria-pressed", String(helix.frozen));
  e.currentTarget.textContent = helix.frozen ? "Resume" : "Freeze";
});

setInterval(() => { $("clock").textContent = new Date().toISOString().slice(11, 19) + " UTC"; }, 1000);

// ---- renderers --------------------------------------------------------------------
let lastData = null;

function renderHero(d) {
  const p = d.paper_trading, s = p.pnl_summary || {};
  const total = s.total_pnl ?? 0;
  const sign = $("pnl-sign");
  sign.textContent = total > 0 ? "+" : total < 0 ? MINUS : "±";
  sign.className = "lcd-sign num " + signClass(total);
  const key = "lcd";
  const from = shown.get(key) ?? 0;
  shown.set(key, total);
  if (reduce) lcd.render(total);
  else { const o = { v: from }; animate(o, { v: total, duration: 1400, ease: "outExpo", onUpdate: () => lcd.render(o.v) }); }

  countTo($("h-realized"), s.realized_pnl ?? null, usd);
  countTo($("h-unrealized"), s.unrealized_pnl ?? null, usd);
  countTo($("h-capital"), s.open_cost ?? null, (x) => usd(x, false));
  $("h-realized").className = "v " + signClass(s.realized_pnl);
  $("h-unrealized").className = "v " + signClass(s.unrealized_pnl);

  const rel = d.relations || {};
  $("hero-foot").innerHTML =
    `<span><b>${s.open_count ?? 0}</b> open positions</span>` +
    `<span><b>${rel.pairs_judged ?? 0}</b> market pairs judged</span>` +
    `<span><b>${p.total_fills}</b> paper fills</span>` +
    `<span>scored ${esc(ago(s.generated_at))}</span>`;
  $("visual-caption").textContent = `relation graph · ${rel.pairs_judged ?? 0} pairs · ${rel.traded ?? 0} traded`;
}

function sparkPath(values) {
  const pts = values.filter((v) => v !== null && v !== undefined);
  if (pts.length < 2) return "";
  const lo = Math.min(0, ...pts), hi = Math.max(0, ...pts), span = hi - lo || 1;
  const X = (i) => (i / (pts.length - 1)) * 300, Y = (v) => 31 - ((v - lo) / span) * 28;
  const zero = Y(0);
  return `<line x1="0" x2="300" y1="${zero}" y2="${zero}"></line><path d="${pts.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ")}"></path>`;
}

function renderStrategies(d) {
  const by = (d.paper_trading.pnl_summary || {}).by_strategy || {};
  const curve = d.paper_trading.equity_curve || [];
  document.querySelectorAll("[data-strategy]").forEach((cell) => {
    const k = cell.dataset.strategy, t = by[k];
    const pnlEl = cell.querySelector('[data-field="pnl"]');
    pnlEl.dataset.key = "strat-" + k;
    countTo(pnlEl, t ? t.total_pnl : null, (x) => (x === null ? "$0.00" : usd(x)));
    pnlEl.className = "big " + signClass(t?.total_pnl);
    cell.querySelector('[data-field="sub"]').textContent = t
      ? `${t.open_count} open · ${t.closed_count} closed · ${t.wins} won`
      : "no positions yet";
    const svg = cell.querySelector('[data-field="spark"]');
    svg.innerHTML = sparkPath(curve.map((r) => (r.by_strategy || {})[k] ?? null));
    const path = svg.querySelector("path");
    if (path && !reduce && !svg.dataset.drawn) {
      svg.dataset.drawn = "1";
      const len = path.getTotalLength();
      path.style.strokeDasharray = `${len}`;
      animate(path, { strokeDashoffset: [len, 0], duration: 1600, ease: "inOutQuad", delay: 400 });
    }
  });
}

function renderBook(d) {
  const p = d.paper_trading, s = p.pnl_summary || {};
  const closed = s.closed_count || 0;
  const rows = [
    ["Entries / exits", `${p.total_fills} / ${p.total_exits}`],
    ["Open positions", s.open_count ?? 0],
    ["Closed", closed],
    ["Win rate (closed)", closed ? pct((s.wins || 0) / closed, 0) : "--"],
    ["Settled", s.settled_count ?? 0],
    ["Risk-gate vetoes", p.total_vetoes],
    ["Last scoring pass", ago(s.generated_at)],
  ];
  $("book-table").innerHTML = rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join("");
}

// ---- charts (Chart.js, themed from CSS tokens) -------------------------------------
let equityChart, capitalChart, diffChart;
function chartTheme() {
  return {
    s1: cssVar("--series-1"), s2: cssVar("--series-2"), ink: cssVar("--ink"), ink2: cssVar("--ink-2"),
    ink3: cssVar("--ink-3"), grid: cssVar("--grid-line"), rule: cssVar("--rule-strong"), bg: cssVar("--bg"),
    mono: cssVar("--mono"),
  };
}
function baseOptions(t, { money = true, yTitle } = {}) {
  const tick = { color: t.ink3, font: { family: t.mono, size: 10.5 } };
  return {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: t.ink, titleColor: t.bg, bodyColor: t.bg, borderWidth: 0, cornerRadius: 0,
        titleFont: { family: t.mono, size: 11 }, bodyFont: { family: t.mono, size: 11 }, padding: 10,
        boxWidth: 8, boxHeight: 2, usePointStyle: false,
        callbacks: money ? { label: (c) => ` ${c.dataset.label}  ${usd(c.parsed.y)}` } : {},
      },
    },
    scales: {
      x: { ticks: { ...tick, maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false }, border: { color: t.rule } },
      y: {
        ticks: { ...tick, callback: (v) => (money ? usd(v) : v) }, grid: { color: t.grid }, border: { display: false },
        grace: "12%", title: yTitle ? { display: true, text: yTitle, color: t.ink3, font: { family: t.mono, size: 10.5 } } : undefined,
      },
    },
  };
}

function renderCharts(d, { animateLines = true } = {}) {
  const t = chartTheme();
  const curve = d.paper_trading.equity_curve || [];
  const labels = curve.map((r) => new Date(r.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }));
  const hasCurve = curve.length > 0;
  $("equity-empty").hidden = hasCurve;
  $("equity-empty").textContent = d.paper_trading.total_fills
    ? "the curve starts at the next scoring pass (every loop cycle)"
    : "no paper trades yet — the curve starts with the first scoring pass";
  document.querySelectorAll("#equityChart, #capitalChart").forEach((c) => (c.parentElement.hidden = !hasCurve));

  const last = curve[curve.length - 1] || {};
  $("equity-legend").innerHTML =
    `<span><i style="background:${t.s1}"></i>Total ${esc(usd(last.total_pnl ?? null))}</span>` +
    `<span><i style="background:${t.s2}"></i>Realized ${esc(usd(last.realized_pnl ?? null))}</span>`;

  const anim = reduce || !animateLines ? false : { duration: 1100, easing: "easeOutQuart" };
  const point = curve.length > 40 ? 0 : 3;

  equityChart?.destroy();
  if (hasCurve) {
    equityChart = new Chart($("equityChart"), {
      type: "line",
      data: {
        labels,
        datasets: [
          { label: "Total", data: curve.map((r) => r.total_pnl), borderColor: t.s1, backgroundColor: t.s1, borderWidth: 2, pointRadius: point, pointHoverRadius: 5, pointBorderColor: t.bg, pointBorderWidth: 2, tension: 0.12 },
          { label: "Realized", data: curve.map((r) => r.realized_pnl), borderColor: t.s2, backgroundColor: t.s2, borderWidth: 2, pointRadius: point, pointHoverRadius: 5, pointBorderColor: t.bg, pointBorderWidth: 2, tension: 0.12 },
        ],
      },
      options: { ...baseOptions(t), animation: anim },
    });
  }

  capitalChart?.destroy();
  if (hasCurve) {
    const opts = baseOptions(t);
    opts.scales.y.beginAtZero = true;
    opts.scales.y.ticks.callback = (v) => usd(v, false);
    opts.plugins.tooltip.callbacks = { label: (c) => ` Capital at risk  ${usd(c.parsed.y, false)}` };
    capitalChart = new Chart($("capitalChart"), {
      type: "line",
      data: { labels, datasets: [{ label: "Capital at risk", data: curve.map((r) => r.open_cost), borderColor: t.ink2, borderWidth: 1.5, stepped: true, pointRadius: 0, pointHoverRadius: 4 }] },
      options: { ...opts, animation: anim },
    });
  }

  const c = d.cross_venue;
  diffChart?.destroy();
  if (c.total_logged) {
    const bins = c.histogram_bins;
    const hl = bins.slice(0, -1).map((b, i) => `${(b * 100).toFixed(0)}–${(bins[i + 1] * 100).toFixed(0)}%`);
    const opts = baseOptions(t, { money: false });
    opts.interaction = { mode: "nearest", intersect: true };
    opts.plugins.tooltip.callbacks = { label: (x) => ` ${x.parsed.y} comparisons` };
    diffChart = new Chart($("diffChart"), {
      type: "bar",
      data: { labels: hl, datasets: [{ label: "comparisons", data: c.histogram_counts, backgroundColor: t.s1, borderRadius: { topLeft: 3, topRight: 3 }, borderSkipped: "bottom", barPercentage: 0.86, categoryPercentage: 0.9 }] },
      options: { ...opts, animation: anim },
    });
  }
}

function renderRelations(d) {
  const r = d.relations || { recent: [] };
  const stats = [
    ["Pairs judged by Jev", r.pairs_judged],
    ["Opportunities logged", r.opportunities_logged],
    ["Tradeable", r.tradeable_logged],
    ["Paper-traded", r.traded],
  ];
  $("rel-stats").innerHTML = stats.map(([k], i) => `<div class="kv"><div class="k">${esc(k)}</div><div class="v num" id="rel-stat-${i}">--</div></div>`).join("");
  stats.forEach(([, v], i) => countTo($(`rel-stat-${i}`), v ?? 0, (x) => Math.round(x ?? 0).toLocaleString()));

  const el = $("rel-table");
  if (!r.recent?.length) { el.innerHTML = emptyNote("no price has broken a Jev-confirmed relation yet — rare by design"); return; }
  el.innerHTML = table(
    [["Relation"], ["Legs"], ["Edge / set", "num"], ["Qty", "num"], ["Status"], ["When"]],
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

const STRATEGY_LABEL = { relation_arb: "relation", stat_arb: "stat-arb", ladder_bracket: "ladder" };
function renderPositions(d) {
  const p = d.paper_trading, s = p.pnl_summary || {};
  const open = (s.positions || []).filter((x) => x.status === "open");
  const pos = $("positions-table");
  pos.innerHTML = open.length
    ? table(
        [["Ticker"], ["Strategy"], ["Side"], ["Qty", "num"], ["Avg cost", "num"], ["Mark", "num"], ["Unrealized", "num"], ["Opened", "num"]],
        open.map((x) => `<tr>
          <td class="tk">${esc(x.ticker)}</td><td><span class="tag">${esc(STRATEGY_LABEL[x.strategy] || x.strategy)}</span></td>
          <td class="tk">${esc((x.side || "").toUpperCase())}</td><td class="num">${esc(x.qty_open)}</td>
          <td class="num">${esc(pct(x.avg_cost))}</td><td class="num">${esc(pct(x.mark))}</td>
          <td class="num ${signClass(x.unrealized_pnl)}">${esc(usd(x.unrealized_pnl))}</td><td class="num">${esc(ago(x.opened_at))}</td></tr>`),
      )
    : emptyNote("no open positions");
  staggerRows(pos);

  const tr = $("paper-table");
  tr.innerHTML = p.recent_fills?.length
    ? table(
        [["Action"], ["Ticker"], ["Strategy"], ["Side"], ["Qty", "num"], ["Price", "num"], ["Cost / PnL", "num"], ["Reason"], ["When", "num"]],
        p.recent_fills.map((f) => {
          const sell = f.action === "sell";
          const money = sell ? `<span class="${signClass(f.pnl_usd)}">${esc(usd(f.pnl_usd))}</span>` : esc(usd(f.cost_usd, false));
          return `<tr><td><span class="tag ${sell ? "solid" : ""}">${sell ? "exit" : "enter"}</span></td>
            <td class="tk">${esc(f.ticker)}</td><td><span class="tag">${esc(STRATEGY_LABEL[f.strategy] || f.strategy)}</span></td>
            <td class="tk">${esc((f.side || "").toUpperCase())}</td><td class="num">${esc(f.qty ?? "")}</td>
            <td class="num">${esc(pct(f.price))}</td><td class="num">${money}</td>
            <td class="wrap">${esc(f.reason || "")}</td><td class="num">${esc(ago(f.ts))}</td></tr>`;
        }),
      )
    : emptyNote("no paper trades yet");
  staggerRows(tr);
}

function renderSports(d) {
  const c = d.cross_venue;
  const stats = [["Comparisons", c.total_logged, (x) => Math.round(x).toLocaleString()], ["Avg divergence", c.avg_diff, (x) => pct(x)], ["Max divergence", c.max_diff, (x) => pct(x)], ["Leagues", Object.keys(c.by_league).length, (x) => Math.round(x)]];
  $("cv-stats").innerHTML = stats.map(([k], i) => `<div class="kv"><div class="k">${esc(k)}</div><div class="v num" id="cv-stat-${i}">--</div></div>`).join("");
  stats.forEach(([, v, f], i) => countTo($(`cv-stat-${i}`), v ?? null, (x) => (x === null ? "--" : f(x))));
  const el = $("cv-table");
  el.innerHTML = c.top_divergences.length
    ? table(
        [["League"], ["Game"], ["Kalshi"], ["Diff", "num"], ["Logged", "num"]],
        c.top_divergences.map((r) => `<tr><td><span class="tag">${esc((r.league || "").toUpperCase())}</span></td>
          <td class="tk">${esc(r.kalshi_event_ticker)}</td>
          <td class="tk">${Object.entries(r.kalshi_probs || {}).map(([k, v]) => `${esc(k)} ${esc(pct(v, 0))}`).join("<br>")}</td>
          <td class="num">${esc(pct(r.diff))}</td><td class="num">${esc(ago(r.logged_at))}</td></tr>`),
      )
    : emptyNote("nothing logged yet");
  staggerRows(el);
}

function renderSystem(d) {
  const h = d.loop_health;
  const status = $("loop-status");
  const live = h.last_line_at && !h.stale;
  status.classList.toggle("stale", !live);
  $("loop-status-text").textContent = !h.last_line_at ? "no loop" : live ? "live" : "stale";
  $("loop-last").textContent = h.last_line_at ? `last activity ${ago(h.last_line_at)}` : "no activity yet";
  $("loop-log").innerHTML = (h.recent_lines || [])
    .map((l) => (l.startsWith("[") ? `<span class="ts">${esc(l)}</span>` : esc(l)))
    .join("\n") || esc("run_loop.py hasn't logged anything yet");
  const log = $("loop-log");
  log.scrollTop = log.scrollHeight;

  const p = d.predictions;
  $("brier-tag").textContent = p.jev_brier === null ? `${p.total_scored} scored` : `Brier Jev ${p.jev_brier} · market ${p.market_brier}`;
  const el = $("pred-table");
  el.innerHTML = p.recent.length
    ? table(
        [["Ticker"], ["Market", "num"], ["Jev", "num"], ["Outcome"]],
        p.recent.map((r) => `<tr><td class="tk">${esc(r.ticker)}</td><td class="num">${esc(pct(r.market_prob))}</td>
          <td class="num">${esc(pct(r.jev_prob))}</td>
          <td>${r.outcome === null ? '<span class="tag">pending</span>' : r.outcome ? '<span class="tag good">yes</span>' : '<span class="tag bad">no</span>'}</td></tr>`),
      )
    : emptyNote("nothing logged yet");
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
    renderStrategies(d);
    renderBook(d);
    renderRelations(d);
    renderPositions(d);
    renderSports(d);
    renderSystem(d);
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
