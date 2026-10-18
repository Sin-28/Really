const graphCanvas = document.querySelector("#graph-canvas");
const ctx = graphCanvas.getContext("2d");
const starCanvas = document.querySelector("#starfield");
const starCtx = starCanvas.getContext("2d");
const frame = document.querySelector(".canvas-frame");
const tooltip = document.querySelector("#node-tooltip");
const search = document.querySelector("#graph-search");

let graph = {nodes: [], edges: []};
let nodeById = new Map();
let width = 0;
let height = 0;
let scale = 1;
let panX = 0;
let panY = 0;
let selectedId = null;
let hoveredId = null;
let searchText = "";
let dragging = false;
let dragStart = null;
let stars = [];
let pixelRatio = Math.min(window.devicePixelRatio || 1, 2);

function seeded(seed) {
  let value = seed >>> 0;
  return () => {
    value = (value * 1664525 + 1013904223) >>> 0;
    return value / 4294967296;
  };
}

function resizeCanvas() {
  const rect = frame.getBoundingClientRect();
  width = rect.width;
  height = rect.height;
  pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  graphCanvas.width = Math.round(width * pixelRatio);
  graphCanvas.height = Math.round(height * pixelRatio);
  ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  starCanvas.width = Math.round(window.innerWidth * pixelRatio);
  starCanvas.height = Math.round(window.innerHeight * pixelRatio);
  starCtx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  makeStars();
  if (graph.nodes.length) {
    layoutGraph();
    drawGraph();
  }
}

function makeStars() {
  const random = seeded(914230);
  const count = Math.round((window.innerWidth * window.innerHeight) / 2400);
  stars = Array.from({length: count}, () => ({
    x: random() * window.innerWidth,
    y: random() * window.innerHeight,
    radius: random() * 1.25 + .15,
    alpha: random() * .62 + .16,
    tint: random(),
  }));
  starCtx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  for (const star of stars) {
    const color = star.tint > .85 ? `rgba(188,157,255,${star.alpha})` : `rgba(218,229,255,${star.alpha})`;
    starCtx.beginPath();
    starCtx.fillStyle = color;
    starCtx.arc(star.x, star.y, star.radius, 0, Math.PI * 2);
    starCtx.fill();
    if (star.radius > 1.05) {
      starCtx.shadowBlur = 8;
      starCtx.shadowColor = color;
      starCtx.fill();
      starCtx.shadowBlur = 0;
    }
  }
}

function layoutGraph() {
  const random = seeded(1397 + graph.nodes.length);
  const cx = width / 2;
  const cy = height / 2;
  graph.nodes.forEach((node, index) => {
    node.x = cx * (.14 + random() * 1.44);
    node.y = cy * (.12 + random() * 1.52);
    node.vx = 0;
    node.vy = 0;
    node.index = index;
  });
  for (let step = 0; step < 190; step += 1) {
    for (let i = 0; i < graph.nodes.length; i += 1) {
      const a = graph.nodes[i];
      for (let j = i + 1; j < graph.nodes.length; j += 1) {
        const b = graph.nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let distance2 = dx * dx + dy * dy + 18;
        const force = 185 / distance2;
        const distance = Math.sqrt(distance2);
        dx /= distance;
        dy /= distance;
        a.vx -= dx * force;
        a.vy -= dy * force;
        b.vx += dx * force;
        b.vy += dy * force;
      }
    }
    for (let i = 0; i < graph.edges.length; i += 1) {
      const edge = graph.edges[i];
      const a = nodeById.get(edge.source);
      const b = nodeById.get(edge.target);
      if (!a || !b) continue;
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      const distance = Math.sqrt(dx * dx + dy * dy) || 1;
      const desired = edge.relation === "contains" ? 86 : edge.relation === "is_a" ? 68 : 128;
      const force = (distance - desired) * .0038;
      dx /= distance;
      dy /= distance;
      a.vx += dx * force;
      a.vy += dy * force;
      b.vx -= dx * force;
      b.vy -= dy * force;
    }
    for (const node of graph.nodes) {
      node.vx += (cx - node.x) * .0018;
      node.vy += (cy - node.y) * .0018;
      node.vx *= .79;
      node.vy *= .79;
      node.x = Math.max(20, Math.min(width - 20, node.x + node.vx));
      node.y = Math.max(20, Math.min(height - 20, node.y + node.vy));
    }
  }
}

function isSearchMatch(node) {
  if (!searchText) return true;
  const haystack = [node.name, node.id, ...(node.aliases || [])].join(" ").toLowerCase();
  return haystack.includes(searchText);
}

function connectedIds(id) {
  const ids = new Set([id]);
  for (const edge of graph.edges) {
    if (edge.source === id) ids.add(edge.target);
    if (edge.target === id) ids.add(edge.source);
  }
  return ids;
}

function drawGraph() {
  ctx.clearRect(0, 0, width, height);
  ctx.save();
  ctx.translate(width / 2 + panX, height / 2 + panY);
  ctx.scale(scale, scale);
  ctx.translate(-width / 2, -height / 2);

  const focusIds = selectedId ? connectedIds(selectedId) : null;
  const selected = selectedId && nodeById.get(selectedId);
  for (const edge of graph.edges) {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) continue;
    const searched = !searchText || isSearchMatch(source) || isSearchMatch(target);
    const focused = !focusIds || (focusIds.has(source.id) && focusIds.has(target.id));
    const active = searched && focused;
    const substitution = edge.relation === "substitute_for";
    const typeEdge = edge.relation === "is_a";
    ctx.beginPath();
    ctx.moveTo(source.x, source.y);
    ctx.lineTo(target.x, target.y);
    ctx.strokeStyle = substitution
      ? `rgba(255,143,220,${active ? .62 : .055})`
      : typeEdge ? `rgba(245,207,117,${active ? .42 : .045})`
      : `rgba(145,151,255,${active ? .43 : .055})`;
    ctx.lineWidth = active && focusIds ? 1.7 : active ? 1.05 : .65;
    ctx.shadowBlur = active ? 9 : 0;
    ctx.shadowColor = substitution ? "#fa84df" : typeEdge ? "#f5cf75" : "#8e8cff";
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  for (const node of graph.nodes) {
    const matched = isSearchMatch(node);
    const focused = !focusIds || focusIds.has(node.id);
    const active = matched && focused;
    const isRecipe = node.kind === "recipe";
    const isType = node.kind === "ingredient_type";
    const radius = isRecipe ? 5.2 : isType ? 4.4 : 3.2;
    const color = isRecipe ? "#e1a1ff" : isType ? "#ffe291" : "#a7eaff";
    ctx.beginPath();
    const glow = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, radius * (active ? 5.5 : 2.5));
    glow.addColorStop(0, active ? (isRecipe ? "rgba(220,146,255,.43)" : isType ? "rgba(255,216,119,.38)" : "rgba(118,210,255,.37)") : "rgba(0,0,0,0)");
    glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow;
    ctx.arc(node.x, node.y, radius * (active ? 5.5 : 2.5), 0, Math.PI * 2);
    ctx.fill();

    ctx.beginPath();
    if (isType) ctx.rect(node.x - radius, node.y - radius, radius * 2, radius * 2);
    else ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
    ctx.fillStyle = active ? color : isRecipe ? "rgba(225,161,255,.27)" : isType ? "rgba(255,226,145,.27)" : "rgba(167,234,255,.25)";
    ctx.shadowColor = color;
    ctx.shadowBlur = active ? (isRecipe ? 18 : 13) : 2;
    ctx.fill();
    ctx.shadowBlur = 0;
    if (node.id === selectedId || node.id === hoveredId || (searchText && matched)) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius + 3, 0, Math.PI * 2);
      ctx.strokeStyle = node.id === selectedId ? "rgba(255,255,255,.9)" : "rgba(208,192,255,.7)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "left";
      ctx.fillStyle = "rgba(246,242,255,.9)";
      ctx.shadowColor = "#9691ff";
      ctx.shadowBlur = 8;
      ctx.fillText(node.name, node.x + 10, node.y - 8);
      ctx.shadowBlur = 0;
    }
  }
  ctx.restore();
}

function canvasPoint(event) {
  const rect = graphCanvas.getBoundingClientRect();
  return {x: event.clientX - rect.left, y: event.clientY - rect.top};
}

function worldPoint(event) {
  const point = canvasPoint(event);
  return {x: (point.x - width / 2 - panX) / scale + width / 2,
    y: (point.y - height / 2 - panY) / scale + height / 2};
}

function hitTest(event) {
  const point = worldPoint(event);
  let closest = null;
  let closestDistance = 14 / scale;
  for (const node of graph.nodes) {
    const distance = Math.hypot(node.x - point.x, node.y - point.y);
    if (distance < closestDistance) {
      closest = node;
      closestDistance = distance;
    }
  }
  return closest;
}

function setZoom(next) {
  scale = Math.max(.55, Math.min(2.4, next));
  drawGraph();
}

function showInspector(node) {
  selectedId = node.id;
  document.querySelector("#inspector-title").textContent = node.name;
  document.querySelector("#inspector-description").textContent = node.description || "No additional description is stored for this graph record.";
  const type = document.querySelector("#inspector-type");
  type.hidden = false;
  type.textContent = node.kind === "recipe" ? "Recipe record" : node.kind === "ingredient_type" ? "Ingredient type" : "Ingredient record";
  const source = document.querySelector("#inspector-source");
  const metadata = [];
  if (node.fdc_id) metadata.push(`USDA FoodData Central · FDC ${node.fdc_id}`);
  if (node.ingredient_type) metadata.push(`type: ${node.ingredient_type}`);
  if (node.substitution_group) metadata.push(`substitution group: ${node.substitution_group.replaceAll("_", " ")}`);
  source.hidden = metadata.length === 0;
  source.textContent = metadata.join(" · ");
  const rows = [];
  for (const edge of graph.edges) {
    let neighborId = null;
    let relation = edge.relation.replaceAll("_", " ");
    if (edge.source === node.id) neighborId = edge.target;
    else if (edge.target === node.id) {
      neighborId = edge.source;
      if (edge.relation === "contains") relation = "contained in";
    }
    if (!neighborId) continue;
    const other = nodeById.get(neighborId);
    if (!other) continue;
    const measure = edge.amount_g != null ? ` · ${edge.amount_g} g` : "";
    const score = edge.compatibility != null ? ` · ${Math.round(edge.compatibility * 100)}% fit` : "";
    rows.push({name: other.name, relation: `${relation}${measure}${score}`, kind: other.kind});
  }
  document.querySelector("#neighbor-count").textContent = String(rows.length);
  const list = document.querySelector("#neighbor-list");
  list.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "empty-neighbors";
    empty.textContent = "No linked records are stored for this node.";
    list.append(empty);
  } else {
    for (const row of rows) {
      const item = document.createElement("div");
      item.className = "neighbor-item";
      const name = document.createElement("span");
      name.className = "neighbor-name";
      name.textContent = row.name;
      const relation = document.createElement("span");
      relation.className = "neighbor-relation";
      relation.textContent = row.relation;
      item.append(name, relation);
      list.append(item);
    }
  }
  drawGraph();
}

function hideInspector() {
  selectedId = null;
  document.querySelector("#inspector-title").textContent = "Explore the graph";
  document.querySelector("#inspector-description").textContent = "Select a glowing node to see its catalog record and connected ingredients or recipes.";
  document.querySelector("#inspector-type").hidden = true;
  document.querySelector("#inspector-source").hidden = true;
  document.querySelector("#neighbor-count").textContent = "0";
  const list = document.querySelector("#neighbor-list");
  list.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "empty-neighbors";
  empty.textContent = "Select a node to inspect its links.";
  list.append(empty);
  drawGraph();
}

function bindInteractions() {
  graphCanvas.addEventListener("pointerdown", event => {
    const node = hitTest(event);
    if (node) {
      showInspector(node);
      return;
    }
    dragging = true;
    dragStart = {x: event.clientX, y: event.clientY, panX, panY};
    graphCanvas.setPointerCapture(event.pointerId);
  });
  graphCanvas.addEventListener("pointermove", event => {
    if (dragging && dragStart) {
      panX = dragStart.panX + event.clientX - dragStart.x;
      panY = dragStart.panY + event.clientY - dragStart.y;
      drawGraph();
      return;
    }
    const node = hitTest(event);
    hoveredId = node?.id || null;
    graphCanvas.classList.toggle("graph-canvas--pointing", Boolean(node));
    if (node) {
      tooltip.hidden = false;
      const nodeKind = node.kind === "recipe" ? "Recipe" : node.kind === "ingredient_type" ? "Ingredient type" : "Ingredient";
      tooltip.textContent = `${nodeKind} · ${node.name}`;
    } else tooltip.hidden = true;
    drawGraph();
  });
  graphCanvas.addEventListener("pointerup", () => { dragging = false; dragStart = null; });
  graphCanvas.addEventListener("pointerleave", () => { hoveredId = null; tooltip.hidden = true; drawGraph(); });
  graphCanvas.addEventListener("wheel", event => {
    event.preventDefault();
    setZoom(scale * (event.deltaY < 0 ? 1.09 : .92));
  }, {passive: false});
  document.querySelector("#zoom-in").addEventListener("click", () => setZoom(scale * 1.18));
  document.querySelector("#zoom-out").addEventListener("click", () => setZoom(scale / 1.18));
  document.querySelector("#zoom-reset").addEventListener("click", () => { scale = 1; panX = 0; panY = 0; hideInspector(); });
  search.addEventListener("input", () => { searchText = search.value.trim().toLowerCase(); drawGraph(); });
  document.addEventListener("keydown", event => {
    if (event.key === "/" && document.activeElement !== search) {
      event.preventDefault();
      search.focus();
    }
    if (event.key === "Escape") {
      search.value = "";
      searchText = "";
      search.blur();
      hideInspector();
    }
  });
}

async function loadGraph() {
  try {
    const response = await fetch("/api/graph");
    if (!response.ok) throw new Error("Graph data request failed");
    graph = await response.json();
    nodeById = new Map(graph.nodes.map(node => [node.id, node]));
    document.querySelector("#node-count").textContent = String(graph.nodes.length);
    document.querySelector("#edge-count").textContent = String(graph.edges.length);
    document.querySelector("#graph-loading").hidden = true;
    resizeCanvas();
  } catch {
    document.querySelector("#graph-loading").hidden = true;
    document.querySelector("#graph-error").hidden = false;
  }
}

bindInteractions();
resizeCanvas();
new ResizeObserver(resizeCanvas).observe(frame);
loadGraph();
