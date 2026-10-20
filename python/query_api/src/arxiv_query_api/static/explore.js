// Explore page: draws one web as a graph (decision 38). The reader's path is a fixed spine in reading
// order; suggestions settle around it under a force layout. Data comes from the page (#graph-data).
(() => {
  const data = JSON.parse(document.getElementById("graph-data").textContent);
  const box = document.getElementById("graph");
  if (!box || typeof cytoscape === "undefined") return;
  cytoscape.use(cytoscapeFcose);

  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  const color = {
    path: v("--path"), pathSoft: v("--path-soft"), accent: v("--accent"), accentSoft: v("--accent-soft"),
    text: v("--text"), muted: v("--muted"), faint: v("--faint"), line: v("--line-strong"), bg: v("--bg"),
    surface: v("--surface"),
  };
  const webId = data.web.id;
  const byId = new Map(data.nodes.map((n) => [n.id, n]));
  const short = (t) => {
    const head = t.split(":")[0].trim();
    if (head !== t && head.length <= 26) return head;
    const words = t.split(/\s+/);
    return words.length > 5 ? words.slice(0, 5).join(" ") + "…" : t;
  };

  const path = data.nodes.filter((n) => n.kind === "path");
  const suggestions = data.nodes.filter((n) => n.kind === "suggestion");
  const labelled = new Set([...suggestions].sort((a, b) => b.score - a.score).slice(0, 6).map((n) => n.id));
  const elements = data.nodes.map((n) => ({
    data: { ...n, label: n.kind === "path" ? `${n.order}. ${short(n.title)}` : short(n.title) },
    classes: [n.kind === "path" ? "path" : n.in_index ? "suggestion" : "external", labelled.has(n.id) ? "labelled" : ""].join(" "),
  }));
  // Each suggestion hangs off its strongest reason only: one branch per suggestion keeps the picture a
  // readable tree around the path. All reasons stay in the side panel.
  const primary = new Map(suggestions.map((n) => [n.id, n.reasons[0]]));
  const keep = (e) => {
    if (e.type === "trail") return true;
    const s = primary.get(e.source), t = primary.get(e.target);
    if (!s && !t) return true; // a citation between two papers on the path
    const sugg = s ? e.source : e.target, other = s ? e.target : e.source;
    return primary.get(sugg).from === other;
  };
  for (const e of data.edges) {
    if (!byId.has(e.source) || !byId.has(e.target) || !keep(e)) continue;
    elements.push({ data: { id: `${e.source}|${e.target}|${e.type}`, source: e.source, target: e.target }, classes: e.type });
  }

  // The path is pinned in reading order and wraps like a snake after four papers, so it stays the spine of
  // the picture however long it grows; suggestions settle around the paper they branch from.
  const step = 215, perRow = 4, rowGap = 270;
  const fixed = path.map((n, i) => {
    const row = Math.floor(i / perRow), col = i % perRow;
    return { nodeId: n.id, position: { x: (row % 2 ? perRow - 1 - col : col) * step, y: row * rowGap } };
  });

  const cy = cytoscape({
    container: box,
    elements,
    minZoom: 0.25,
    maxZoom: 2.5,
    wheelSensitivity: 0.25,
    style: [
      { selector: "node", style: {
          "background-color": color.accentSoft, "border-color": color.accent, "border-width": 2,
          width: "mapData(score, 0, 2.5, 20, 34)", height: "mapData(score, 0, 2.5, 20, 34)",
          label: "", color: color.text, "font-family": "system-ui, -apple-system, sans-serif", "font-size": 13,
          "text-valign": "bottom", "text-margin-y": 6, "text-wrap": "wrap", "text-max-width": 150,
          "text-background-color": color.bg, "text-background-opacity": 0.85, "text-background-padding": 2,
          "text-background-shape": "roundrectangle", "transition-property": "opacity, border-width",
          "transition-duration": 150 } },
      { selector: "node.path", style: {
          "background-color": color.path, "border-color": color.pathSoft, "border-width": 7, width: 38, height: 38,
          label: "data(label)", "font-size": 15, "font-weight": 600, "text-margin-y": 8, "z-index": 20 } },
      { selector: "node.labelled", style: { label: "data(label)", color: color.muted } },
      { selector: "node.external", style: {
          "background-color": color.surface, "border-color": color.faint, "border-style": "dashed",
          width: 20, height: 20 } },
      { selector: "node.hover, node:selected", style: { label: "data(label)", "z-index": 30 } },
      { selector: "node:selected", style: { "border-width": 4, "border-color": color.accent } },
      { selector: "node.path:selected", style: { "border-color": color.accent, "border-width": 5 } },
      { selector: "edge", style: {
          width: 1.4, "line-color": color.line, "curve-style": "bezier", opacity: 0.75,
          "transition-property": "opacity", "transition-duration": 150 } },
      { selector: "edge.trail", style: {
          width: 7, "line-color": color.path, "line-cap": "round", opacity: 0.9, "z-index": 10,
          "curve-style": "straight" } },
      { selector: "edge.builds_on", style: {
          width: 1.6, "line-color": color.muted, "target-arrow-shape": "triangle", "target-arrow-color": color.muted,
          "arrow-scale": 0.85 } },
      { selector: "edge.similar", style: {
          "line-style": "dashed", "line-dash-pattern": [5, 5], "line-color": color.accent, opacity: 0.45 } },
      { selector: ".faded", style: { opacity: 0.1 } },
    ],
    layout: {
      name: "fcose",
      quality: "proof",
      randomize: true,
      animate: false,
      fixedNodeConstraint: fixed.length ? fixed : undefined,
      nodeRepulsion: () => 14000,
      idealEdgeLength: (e) => (e.hasClass("trail") ? step : 125),
      edgeElasticity: () => 0.45,
      nodeSeparation: 110,
      gravity: 0.15,
      padding: 40,
    },
  });
  cy.ready(() => cy.fit(undefined, 40));

  // Hover: light up a node's neighbourhood so "why is this here" is visible at a glance.
  cy.on("mouseover", "node", (ev) => {
    const hood = ev.target.closedNeighborhood();
    cy.elements().not(hood).addClass("faded");
    ev.target.addClass("hover");
  });
  cy.on("mouseout", "node", (ev) => {
    cy.elements().removeClass("faded");
    ev.target.removeClass("hover");
  });

  // Side panel, built with DOM calls (titles are data, never markup).
  const panel = document.getElementById("panel");
  const hint = panel.innerHTML;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const titleOf = (id) => (byId.get(id) ? byId.get(id).title : id);
  const reasonLine = (r) => {
    const t = `“${short(titleOf(r.from))}”`;
    if (r.kind === "builds_on") return `Your paper ${t} builds on it`;
    if (r.kind === "follow_up") return `It builds on your paper ${t}`;
    return `Similar to ${t}`;
  };
  const openHref = (n) => {
    if (n.kind === "path") return `/paper/${n.id}`;
    const r = n.reasons[0];
    const kind = r.kind === "follow_up" ? "followed_by" : r.kind;
    return `/paper/${n.id}?via=${encodeURIComponent(r.from)}&kind=${kind}&web=${webId}`;
  };

  const show = (n) => {
    panel.replaceChildren();
    const badge = n.kind === "path" ? `On your path · #${n.order}` : n.in_index ? "Suggestion" : "Outside this index";
    panel.append(el("p", `panel-badge ${n.kind === "path" ? "on-path" : n.in_index ? "sugg" : "ext"}`, badge));
    panel.append(el("h3", "panel-title", n.title));
    const meta = [n.category, n.year].filter(Boolean).join(" · ");
    if (meta) panel.append(el("p", "r-meta", meta));
    if (n.summary) panel.append(el("p", "panel-summary", n.summary));
    if (n.kind === "suggestion") {
      panel.append(el("p", "io-label", "Why it is here"));
      const ul = el("ul", "panel-reasons");
      n.reasons.forEach((r) => ul.append(el("li", `why-${r.kind}`, reasonLine(r))));
      panel.append(ul);
    }
    const actions = el("div", "panel-actions");
    if (n.in_index || n.kind === "path") {
      const open = el("a", "btn solid", n.kind === "path" ? "Open paper" : "Read it");
      open.href = openHref(n);
      actions.append(open);
      const design = el("a", "btn", "High-level design");
      design.href = `/paper/${n.id}/design`;
      actions.append(design);
    } else {
      // Outside the index: open it on arXiv when the id is known, else search arXiv by title.
      const ext = el("a", "btn", n.arxiv_id ? "Open on arXiv" : "Find on arXiv");
      ext.href = n.arxiv_id
        ? `https://arxiv.org/abs/${n.arxiv_id}`
        : `https://arxiv.org/search/?searchtype=title&query=${encodeURIComponent(n.title)}`;
      ext.rel = "noopener";
      actions.append(ext);
    }
    if (n.kind === "suggestion") {
      const no = el("button", "link-btn", "Not interested");
      no.type = "button";
      no.addEventListener("click", () => dismiss(n));
      actions.append(no);
    }
    panel.append(actions);
    if (n.kind === "suggestion" && n.in_index) {
      panel.append(el("p", "fine", "Reading it for 10 seconds adds it to this path, and new suggestions branch from it."));
    }
  };

  const dismiss = (n) => {
    fetch("/suggestions/dismiss", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key: n.arxiv_id || n.key }),
    }).then((r) => {
      if (!r.ok) return;
      cy.getElementById(n.id).remove();
      panel.innerHTML = hint;
    });
  };

  cy.on("tap", "node", (ev) => show(ev.target.data()));
  // #paper=<id> opens the web with that paper selected (linked from paper pages).
  const linked = /^#paper=(.+)$/.exec(location.hash);
  if (linked) {
    const node = cy.getElementById(decodeURIComponent(linked[1]));
    if (node.nonempty()) {
      node.select();
      show(node.data());
    }
  }
  cy.on("tap", (ev) => {
    if (ev.target === cy) {
      cy.elements().unselect();
      panel.innerHTML = hint;
    }
  });
  document.getElementById("fit").addEventListener("click", () => cy.animate({ fit: { padding: 40 }, duration: 250 }));
})();
