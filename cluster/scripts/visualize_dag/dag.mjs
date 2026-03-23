import * as d3 from "d3";
import { graphStratify, sugiyama, shapeEllipse, tweakShape } from "d3-dag";

const DATA = window.GRAPH_DATA;
const NODE_R = 7;

// Convert edge list to d3-dag parentIds format.
// Edge convention: source depends on target → target is parent.
const parentMap = new Map();
DATA.nodes.forEach((n) => parentMap.set(n.id, []));
DATA.links.forEach((l) => {
  if (parentMap.has(l.source)) parentMap.get(l.source).push(l.target);
});
const dagData = DATA.nodes.map((n) => ({
  id: n.id,
  parentIds: parentMap.get(n.id) || [],
}));

// Build DAG and run Sugiyama layout
const dag = graphStratify()(dagData);
const nodeW = 180;
const nodeH = 28;
const layout = sugiyama()
  .nodeSize([nodeW, nodeH])
  .gap([12, 8])
  .tweaks([tweakShape([nodeW, nodeH], shapeEllipse)]);
const { width, height } = layout(dag);

// Build adjacency maps for interactive highlight
const depsOf = new Map();
const rdepsOf = new Map();
DATA.nodes.forEach((n) => {
  depsOf.set(n.id, new Set());
  rdepsOf.set(n.id, new Set());
});
DATA.links.forEach((l) => {
  depsOf.get(l.source).add(l.target);
  rdepsOf.get(l.target).add(l.source);
});

function transitive(startId, adjMap) {
  const visited = new Set();
  const stack = [startId];
  while (stack.length) {
    const cur = stack.pop();
    if (visited.has(cur)) continue;
    visited.add(cur);
    for (const nb of adjMap.get(cur) || []) stack.push(nb);
  }
  return visited;
}

// SVG setup
const W = window.innerWidth;
const H = window.innerHeight;
const svg = d3.select("#dag").attr("width", W).attr("height", H);
const root = svg.append("g");

// Fit the DAG into the viewport
const pad = 40;
const scale = Math.min((W - 2 * pad) / width, (H - 2 * pad) / height);
const tx = (W - width * scale) / 2;
const ty = (H - height * scale) / 2;
const zoomBehavior = d3
  .zoom()
  .scaleExtent([0.05, 4])
  .on("zoom", (e) => root.attr("transform", e.transform));
svg.call(zoomBehavior);
svg.call(
  zoomBehavior.transform,
  d3.zoomIdentity.translate(tx, ty).scale(scale),
);

// Arrow markers
const defs = root.append("defs");
function makeArrow(id, color) {
  defs
    .append("marker")
    .attr("id", id)
    .attr("viewBox", "0 -4 8 8")
    .attr("refX", 8)
    .attr("refY", 0)
    .attr("markerWidth", 5)
    .attr("markerHeight", 5)
    .attr("orient", "auto")
    .append("path")
    .attr("fill", color)
    .attr("d", "M0,-4L8,0L0,4Z");
}
makeArrow("arrow", "#555");
makeArrow("arrow-hl", "#ff6b6b");

// Render edges as curved paths
const line = d3.line().curve(d3.curveCatmullRom);
const linkG = root.append("g");
const links = dag.links();
const linkSel = linkG
  .selectAll("path")
  .data(links)
  .join("path")
  .attr("class", "link")
  .attr("marker-end", "url(#arrow)")
  .attr("d", (d) => line(d.points));

// Render nodes
const nodeG = root.append("g");
const nodes = [...dag.nodes()];
const nodeSel = nodeG
  .selectAll("g")
  .data(nodes)
  .join("g")
  .attr("class", "node")
  .attr("transform", (d) => `translate(${d.x},${d.y})`);
nodeSel.append("circle").attr("r", NODE_R);
nodeSel
  .append("text")
  .text((d) => d.data.id)
  .attr("dx", NODE_R + 4)
  .attr("dy", 3);

// Tooltip
const tooltip = d3.select("#tooltip");
nodeSel
  .on("mouseover", (_event, d) => {
    const id = d.data.id;
    const deps = [...(depsOf.get(id) || [])].sort();
    const rdeps = [...(rdepsOf.get(id) || [])].sort();
    let html = "<h3>" + id + "</h3>";
    if (deps.length) {
      html +=
        '<span class="label">Depends on (' + deps.length + "):</span><ul>";
      deps.forEach((x) => (html += "<li>" + x + "</li>"));
      html += "</ul>";
    }
    if (rdeps.length) {
      html +=
        '<span class="label">Depended on by (' +
        rdeps.length +
        "):</span><ul>";
      rdeps.forEach((x) => (html += "<li>" + x + "</li>"));
      html += "</ul>";
    }
    tooltip.html(html).style("display", "block");
  })
  .on("mousemove", (event) => {
    tooltip
      .style("left", event.clientX + 16 + "px")
      .style("top", event.clientY - 10 + "px");
  })
  .on("mouseout", () => tooltip.style("display", "none"));

// Click to highlight transitive dependency chain
let selectedId = null;

function linkId(l) {
  const s = l.source.data ? l.source.data.id : l.source;
  const t = l.target.data ? l.target.data.id : l.target;
  return [s, t];
}

function clearHighlight() {
  selectedId = null;
  nodeSel.select("circle").attr("class", "");
  nodeSel.select("text").attr("class", "");
  linkSel.attr("class", "link").attr("marker-end", "url(#arrow)");
}

nodeSel.on("click", (event, d) => {
  event.stopPropagation();
  const id = d.data.id;
  if (selectedId === id) {
    clearHighlight();
    return;
  }
  selectedId = id;
  const ancestors = transitive(id, depsOf);
  const descendants = transitive(id, rdepsOf);
  const related = new Set([...ancestors, ...descendants]);

  nodeSel.select("circle").attr("class", (n) => {
    const nid = n.data.id;
    if (nid === id) return "selected";
    return related.has(nid) ? "highlighted" : "dimmed";
  });
  nodeSel
    .select("text")
    .attr("class", (n) => (related.has(n.data.id) ? "" : "dimmed"));
  linkSel
    .attr("class", (l) => {
      const [s, t] = linkId(l);
      if (ancestors.has(s) && ancestors.has(t)) return "link highlighted";
      if (descendants.has(s) && descendants.has(t)) return "link highlighted";
      return "link dimmed";
    })
    .attr("marker-end", (l) => {
      const [s, t] = linkId(l);
      if (
        (ancestors.has(s) && ancestors.has(t)) ||
        (descendants.has(s) && descendants.has(t))
      )
        return "url(#arrow-hl)";
      return "url(#arrow)";
    });
});

svg.on("click", clearHighlight);

// Remove loading indicator
document.getElementById("loading").remove();
