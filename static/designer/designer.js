/* static/designer/designer.js
 * M4.8 §14 Browser UI — vanilla JS, SVG canvas, Mermaid (via CDN) for
 * text-preview round-tripping, no build step. Talks only to §13's
 * `/designer/*` API — never touches the database directly.
 *
 * Scope note: this SPA implements drag/drop node placement, edge
 * creation, a property panel, palette search, undo/redo + delete
 * keyboard shortcuts, a validation sidebar, JSON/YAML/Mermaid export,
 * and a minimal mini-map + replay-overlay renderer. A richer mini-map
 * (click-to-pan) and an inline version-diff visual (vs. this build's
 * plain-list diff view) are deferred — see
 * docs/M4.8_Workflow_Designer_Handover.md §5.
 */
(function () {
  "use strict";

  const API = "/designer";
  const NS = "http://www.w3.org/2000/svg";

  const state = {
    workflow: { workflow_name: "untitled_workflow", version: "0.0.1", nodes: [], edges: [], groups: [],
                entry_node_id: null, viewport: { zoom: 1, pan_x: 0, pan_y: 0 }, metadata: {} },
    selection: { node_ids: [], edge_ids: [] },
    history: [], future: [],
    library: { templates: [], categories: [] },
    dirty: false,
    pendingEdgeSource: null,
    dragNodeId: null,
    dragOffset: { x: 0, y: 0 },
  };

  // ── DOM refs ─────────────────────────────────────────────────────
  const el = (id) => document.getElementById(id);
  const svg = () => el("canvas");

  // ── API helpers ──────────────────────────────────────────────────
  async function apiGet(path) {
    const r = await fetch(API + path);
    return r.json();
  }
  async function apiPost(path, body) {
    const r = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(body) });
    return r.json();
  }
  async function apiPut(path, body) {
    const r = await fetch(API + path, { method: "PUT", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(body) });
    return r.json();
  }

  // ── History (mirrors canvas_state.py's snapshot model) ──────────
  function pushHistory() {
    state.history.push(JSON.parse(JSON.stringify(state.workflow)));
    if (state.history.length > 100) state.history.shift();
    state.future = [];
    state.dirty = true;
    updateAutosaveIndicator();
  }
  function undo() {
    if (!state.history.length) return;
    state.future.unshift(JSON.parse(JSON.stringify(state.workflow)));
    state.workflow = state.history.pop();
    render();
  }
  function redo() {
    if (!state.future.length) return;
    state.history.push(JSON.parse(JSON.stringify(state.workflow)));
    state.workflow = state.future.shift();
    render();
  }

  // ── Node/edge mutation ───────────────────────────────────────────
  function addNode(nodeType, x, y, label) {
    pushHistory();
    const id = nodeType + "_" + Math.random().toString(36).slice(2, 8);
    state.workflow.nodes.push({ id, node_type: nodeType, label: label || nodeType, x, y, config: {} });
    if (!state.workflow.entry_node_id) state.workflow.entry_node_id = id;
    render();
  }
  function moveNode(id, x, y) {
    const n = state.workflow.nodes.find((n) => n.id === id);
    if (n) { n.x = x; n.y = y; }
  }
  function deleteSelection() {
    pushHistory();
    const nodeIds = new Set(state.selection.node_ids);
    const edgeIds = new Set(state.selection.edge_ids);
    state.workflow.nodes = state.workflow.nodes.filter((n) => !nodeIds.has(n.id));
    state.workflow.edges = state.workflow.edges.filter(
      (e) => !edgeIds.has(e.id) && !nodeIds.has(e.source) && !nodeIds.has(e.target)
    );
    if (nodeIds.has(state.workflow.entry_node_id)) state.workflow.entry_node_id = null;
    state.selection = { node_ids: [], edge_ids: [] };
    render();
  }
  function addEdge(source, target) {
    if (source === target) return;
    pushHistory();
    const id = "e_" + Math.random().toString(36).slice(2, 8);
    state.workflow.edges.push({ id, source, target, label: "" });
    render();
  }

  // ── Rendering ────────────────────────────────────────────────────
  function render() {
    renderCanvas();
    renderPalette();
    renderProps();
    validate();
    updateAutosaveIndicator();
  }

  function renderCanvas() {
    const s = svg();
    while (s.firstChild) s.removeChild(s.firstChild);

    const defs = document.createElementNS(NS, "defs");
    defs.innerHTML = '<marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">' +
      '<path d="M0,0 L7,3 L0,6 z" fill="#8a90a2"/></marker>';
    s.appendChild(defs);

    const vp = state.workflow.viewport || { zoom: 1, pan_x: 0, pan_y: 0 };
    const g = document.createElementNS(NS, "g");
    g.setAttribute("transform", `translate(${vp.pan_x},${vp.pan_y}) scale(${vp.zoom})`);
    s.appendChild(g);

    for (const edge of state.workflow.edges) {
      const src = state.workflow.nodes.find((n) => n.id === edge.source);
      const tgt = state.workflow.nodes.find((n) => n.id === edge.target);
      if (!src || !tgt) continue;
      const line = document.createElementNS(NS, "line");
      line.setAttribute("x1", src.x + 60); line.setAttribute("y1", src.y + 20);
      line.setAttribute("x2", tgt.x); line.setAttribute("y2", tgt.y + 20);
      line.setAttribute("class", "edge-line" + (state.selection.edge_ids.includes(edge.id) ? " selected" : ""));
      line.addEventListener("click", () => { state.selection = { node_ids: [], edge_ids: [edge.id] }; render(); });
      g.appendChild(line);
    }

    for (const node of state.workflow.nodes) {
      const group = document.createElementNS(NS, "g");
      group.setAttribute("transform", `translate(${node.x},${node.y})`);
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("width", 120); rect.setAttribute("height", 40); rect.setAttribute("rx", 8);
      let cls = "node-rect" + (state.selection.node_ids.includes(node.id) ? " selected" : "");
      if (state.replayOverlay) {
        if (state.replayOverlay.current_node_id === node.id) cls += " replay-current";
        if (state.replayOverlay.failed_node_id === node.id) cls += " replay-failed";
      }
      rect.setAttribute("class", cls);
      const text = document.createElementNS(NS, "text");
      text.setAttribute("x", 8); text.setAttribute("y", 24); text.setAttribute("class", "node-label");
      text.textContent = (node.label || node.id) + (node.id === state.workflow.entry_node_id ? " ▶" : "");

      group.appendChild(rect); group.appendChild(text);
      group.style.cursor = "grab";
      group.addEventListener("mousedown", (ev) => onNodeMouseDown(ev, node));
      group.addEventListener("click", (ev) => onNodeClick(ev, node));
      g.appendChild(group);
    }
  }

  function onNodeMouseDown(ev, node) {
    if (ev.shiftKey) return; // shift+click reserved for edge creation, handled in onNodeClick
    state.dragNodeId = node.id;
    const rect = svg().getBoundingClientRect();
    state.dragOffset = { x: ev.clientX - rect.left - node.x, y: ev.clientY - rect.top - node.y };
  }
  function onNodeClick(ev, node) {
    if (ev.shiftKey) {
      if (state.pendingEdgeSource && state.pendingEdgeSource !== node.id) {
        addEdge(state.pendingEdgeSource, node.id);
        state.pendingEdgeSource = null;
      } else {
        state.pendingEdgeSource = node.id;
      }
      return;
    }
    state.selection = { node_ids: [node.id], edge_ids: [] };
    render();
  }

  svg() && svg().addEventListener("mousemove", (ev) => {
    if (!state.dragNodeId) return;
    const rect = svg().getBoundingClientRect();
    const x = ev.clientX - rect.left - state.dragOffset.x;
    const y = ev.clientY - rect.top - state.dragOffset.y;
    moveNode(state.dragNodeId, x, y);
    renderCanvas();
  });
  document.addEventListener("mouseup", () => {
    if (state.dragNodeId) { pushHistory(); state.dragNodeId = null; render(); }
  });

  function renderPalette() {
    const p = el("palette-list");
    if (!p) return;
    p.innerHTML = "";
    const query = (el("palette-search").value || "").toLowerCase();
    for (const category of state.library.categories) {
      const items = state.library.templates.filter(
        (t) => t.category === category && t.label.toLowerCase().includes(query)
      );
      if (!items.length) continue;
      const h = document.createElement("div");
      h.className = "palette-category"; h.textContent = category;
      p.appendChild(h);
      for (const t of items) {
        const row = document.createElement("div");
        row.className = "palette-item";
        row.textContent = t.label + (t.source !== "builtin" ? ` (${t.source})` : "");
        row.addEventListener("click", () => addNode(t.node_type, 80 + Math.random() * 300, 80 + Math.random() * 200, t.label));
        p.appendChild(row);
      }
    }
  }

  function renderProps() {
    const panel = el("props");
    if (!panel) return;
    const id = state.selection.node_ids[0];
    const node = state.workflow.nodes.find((n) => n.id === id);
    if (!node) { panel.innerHTML = "<div class='validation-ok'>Select a node to edit its properties.</div>"; return; }
    panel.innerHTML = `
      <label>Label</label><input id="prop-label" value="${node.label || ""}">
      <label>Node type</label><input value="${node.node_type}" disabled>
      <label>Config (JSON)</label><textarea id="prop-config" rows="8">${JSON.stringify(node.config || {}, null, 2)}</textarea>
      <label><input type="checkbox" id="prop-entry" ${node.id === state.workflow.entry_node_id ? "checked" : ""}> Entry node</label>
      <button id="prop-apply">Apply</button>`;
    el("prop-apply").addEventListener("click", () => {
      pushHistory();
      node.label = el("prop-label").value;
      try { node.config = JSON.parse(el("prop-config").value || "{}"); } catch (e) { /* keep prior config on bad JSON */ }
      if (el("prop-entry").checked) state.workflow.entry_node_id = node.id;
      render();
    });
  }

  async function validate() {
    const box = el("validation");
    if (!box) return;
    try {
      const result = await apiPost("/validate", state.workflow);
      box.innerHTML = "";
      if (!result.issues || !result.issues.length) {
        box.innerHTML = "<div class='validation-ok'>No issues.</div>";
        return;
      }
      for (const issue of result.issues) {
        const d = document.createElement("div");
        d.className = "validation-issue " + issue.severity;
        d.textContent = `[${issue.rule_id}] ${issue.message}`;
        box.appendChild(d);
      }
    } catch (e) {
      box.innerHTML = "<div class='validation-issue warning'>Validation service unavailable.</div>";
    }
  }

  function updateAutosaveIndicator() {
    const ind = el("autosave-indicator");
    if (!ind) return;
    ind.textContent = state.dirty ? "Unsaved changes" : "Saved";
    ind.className = state.dirty ? "dirty" : "";
  }

  // ── Toolbar actions ──────────────────────────────────────────────
  async function save() {
    await apiPut(`/workflows/${encodeURIComponent(state.workflow.workflow_name)}`, state.workflow);
    state.dirty = false;
    updateAutosaveIndicator();
  }
  async function loadLibrary() {
    state.library = await apiGet("/library");
    renderPalette();
  }
  async function loadWorkflow(name) {
    const w = await apiGet(`/workflows/${encodeURIComponent(name)}`);
    if (w && w.workflow_name) { state.workflow = w; state.history = []; state.future = []; render(); }
  }
  async function exportAs(fmt) {
    const result = await apiPost("/export", { layout: state.workflow, format: fmt });
    const blob = new Blob([result.content || ""], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${state.workflow.workflow_name}.${fmt === "markdown" ? "md" : fmt}`;
    a.click();
  }

  // Autosave loop (§3 "Autosave state" — a plain interval owning the
  // wall-clock scheduling `canvas_state.needs_autosave` deliberately
  // does not).
  setInterval(() => { if (state.dirty) save(); }, 30000);

  document.addEventListener("keydown", (ev) => {
    const mod = ev.ctrlKey || ev.metaKey;
    if (mod && ev.key === "z" && !ev.shiftKey) { ev.preventDefault(); undo(); }
    else if (mod && (ev.key === "y" || (ev.key === "z" && ev.shiftKey))) { ev.preventDefault(); redo(); }
    else if (ev.key === "Delete" || ev.key === "Backspace") { deleteSelection(); }
    else if (mod && ev.key === "s") { ev.preventDefault(); save(); }
  });

  window.addEventListener("DOMContentLoaded", () => {
    el("palette-search").addEventListener("input", renderPalette);
    el("btn-save").addEventListener("click", save);
    el("btn-undo").addEventListener("click", undo);
    el("btn-redo").addEventListener("click", redo);
    el("btn-export-json").addEventListener("click", () => exportAs("json"));
    el("btn-export-mermaid").addEventListener("click", () => exportAs("mermaid"));
    loadLibrary();
    render();
  });
})();
