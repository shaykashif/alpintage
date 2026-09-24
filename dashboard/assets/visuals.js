// Decorative + display primitives for the dashboard. Plain ES module, no
// framework: the dashboard is a single Flask-served page with no build step.
//
// - DotField: pointillist halftone dots on a slowly drifting field.
//   Theme-aware: reads --dot from CSS.
// - FiberHelix: an original canvas take on the 21st.dev "Helix Chrono
//   Matrix" look (thin 3D fibers, traveling nodes, morphing topologies;
//   the caller decides when to morph -- see app.js).
// - Lcd: the seven-segment digit from 21st.dev "Segmented LCD Number Ticker"
//   (@shadcnspace), extended with a decimal point; sign is rendered by the
//   caller so it can carry +/- text, not color alone.

const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function toRgb(color) {
  const c = document.createElement("canvas");
  c.width = c.height = 1;
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#6B7280";
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
  return { r, g, b };
}

/** Run `frame(t)` on rAF only while the element is on screen and the tab is visible. */
function visibleLoop(el, frame) {
  let raf = 0, onScreen = true, stopped = false;
  const loop = (t) => { if (stopped) return; frame(t); raf = requestAnimationFrame(loop); };
  const start = () => { if (!raf && onScreen && !document.hidden) raf = requestAnimationFrame(loop); };
  const stop = () => { cancelAnimationFrame(raf); raf = 0; };
  new IntersectionObserver(([e]) => { onScreen = e.isIntersecting; onScreen ? start() : stop(); }).observe(el);
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  start();
  return { stop: () => { stopped = true; stop(); } };
}

/** Size a canvas's backing store to its CSS box. The CSS box MUST be set by
 *  a stylesheet (width/height 100%): a canvas left to its intrinsic size
 *  grows every time its backing store does -- a ResizeObserver feedback
 *  loop that froze the page in an earlier version (glyph matrix). */
function fitCanvas(canvas, ctx) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
  if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [w, h];
}

// ---- Pointillist dot field --------------------------------------------------
// A halftone grid: every dot's size follows a slowly drifting interference
// field, so the panel reads as a quietly moving pointillist texture.
export class DotField {
  constructor(canvas, { spacing = 9, maxSize = 2.2 } = {}) {
    Object.assign(this, { canvas, spacing, maxSize });
    this.ctx = canvas.getContext("2d");
    this.time = 0;
    this.lastT = null;
    this.refreshColor();
    this.resize();
    new ResizeObserver(() => { this.resize(); this.draw(); }).observe(canvas);
    this.draw();
    if (!reduceMotion()) visibleLoop(canvas, (t) => this.frame(t));
  }
  refreshColor() { this.color = cssVar("--dot") || "#171410"; this.draw?.(); }
  resize() {
    [this.w, this.h] = fitCanvas(this.canvas, this.ctx);
    this.cols = Math.ceil(this.w / this.spacing) + 1;
    this.rows = Math.ceil(this.h / this.spacing) + 1;
  }
  field(x, y) {
    // Two drifting interference waves -> smooth 0..1 halftone density.
    const t = this.time * 0.25;
    const a = Math.sin(x * 0.012 + t) * Math.cos(y * 0.018 - t * 0.7);
    const b = Math.sin((x + y) * 0.007 - t * 0.5 + Math.sin(y * 0.01 + t) * 1.4);
    return Math.max(0, Math.min(1, 0.5 + 0.32 * a + 0.28 * b));
  }
  frame(t) {
    if (this.lastT === null) this.lastT = t;
    this.time += Math.min(0.05, (t - this.lastT) / 1000);
    this.lastT = t;
    this.draw();
  }
  draw() {
    if (!this.cols) return;
    const { ctx, spacing: s, maxSize } = this;
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.fillStyle = this.color;
    for (let r = 0; r < this.rows; r++) {
      for (let c = 0; c < this.cols; c++) {
        const x = c * s, y = r * s;
        // Lighter behind the copy on the left so the text stays readable.
        const shade = 0.35 + 0.65 * Math.min(1, x / (this.w * 0.62));
        const size = maxSize * (0.18 + 0.82 * this.field(x, y)) * shade;
        if (size >= 0.35) ctx.fillRect(x - size / 2, y - size / 2, size, size);
      }
    }
  }
}

// ---- Fiber helix ------------------------------------------------------------
// Each topology maps (strand s of S, position u in [0,1], time) to a 3D point.
const TOPOLOGIES = {
  helix(s, S, u, t) {
    const off = (s % 2) * Math.PI + (Math.floor(s / 2) / (S / 2)) * 0.9;
    const th = u * Math.PI * 2 * 1.6 + off + t * 0.35;
    const r = 0.52 + 0.48 * Math.sin(Math.PI * u);
    return [r * Math.cos(th), (u - 0.5) * 2.1, r * Math.sin(th)];
  },
  strata(s, S, u, t) {
    const lv = s / (S - 1);
    const th = u * Math.PI * 2 + t * 0.25 + s * 0.31;
    const r = 0.25 + 0.8 * Math.sin(Math.PI * (s + 0.5) / S) + 0.05 * Math.sin(3 * th + t * 1.3);
    return [r * Math.cos(th), (lv - 0.5) * 1.9, r * Math.sin(th)];
  },
  ribbon(s, S, u, t) {
    const v = (s / (S - 1) - 0.5) * 0.9;
    const ph = u * Math.PI * 2 + t * 0.22;
    const R = 0.78 + v * Math.cos(ph / 2);
    return [R * Math.cos(ph), v * Math.sin(ph / 2) * 1.7, R * Math.sin(ph)];
  },
};
export const FIBER_MODES = Object.keys(TOPOLOGIES);

export class FiberHelix {
  constructor(canvas, { strands = 18, samples = 96, nodesPerStrand = 2 } = {}) {
    Object.assign(this, { canvas, strands, samples, nodesPerStrand });
    this.ctx = canvas.getContext("2d");
    this.from = "helix";
    this.to = "helix";
    this.morph = 1; // 0 -> `from`, 1 -> `to`; tweened by the caller (anime.js)
    this.time = 0;
    this.tilt = { x: 0.38, y: 0, tx: 0.38, ty: 0 };
    this.lastT = null;
    this.refreshColors();
    this.resize();
    new ResizeObserver(() => { this.resize(); this.draw(); }).observe(canvas);
    canvas.addEventListener("pointermove", (e) => {
      const b = canvas.getBoundingClientRect();
      this.tilt.ty = ((e.clientX - b.left) / b.width - 0.5) * 0.9;
      this.tilt.tx = 0.38 + ((e.clientY - b.top) / b.height - 0.5) * 0.5;
    });
    canvas.addEventListener("pointerleave", () => { this.tilt.tx = 0.38; this.tilt.ty = 0; });
    this.draw();
    if (!reduceMotion()) visibleLoop(canvas, (t) => this.frame(t));
  }
  refreshColors() {
    this.fiber = toRgb(cssVar("--fiber") || "#171410");
    this.node = cssVar("--accent") || "#2200FF";
    this.draw?.();
  }
  setMode(mode) {
    if (!TOPOLOGIES[mode] || mode === this.to) return false;
    this.from = this.to;
    this.to = mode;
    this.morph = 0;
    return true;
  }
  resize() {
    [this.w, this.h] = fitCanvas(this.canvas, this.ctx);
  }
  point(s, u) {
    const a = TOPOLOGIES[this.from](s, this.strands, u, this.time);
    const b = TOPOLOGIES[this.to](s, this.strands, u, this.time);
    const k = this.morph * this.morph * (3 - 2 * this.morph); // smoothstep between shapes
    let x = a[0] + (b[0] - a[0]) * k, y = a[1] + (b[1] - a[1]) * k, z = a[2] + (b[2] - a[2]) * k;
    const yaw = this.time * 0.18 + this.tilt.y;
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    [x, z] = [x * cy - z * sy, x * sy + z * cy];
    const cx = Math.cos(this.tilt.x), sx = Math.sin(this.tilt.x);
    [y, z] = [y * cx - z * sx, y * sx + z * cx];
    const scale = Math.min(this.w, this.h) * 0.36;
    const persp = 3.2 / (3.2 + z);
    return [this.w / 2 + x * scale * persp, this.h / 2 + 12 + y * scale * persp, z];
  }
  frame(t) {
    if (this.lastT === null) this.lastT = t;
    const dt = Math.min(0.05, (t - this.lastT) / 1000);
    this.lastT = t;
    this.tilt.x += (this.tilt.tx - this.tilt.x) * 0.06;
    this.tilt.y += (this.tilt.ty - this.tilt.y) * 0.06;
    this.time += dt;
    this.draw();
  }
  draw() {
    if (!this.w) return;
    const { ctx, strands: S, samples: N } = this;
    const { r, g, b } = this.fiber;
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.lineWidth = 0.8;
    for (let s = 0; s < S; s++) {
      let depth = 0;
      ctx.beginPath();
      for (let i = 0; i <= N; i++) {
        const [px, py, pz] = this.point(s, i / N);
        depth += pz;
        i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
      }
      const near = 0.5 - depth / (N + 1) / 2.4; // nearer fibers read darker
      ctx.strokeStyle = `rgba(${r},${g},${b},${0.12 + 0.42 * Math.max(0, Math.min(1, near))})`;
      ctx.stroke();
    }
    ctx.fillStyle = this.node;
    for (let s = 0; s < S; s++) {
      for (let k = 0; k < this.nodesPerStrand; k++) {
        const u = (this.time * 0.09 + s * 0.137 + k / this.nodesPerStrand) % 1;
        const [px, py] = this.point(s, u);
        ctx.fillRect(px - 1.5, py - 1.5, 3, 3);
      }
    }
  }
}

// ---- Seven-segment LCD --------------------------------------------------------
const SEGMENT_MAP = [63, 6, 91, 79, 102, 109, 125, 7, 127, 111];
const SEGMENT_PATHS = ["M2.5 1h7", "M11 2.5v6.5", "M11 12v6.5", "M2.5 20h7", "M1 12v6.5", "M1 2.5v6.5", "M2.5 10.5h7"];
const SVG_NS = "http://www.w3.org/2000/svg";

export class Lcd {
  /** Renders |value| with `decimals` places as seven-segment digits. */
  constructor(el, { decimals = 2, minIntDigits = 1 } = {}) {
    Object.assign(this, { el, decimals, minIntDigits });
    this.key = "";
  }
  digitSvg() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 12 21");
    svg.setAttribute("class", "digit");
    for (const d of SEGMENT_PATHS) {
      const p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    }
    return svg;
  }
  render(value) {
    const text = Math.abs(value).toFixed(this.decimals).padStart(this.minIntDigits + (this.decimals ? this.decimals + 1 : 0), "0");
    const shape = text.replace(/\d/g, "d");
    if (shape !== this.key) { // rebuild slots only when the digit layout changes
      this.el.replaceChildren(...[...text].map((ch) => (ch === "." ? Object.assign(document.createElement("i"), { className: "dp" }) : this.digitSvg())));
      this.key = shape;
    }
    [...text].forEach((ch, i) => {
      if (ch === ".") return;
      const bits = SEGMENT_MAP[+ch];
      [...this.el.children[i].children].forEach((p, s) => p.setAttribute("class", (bits >> s) & 1 ? "on" : "off"));
    });
    this.el.setAttribute("aria-label", text);
  }
}
