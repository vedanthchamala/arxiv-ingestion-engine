// Explore page: one research web drawn as a star chart (decision 38). The reader's path is a gold
// constellation in reading order; each suggestion settles near the path paper it branches from. Zoom is
// semantic (positions scale, text and strokes do not), and labels are placed after every move so that none
// crosses an edge, a star or another label: a label with no free spot stays hidden until its star is hovered.
(() => {
  const box = document.getElementById("graph");
  const dataEl = document.getElementById("graph-data");
  if (!box || !dataEl || typeof d3 === "undefined") return;
  const data = JSON.parse(dataEl.textContent);
  const webId = data.web.id;
  const panel = document.getElementById("panel");
  const hintHTML = panel.innerHTML;
  const dwell = panel.dataset.dwell || "10";
  const tip = document.getElementById("tip");
  const ui = [document.getElementById("legend"), document.querySelector(".sky-tools")].filter(Boolean);
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const ctx = document.createElement("canvas").getContext("2d");
  const measure = (text, weight, size) => {
    ctx.font = `${weight} ${size}px "Plex Condensed", "IBM Plex Sans Condensed", sans-serif`;
    return ctx.measureText(text).width;
  };
  const shortTitle = (t) => {
    const head = t.split(/:\s|\s[—–]\s/)[0].trim();
    return head !== t && head.length >= 3 && head.length <= 34 ? head : t;
  };
  // Greedy wrap to two lines; the second line ends in an ellipsis when the title runs longer.
  const wrap = (text, weight, size, maxW) => {
    const fits = (s) => measure(s, weight, size) <= maxW;
    const clip = (s) => {
      while (s.length > 1 && !fits(`${s}…`)) s = s.slice(0, -1).trimEnd();
      return `${s}…`;
    };
    const all = [];
    let cur = "";
    for (const w of text.split(/\s+/).filter(Boolean)) {
      const next = cur ? `${cur} ${w}` : w;
      if (!cur || fits(next)) cur = next;
      else {
        all.push(cur);
        cur = w;
      }
    }
    if (cur) all.push(cur);
    const lines = all.slice(0, 2).map((l) => (fits(l) ? l : clip(l)));
    if (all.length > 2 && !lines[1].endsWith("…")) lines[1] = clip(lines[1]);
    return lines;
  };

  let nodes = data.nodes.map((n) => ({ ...n }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const path = nodes.filter((n) => n.kind === "path");
  const maxScore = d3.max(nodes, (n) => n.score || 0) || 1;
  const kindOf = (n) => (n.kind === "path" ? "path" : n.in_index ? "sugg" : "outside");

  // One edge per pair of papers: the reading trail beats "builds on", which beats "similar".
  const RANK = { trail: 3, builds_on: 2, similar: 1 };
  const pairs = new Map();
  for (const e of data.edges) {
    if (!byId.has(e.source) || !byId.has(e.target) || e.source === e.target) continue;
    const key = [e.source, e.target].sort().join("|");
    const had = pairs.get(key);
    if (!had || RANK[e.type] > RANK[had.type]) pairs.set(key, { ...e, key });
  }
  let edges = [...pairs.values()];
  let adj = new Map();
  const index = () => {
    adj = new Map(nodes.map((n) => [n.id, new Set()]));
    for (const e of edges) {
      adj.get(e.source).add(e.target);
      adj.get(e.target).add(e.source);
      const s = byId.get(e.source), t = byId.get(e.target);
      const sugg = s.kind === "suggestion" ? s : t.kind === "suggestion" ? t : null;
      e.primary = !sugg || sugg.primary === (sugg === s ? t : s).id;
    }
  };

  // The path is a fixed spine in reading order, zigzagging and wrapping like a snake after five papers.
  // Each path paper keeps a side free for its own label (`slot`): east of a lone paper, otherwise the
  // outer side of the zigzag. Its suggestions are pulled to the other side, stronger ones closer, and force
  // only resolves collisions, so clusters never drift into a tangle between papers.
  const layout = () => {
    const STEP = 300, PER_ROW = 5, ROW = 520;
    const single = path.length === 1;
    const cols = Math.min(path.length, PER_ROW), rows = Math.ceil(path.length / PER_ROW);
    path.forEach((n, i) => {
      const row = Math.floor(i / PER_ROW);
      const col = row % 2 ? PER_ROW - 1 - (i % PER_ROW) : i % PER_ROW;
      n.fx = n.x = (col - (cols - 1) / 2) * STEP;
      n.fy = n.y = (row - (rows - 1) / 2) * ROW + (single ? 0 : i % 2 ? 34 : -34);
      n.slot = single ? [1, 0] : [0, i % 2 ? 1 : -1];
    });
    const kids = d3.group(nodes.filter((n) => n.kind === "suggestion"), (n) => n.primary);
    for (const [from, list] of kids) {
      const p = byId.get(from) || { fx: 0, fy: 0, slot: [0, -1] };
      list.sort((a, b) => b.strength - a.strength);
      if (single) {
        const wedge = (80 * Math.PI) / 180;
        const ring = Math.max(170, (list.length * 110) / (2 * Math.PI - wedge));
        list.forEach((k, j) => {
          const a = wedge / 2 + ((2 * Math.PI - wedge) * (j + 0.5)) / list.length;
          const r = ring * (0.88 + 0.3 * (1 - k.strength));
          k.tx = p.fx + Math.cos(a) * r;
          k.ty = p.fy + Math.sin(a) * r;
        });
      } else {
        // Rows of three inside the paper's own column (staggered, so links to the second row pass between
        // the first, and the middle one dropped so labels have room), leaving the neighbours' label sides clear.
        const dir = -p.slot[1];
        list.forEach((k, m) => {
          const row = Math.floor(m / 3), inRow = Math.min(3, list.length - row * 3);
          k.tx = p.fx + ((m % 3) - (inRow - 1) / 2) * 108 + (row % 2 ? 54 : 0);
          k.ty = p.fy + dir * (150 + row * 150 + (m % 3 === 1 ? 72 : 0));
        });
      }
      for (const k of list) {
        k.x = k.tx;
        k.y = k.ty;
      }
    }
    d3.forceSimulation(nodes)
      .force("x", d3.forceX((d) => d.tx ?? d.x).strength((d) => (d.kind === "path" ? 0 : 0.2)))
      .force("y", d3.forceY((d) => d.ty ?? d.y).strength((d) => (d.kind === "path" ? 0 : 0.25)))
      .force("charge", d3.forceManyBody().strength((d) => (d.kind === "path" ? -260 : -110)).distanceMax(260))
      .force("collide", d3.forceCollide((d) => d.r + (d.kind === "path" ? 66 : 52)).iterations(3))
      .stop()
      .tick(400);
  };

  // -- geometry -----------------------------------------------------------------------------------
  let T = d3.zoomIdentity;
  const P = (n) => T.apply([n.x, n.y]);
  const quad = (a, c, b, u) => [
    (1 - u) * (1 - u) * a[0] + 2 * (1 - u) * u * c[0] + u * u * b[0],
    (1 - u) * (1 - u) * a[1] + 2 * (1 - u) * u * c[1] + u * u * b[1],
  ];
  // The control point of the piece of a quadratic between u and v (its blossom).
  const blossom = (a, c, b, u, v) => [
    (1 - u) * (1 - v) * a[0] + ((1 - u) * v + u * (1 - v)) * c[0] + u * v * b[0],
    (1 - u) * (1 - v) * a[1] + ((1 - u) * v + u * (1 - v)) * c[1] + u * v * b[1],
  ];
  const dist = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);
  const root = (f, lo, hi) => {
    if (f(lo) >= 0) return lo;
    if (f(hi) <= 0) return hi;
    for (let i = 0; i < 24; i++) {
      const mid = (lo + hi) / 2;
      if (f(mid) < 0) lo = mid;
      else hi = mid;
    }
    return (lo + hi) / 2;
  };
  // A gently bent curve between two stars, cut back so it starts and ends a few pixels off each rim
  // (the arrowhead's tip touches the rim it points at).
  const geometry = (e) => {
    const s = byId.get(e.source), t = byId.get(e.target);
    const a = P(s), b = P(t);
    let bend = e.type === "trail" ? 0.07 : 0.13;
    // Links from a path paper bow away from the side kept for its label.
    const anchor = e.type === "trail" ? null : s.slot ? s : t.slot ? t : null;
    if (anchor && (a[1] - b[1]) * anchor.slot[0] + (b[0] - a[0]) * anchor.slot[1] > 0) bend = -bend;
    const c = [(a[0] + b[0]) / 2 - (b[1] - a[1]) * bend, (a[1] + b[1]) / 2 + (b[0] - a[0]) * bend];
    const at = (u) => quad(a, c, b, u);
    const gap0 = s.r + (e.type === "trail" ? 6 : 4);
    const gap1 = t.r + (e.type === "builds_on" ? 2.5 : e.type === "trail" ? 6 : 4);
    const u0 = root((u) => dist(at(u), a) - gap0, 0, 0.5);
    const u1 = root((u) => gap1 - dist(at(u), b), 0.5, 1);
    if (u1 - u0 < 0.02) return { d: "", pts: [] };
    const p0 = blossom(a, c, b, u0, u0), p1 = blossom(a, c, b, u0, u1), p2 = blossom(a, c, b, u1, u1);
    const f = (p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    return { d: `M${f(p0)}Q${f(p1)} ${f(p2)}`, pts: d3.range(17).map((i) => quad(p0, p1, p2, i / 16)) };
  };

  // -- drawing ------------------------------------------------------------------------------------
  const svg = d3.select(box).append("svg").attr("class", "sky-svg");
  const defs = svg.append("defs");
  const glow = (id, color, alpha) => {
    const g = defs.append("radialGradient").attr("id", id);
    g.append("stop").attr("offset", "0%").attr("stop-color", color).attr("stop-opacity", alpha);
    g.append("stop").attr("offset", "38%").attr("stop-color", color).attr("stop-opacity", alpha * 0.3);
    g.append("stop").attr("offset", "100%").attr("stop-color", color).attr("stop-opacity", 0);
  };
  const tone = getComputedStyle(box.closest(".explore-page"));
  const color = (name) => tone.getPropertyValue(name).trim();
  glow("glow-path", color("--gold"), 0.6);
  glow("glow-sugg", color("--star"), 0.42);
  glow("glow-outside", color("--deep"), 0.45);
  defs.append("marker")
    .attr("id", "arrow").attr("viewBox", "0 0 10 10").attr("refX", 10).attr("refY", 5)
    .attr("markerWidth", 8).attr("markerHeight", 8).attr("markerUnits", "userSpaceOnUse").attr("orient", "auto")
    .append("path").attr("d", "M0,1.2 L10,5 L0,8.8 L2.6,5 Z").attr("fill", color("--teal"));

  const fieldG = svg.append("g").attr("class", "field").attr("aria-hidden", "true");
  const gridG = svg.append("g").attr("class", "grid").attr("aria-hidden", "true");
  const edgeG = svg.append("g").attr("class", "edges").attr("aria-hidden", "true");
  const starG = svg.append("g").attr("class", "stars");
  const labelG = svg.append("g").attr("class", "labels");
  let edgeSel, starSel, labelSel;
  let hovered = null, selected = null;
  // Each suggestion shows only its link to the paper it branches from; its other links appear while it
  // (or the paper at their other end) is hovered or selected, so the resting picture stays a clean tree.
  const touches = (e, n) => e.source === n.id || e.target === n.id;
  const shown = (e) => e.primary || [hovered, selected].some((n) => n && touches(e, n));
  const showEdges = () => edgeSel.classed("shown", shown);

  const drawField = () => {
    const W = box.clientWidth, H = box.clientHeight, rnd = d3.randomLcg(0.42);
    const dots = d3.range(Math.round((W * H) / 2400)).map(() => [rnd() * W, rnd() * H, 0.3 + rnd() ** 4 * 1.1, 0.1 + rnd() * 0.45]);
    fieldG.selectAll("circle").data(dots).join("circle")
      .attr("cx", (d) => d[0]).attr("cy", (d) => d[1]).attr("r", (d) => d[2]).attr("fill-opacity", (d) => d[3]);
  };

  // A faint polar graticule centred on the path, like the rings of a planisphere.
  const drawGrid = () => {
    const c = T.apply([d3.mean(path, (n) => n.x), d3.mean(path, (n) => n.y)]);
    const radii = [170, 340, 510, 680, 850].map((r) => r * T.k);
    gridG.selectAll("circle").data(radii).join("circle").attr("cx", c[0]).attr("cy", c[1]).attr("r", (r) => r);
    const r0 = radii[0] * 0.55, r1 = radii[radii.length - 1];
    gridG.selectAll("line").data(d3.range(12)).join("line")
      .attr("x1", (i) => c[0] + Math.cos((i * Math.PI) / 6) * r0).attr("y1", (i) => c[1] + Math.sin((i * Math.PI) / 6) * r0)
      .attr("x2", (i) => c[0] + Math.cos((i * Math.PI) / 6) * r1).attr("y2", (i) => c[1] + Math.sin((i * Math.PI) / 6) * r1);
  };

  const reticle = (r, len) => `M${-r - len},0H${-r}M${r},0H${r + len}M0,${-r - len}V${-r}M0,${r}V${r + len}`;

  const build = () => {
    edgeSel = edgeG.selectAll("g.edge").data(edges, (e) => e.key).join((enter) => {
      const g = enter.append("g");
      g.filter((e) => e.type === "trail").append("path").attr("class", "glow");
      g.append("path").attr("class", "line").attr("marker-end", (e) => (e.type === "builds_on" ? "url(#arrow)" : null));
      return g;
    });
    edgeSel.attr("class", (e) => `edge ${e.type}${e.primary ? "" : " secondary"}`);
    showEdges();

    starSel = starG.selectAll("g.star").data(nodes, (n) => n.id).join((enter) => {
      const g = enter.append("g")
        .attr("class", (n) => `star ${kindOf(n)}`)
        .attr("tabindex", 0)
        .attr("role", "button")
        .attr("aria-label", (n) => (n.kind === "path" ? `Stop ${n.order} on your path: ${n.title}` : n.title))
        .style("animation-delay", (n, i) => `${n.kind === "path" ? (n.order - 1) * 70 : 180 + i * 22}ms`);
      g.append("circle").attr("class", "halo").attr("r", (n) => n.r * (n.kind === "path" ? 3.6 : 3.2))
        .attr("fill", (n) => `url(#glow-${kindOf(n)})`);
      g.filter((n) => n.kind === "path").append("circle").attr("class", "ring").attr("r", (n) => n.r + 3.5);
      g.append("circle").attr("class", "core").attr("r", (n) => n.r);
      g.filter((n) => n.kind === "path").append("text").attr("class", "num").text((n) => n.order);
      g.append("path").attr("class", "reticle").attr("d", (n) => reticle(n.r + (n.kind === "path" ? 9 : 6), 5));
      g.append("circle").attr("class", "hit").attr("r", (n) => Math.max(n.r + 7, 13));
      return g;
    });

    labelSel = labelG.selectAll("text.lbl").data(nodes, (n) => n.id).join((enter) =>
      enter.append("text").attr("class", (n) => `lbl ${kindOf(n)}`).attr("aria-hidden", "true")
        .style("animation-delay", (n, i) => `${n.kind === "path" ? 120 : 320 + i * 22}ms`));

    for (const sel of [starSel, labelSel]) {
      sel.on("mouseenter", (ev, n) => hover(n))
        .on("mouseleave", () => hover(null))
        .on("click", (ev, n) => {
          ev.stopPropagation();
          select(n);
        });
    }
    starSel.on("focus", (ev, n) => hover(n))
      .on("blur", () => hover(null))
      .on("keydown", (ev, n) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          select(n);
        }
      });
  };

  // -- labels -------------------------------------------------------------------------------------
  const SLOTS = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];
  const segHits = (a, b, r) => {
    let t0 = 0, t1 = 1;
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const clip = (p, q) => {
      if (p === 0) return q >= 0;
      const t = q / p;
      if (p < 0) {
        if (t > t1) return false;
        if (t > t0) t0 = t;
      } else {
        if (t < t0) return false;
        if (t < t1) t1 = t;
      }
      return true;
    };
    return clip(-dx, a[0] - r.x0) && clip(dx, r.x1 - a[0]) && clip(-dy, a[1] - r.y0) && clip(dy, r.y1 - a[1]);
  };
  const grow = (r, pad) => ({ x0: r.x0 - pad, y0: r.y0 - pad, x1: r.x1 + pad, y1: r.y1 + pad });
  const overlaps = (r, q) => r.x0 < q.x1 && q.x0 < r.x1 && r.y0 < q.y1 && q.y0 < r.y1;
  const hitsCircle = (r, c, rad) => {
    const dx = Math.max(r.x0 - c[0], 0, c[0] - r.x1), dy = Math.max(r.y0 - c[1], 0, c[1] - r.y1);
    return dx * dx + dy * dy < rad * rad;
  };

  const rectFor = (n, text, at, [ux, uy], far) => {
    const g = n.r + (n.kind === "path" ? 9 : 6) + (far ? 14 : 0);
    const d = ux && uy ? g * 0.74 : g;
    const x0 = ux > 0 ? at[0] + d : ux < 0 ? at[0] - d - text.w : at[0] - text.w / 2;
    const y0 = uy > 0 ? at[1] + d : uy < 0 ? at[1] - d - text.h : at[1] - text.h / 2;
    return { x0, y0, x1: x0 + text.w, y1: y0 + text.h, ux, uy, text, key: `${ux}${uy}${far ? "+" : ""}` };
  };

  const placeLabels = () => {
    const W = box.clientWidth, H = box.clientHeight;
    const origin = box.getBoundingClientRect();
    const blocked = ui.map((el) => {
      const b = el.getBoundingClientRect();
      return { x0: b.left - origin.left - 8, y0: b.top - origin.top - 8, x1: b.right - origin.left + 8, y1: b.bottom - origin.top + 8 };
    });
    const at = new Map(nodes.map((n) => [n.id, P(n)]));
    const segs = [];
    for (const e of edges.filter(shown)) {
      const pad = e.type === "trail" ? 5 : 2.5;
      for (let i = 1; i < e.geo.pts.length; i++) segs.push([e.geo.pts[i - 1], e.geo.pts[i], pad]);
    }
    const lit = hovered ? adj.get(hovered.id) : null;
    const rank = (n) =>
      n.kind === "path" ? (path.length - n.order) / 100 : n === selected ? 1 : n === hovered ? 2 : lit && lit.has(n.id) ? 3 : 4 + (1 - n.strength);
    const placed = [];
    for (const n of [...nodes].sort((a, b) => rank(a) - rank(b))) {
      const p = at.get(n.id);
      // Prefer the side facing away from the star's own links, and the spot it had before (no jitter).
      let ax = 0, ay = 0;
      for (const id of adj.get(n.id)) {
        const q = at.get(id), l = dist(p, q) || 1;
        ax -= (q[0] - p[0]) / l;
        ay -= (q[1] - p[1]) / l;
      }
      const al = Math.hypot(ax, ay);
      const score = (o) => {
        const ol = Math.hypot(o.ux, o.uy);
        const own = n.slot && o.ux === n.slot[0] && o.uy === n.slot[1] ? 2 : 0;
        return own + (al ? (o.ux * ax + o.uy * ay) / (ol * al) : o.uy === 0 ? 0.3 : 0) + (o.uy === 0 ? 0.15 : 0) - (o.key.endsWith("+") ? 0.6 : 0) + (n.place && n.place.key === o.key ? 0.5 : 0);
      };
      // A path paper's label falls back to a narrower wrap rather than disappearing.
      const options = n.texts.flatMap((text) =>
        [false, true].flatMap((far) => SLOTS.map((s) => rectFor(n, text, p, s, far))).sort((a, b) => score(b) - score(a)),
      );
      n.place = null;
      if (p[0] < -40 || p[1] < -40 || p[0] > W + 40 || p[1] > H + 40) continue;
      for (const o of options) {
        if (o.x0 < 6 || o.y0 < 6 || o.x1 > W - 6 || o.y1 > H - 6) continue;
        if (blocked.some((b) => overlaps(o, b))) continue;
        if (placed.some((q) => overlaps(grow(o, 4), q))) continue;
        if (nodes.some((m) => hitsCircle(o, at.get(m.id), m.r + 3))) continue;
        if (segs.some(([a, b, pad]) => segHits(a, b, grow(o, pad)))) continue;
        n.place = o;
        placed.push(o);
        break;
      }
    }
    labelSel.each(function (n) {
      const o = n.place;
      const t = d3.select(this).attr("display", o ? null : "none");
      if (!o) return;
      const anchor = o.ux > 0 ? "start" : o.ux < 0 ? "end" : "middle";
      const x = anchor === "start" ? o.x0 : anchor === "end" ? o.x1 : (o.x0 + o.x1) / 2;
      t.attr("text-anchor", anchor)
        .selectAll("tspan").data(o.text.lines).join("tspan").text((l) => l)
        .attr("x", x).attr("y", (l, i) => o.y0 + n.size * 0.9 + i * n.lead);
    });
  };

  const render = () => {
    for (const e of edges) e.geo = geometry(e);
    edgeSel.select(".line").attr("d", (e) => e.geo.d);
    edgeSel.select(".glow").attr("d", (e) => e.geo.d);
    starSel.attr("transform", (n) => `translate(${P(n).map((v) => v.toFixed(1))})`);
    drawGrid();
    placeLabels();
    if (hovered) placeTip(hovered);
  };

  // -- hover, tooltip, selection --------------------------------------------------------------------
  // The tooltip takes whichever corner around the star covers the fewest of the stars it is tracing.
  const placeTip = (n) => {
    const [x, y] = P(n), W = box.clientWidth, H = box.clientHeight;
    const w = tip.offsetWidth, h = tip.offsetHeight, g = n.r + 12;
    const traced = [...adj.get(n.id)].map((id) => P(byId.get(id)));
    const spots = [[x + g, y - h - g], [x - w - g, y - h - g], [x + g, y + g], [x - w - g, y + g]]
      .map(([left, top]) => [Math.min(Math.max(8, left), W - w - 8), Math.min(Math.max(8, top), H - h - 8)]);
    const covers = ([left, top]) =>
      traced.filter(([sx, sy]) => sx > left - 14 && sx < left + w + 14 && sy > top - 14 && sy < top + h + 14).length +
      (x > left - g && x < left + w + g && y > top - g && y < top + h + g ? 99 : 0);
    const [left, top] = spots.reduce((best, s) => (covers(s) < covers(best) ? s : best));
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };
  const kindText = (n) => (n.kind === "path" ? `stop ${n.order} on your path` : n.in_index ? "suggestion" : "not in your index");
  const showTip = (n) => {
    if (!n) {
      tip.hidden = true;
      return;
    }
    const t = document.createElement("p");
    t.className = "t";
    t.textContent = n.title;
    const m = document.createElement("p");
    m.className = "m";
    m.textContent = [n.year, n.category, kindText(n)].filter(Boolean).join(" · ");
    tip.replaceChildren(t, m);
    tip.hidden = false;
    placeTip(n);
  };
  const hover = (n) => {
    hovered = n;
    const lit = n ? new Set([n.id, ...adj.get(n.id)]) : null;
    svg.classed("dimmed", !!n);
    starSel.classed("lit", (m) => !!lit && lit.has(m.id));
    labelSel.classed("lit", (m) => !!lit && lit.has(m.id));
    edgeSel.classed("lit", (e) => !!n && touches(e, n));
    showEdges();
    placeLabels();
    showTip(n);
  };

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const NS = "http://www.w3.org/2000/svg";
  const glyph = (kind) => {
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("width", "26");
    s.setAttribute("height", "8");
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", "1");
    line.setAttribute("y1", "4");
    line.setAttribute("x2", kind === "similar" ? "25" : "19");
    line.setAttribute("y2", "4");
    line.setAttribute("stroke-width", kind === "similar" ? "1.5" : "1.3");
    line.setAttribute("class", kind === "similar" ? "lg-dots" : "lg-teal");
    s.append(line);
    if (kind !== "similar") {
      const head = document.createElementNS(NS, "path");
      head.setAttribute("d", kind === "follow_up" ? "M7 0.6 L-0.5 4 L7 7.4 L5 4 Z" : "M18 0.6 L25.5 4 L18 7.4 L20 4 Z");
      head.setAttribute("class", "lg-teal");
      s.append(head);
    }
    return s;
  };
  const titleOf = (id) => shortTitle(byId.get(id) ? byId.get(id).title : id);
  const reasonText = (r) => {
    if (r.kind === "builds_on") return `Your paper “${titleOf(r.from)}” builds on it`;
    if (r.kind === "follow_up") return `It builds on your paper “${titleOf(r.from)}”`;
    return `Close in topic to “${titleOf(r.from)}”`;
  };
  const authorsText = (names) =>
    names.length > 4 ? `${names.slice(0, 3).join(", ")} and ${names.length - 3} more` : names.join(", ");
  const link = (cls, text, href, external) => {
    const a = el("a", cls, text);
    a.href = href;
    if (external) a.rel = "noopener";
    return a;
  };

  const show = (n) => {
    panel.replaceChildren();
    const kicker = n.kind === "path" ? `Stop ${n.order} on your path` : n.in_index ? "Suggested" : "Not in your index";
    panel.append(el("p", `p-kicker ${kindOf(n)}`, kicker), el("h2", "p-title", n.title));
    if (n.authors && n.authors.length) panel.append(el("p", "p-authors", authorsText(n.authors)));
    const meta = [n.year, n.category, n.arxiv_id && !n.in_index ? `arXiv ${n.arxiv_id}` : null].filter(Boolean).join(" · ");
    if (meta) panel.append(el("p", "p-meta", meta));
    const actions = el("div", "p-actions");
    if (n.kind === "path" || n.in_index) {
      const r = n.kind === "suggestion" ? n.reasons[0] : null;
      const kind = r && (r.kind === "follow_up" ? "followed_by" : r.kind);
      const href = r ? `/paper/${n.id}?via=${encodeURIComponent(r.from)}&kind=${kind}&web=${webId}` : `/paper/${n.id}`;
      actions.append(
        link("p-btn", r ? "Read it" : "Open paper", href),
        link("p-btn ghost", "High-level design", `/paper/${n.id}/design`),
        link(null, "arXiv ↗", `https://arxiv.org/abs/${n.id}`, true),
      );
    } else if (n.arxiv_id) {
      actions.append(link("p-btn", "Open on arXiv ↗", `https://arxiv.org/abs/${n.arxiv_id}`, true));
    } else {
      actions.append(link("p-btn", "Find on arXiv ↗", `https://arxiv.org/search/?searchtype=title&query=${encodeURIComponent(n.title)}`, true));
    }
    if (n.kind === "suggestion") {
      const no = el("button", "p-quiet", "Not interested");
      no.type = "button";
      no.addEventListener("click", () => dismiss(n));
      actions.append(no);
    }
    panel.append(actions);
    if (n.kind === "suggestion" && n.in_index) {
      panel.append(el("p", "p-fine", `Reading it for ${dwell} seconds adds it to your path, and new suggestions branch from it.`));
    }
    if (n.kind === "suggestion") {
      panel.append(el("p", "p-label", "Why it's here"));
      const ul = el("ul", "p-why");
      for (const r of n.reasons) {
        const li = el("li");
        li.append(glyph(r.kind), el("span", null, reasonText(r)));
        ul.append(li);
      }
      panel.append(ul);
    }
    if (n.abstract) panel.append(el("p", "p-label", "Abstract"), el("p", "p-abstract", n.abstract));
    else if (!n.in_index) panel.append(el("p", "p-fine", "This paper isn't in your index, so there's no abstract here yet."));
    panel.scrollTop = 0;
  };

  const select = (n) => {
    selected = n;
    starSel.classed("selected", (m) => m === n);
    labelSel.classed("selected", (m) => m === n);
    if (n) show(n);
    else panel.innerHTML = hintHTML;
    showEdges();
    placeLabels();
  };

  const dismiss = (n) => {
    fetch("/suggestions/dismiss", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key: n.arxiv_id || n.key }),
    }).then((r) => {
      if (!r.ok) return;
      nodes = nodes.filter((m) => m !== n);
      edges = edges.filter((e) => e.source !== n.id && e.target !== n.id);
      byId.delete(n.id);
      index();
      build();
      hover(null);
      select(null);
      render();
    });
  };

  // -- start ----------------------------------------------------------------------------------------
  const zoom = d3.zoom().scaleExtent([0.3, 4]).on("zoom", (ev) => {
    T = ev.transform;
    render();
  });
  const fit = (ms) => {
    const W = box.clientWidth, H = box.clientHeight, legend = 40;
    const [x0, x1] = d3.extent(nodes, (n) => n.x), [y0, y1] = d3.extent(nodes, (n) => n.y);
    const padX = Math.min(320, W * 0.3), padY = Math.min(160, H * 0.2);
    const k = Math.max(0.3, Math.min(1.5, (W - padX) / Math.max(1, x1 - x0), (H - padY - legend) / Math.max(1, y1 - y0)));
    const t = d3.zoomIdentity.translate(W / 2 - (k * (x0 + x1)) / 2, (H - legend) / 2 - (k * (y0 + y1)) / 2).scale(k);
    (ms && !reduceMotion ? svg.transition().duration(ms).ease(d3.easeCubicInOut) : svg).call(zoom.transform, t);
  };

  const start = () => {
    for (const n of nodes) {
      n.strength = n.kind === "path" ? 1 : Math.sqrt((n.score || 0) / maxScore);
      n.r = n.kind === "path" ? (n.order === path.length ? 10.5 : 8.5) : 2.6 + 5.4 * n.strength;
      n.primary = n.kind === "suggestion" ? n.reasons[0].from : null;
      n.size = n.kind === "path" ? 13 : 12;
      n.lead = n.size + 2;
      const weight = n.kind === "path" ? 600 : 400;
      n.texts = (n.kind === "path" ? [180, 112] : [140]).map((maxW) => {
        const lines = wrap(shortTitle(n.title), weight, n.size, maxW);
        return { lines, w: Math.ceil(d3.max(lines, (l) => measure(l, weight, n.size))), h: lines.length * n.lead };
      });
    }
    index();
    layout();
    build();
    drawField();
    svg.call(zoom).on("dblclick.zoom", null).on("click", () => select(null));
    fit(0);
    // After the opening reveal, a label that reappears (zoom, hover) fades in at once.
    setTimeout(() => svg.classed("settled", true), 1500);
    document.getElementById("fit").addEventListener("click", () => fit(650));
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") select(null);
    });
    new ResizeObserver(() => {
      drawField();
      render();
    }).observe(box);
    // #paper=<id> opens the web with that paper selected (linked from paper pages).
    const linked = /^#paper=(.+)$/.exec(location.hash);
    if (linked && byId.has(decodeURIComponent(linked[1]))) select(byId.get(decodeURIComponent(linked[1])));
  };

  const fonts = ['400 12px "Plex Condensed"', '600 13px "Plex Condensed"', '500 10px "Plex Mono"'];
  Promise.all(fonts.map((f) => document.fonts.load(f))).catch(() => null).then(start);
})();
