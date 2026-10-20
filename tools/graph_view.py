"""Write one self-contained web page that shows the book graph: open data/graph.html in any browser.

    python tools/graph_view.py                    # data/graph.html
    python tools/graph_view.py --out /tmp/graph.html

Books, authors, topics (and the broader topics above them), call-number classes, shelves, sections and series
are the dots, coloured by type; lines are the graph's edges. Hover over a dot to see what it is (for a book: author, call
number, where it lives, topics, series). Click a book to light up its neighbours and list every connection with
its reason, and the books the robot would recommend next with their scores and reasons. Drag dots to rearrange
them; click the background to clear. The page has no external scripts, styles or fonts and makes no network
requests: the data, the force layout and the drawing code are all inline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.app import build_finder  # noqa: E402
from robot.config import load_config  # noqa: E402
from robot.services.books import CatalogError  # noqa: E402

RECOMMENDATIONS = 5


def node_info(graph, node) -> list:
    """The facts shown when hovering a node: a list of [label, text] pairs."""
    info = [["Type", node.kind]]
    if node.kind == "book":
        isbn = node.id.split(":", 1)[1]
        book = graph.catalog.lookup(isbn)
        place = graph.locate(isbn)
        info += [["Author", book.author or "unknown"], ["ISBN", book.isbn13]]
        if book.year:
            info.append(["Year", str(book.year)])
        if book.call_number:
            info.append(["Call number", book.call_number])
        if place and place.describe():
            info.append(["Location", place.describe() + (f" ({place.zone} zone)" if place.zone else "")])
        if book.series:
            info.append(["Series", book.series + (f", book {book.series_index}" if book.series_index is not None else "")])
        info.append(["Topics", ", ".join(graph.topics_of(isbn)) or "none"])
        if book.summary:
            info.append(["Summary", book.summary])
    elif node.kind == "shelf":
        shelf = graph.shelves.get(node.label) or next((s for s in graph.shelves.values() if s.id.lower() == node.label.lower()), None)
        if shelf is not None:
            info += [["Section", f"{shelf.section}, level {shelf.floor}"], ["Zone", shelf.zone or "unknown"]]
            if shelf.has_pose:
                info.append(["Map pose", f"x {shelf.map_x}, y {shelf.map_y}, yaw {shelf.map_yaw}"])
            if shelf.description:
                info.append(["Note", shelf.description])
    elif node.kind in ("topic", "broader"):
        topic = node.id.split(":", 1)[1]
        info.append(["Books about it", str(graph.topic_counts().get(topic, 0))])
        if graph.vocabulary.broader.get(topic):
            info.append(["Falls under", ", ".join(graph.vocabulary.broader[topic])])
    return info


def build_data(graph) -> dict:
    nodes = [{"id": n.id, "kind": n.kind, "label": n.label, "info": node_info(graph, n)}
             for n in sorted(graph.nodes.values(), key=lambda n: n.id)]
    edges = [{"src": e.src, "dst": e.dst, "kind": e.kind, "relation": e.relation, "note": e.note} for e in graph.edges]
    recs = {}
    for book in graph.catalog.books:
        recs[f"book:{book.isbn13}"] = [
            {"id": f"book:{r.book.isbn13}", "title": r.book.title, "score": round(r.total, 3), "reasons": r.reasons}
            for r in graph.recommend(book.isbn13, RECOMMENDATIONS)]
    return {"nodes": nodes, "edges": edges, "recs": recs}


def render(graph) -> str:
    """The whole page as a string."""
    # Every "<" inside the JSON is inside a string, so escaping them all is safe, and it stops a title such as
    # "</script>" or "<!--<script>" from ending or confusing the script block. U+2028/2029 are escaped for old browsers.
    data = json.dumps(build_data(graph), ensure_ascii=False)
    data = data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", data)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Book graph</title>
<style>
  :root { --bg:#fafaf8; --fg:#1d1d1b; --muted:#6b6b66; --panel:#ffffff; --line:#c9c9c2; --hot:#1d1d1b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#161615; --fg:#ecece8; --muted:#9a9a93; --panel:#1f1f1e; --line:#4a4a46; --hot:#ecece8; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; display:flex; height:100vh; }
  #stage { flex:1; min-width:0; position:relative; }
  svg { width:100%; height:100%; display:block; cursor:default; }
  aside { width:340px; max-width:42vw; overflow:auto; background:var(--panel); border-left:1px solid var(--line); padding:16px; }
  h1 { font-size:16px; margin:0 0 4px; } h2 { font-size:13px; margin:16px 0 6px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  .hint { color:var(--muted); margin:0 0 12px; }
  .legend span { display:inline-flex; align-items:center; margin:0 10px 4px 0; }
  .legend i { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:5px; }
  dl { margin:0; } dt { color:var(--muted); font-size:12px; margin-top:6px; } dd { margin:0; }
  ul { margin:0; padding-left:18px; } li { margin:0 0 6px; }
  .score { font-variant-numeric: tabular-nums; color:var(--muted); }
  .edge { stroke:var(--line); stroke-width:1; stroke-opacity:.75; } .edge.curated { stroke-dasharray:4 3; stroke-width:1.5; }
  .node { cursor:pointer; stroke:var(--bg); stroke-width:1.5; }
  .label { font-size:13px; fill:var(--fg); pointer-events:none; paint-order:stroke; stroke:var(--bg); stroke-width:3px; }
  .dim { opacity:.2; } .hot.edge { stroke:var(--hot); stroke-width:2.2; }
  @media (max-width: 700px) { body { flex-direction:column; } aside { width:auto; max-width:none; height:45vh; border-left:0; border-top:1px solid var(--line); } }
</style>
</head>
<body>
<div id="stage"><svg id="svg" viewBox="0 0 1400 950" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Book graph"></svg></div>
<aside>
  <h1>Book graph</h1>
  <p class="hint">Hover a dot for details. Click a book to see its connections and what the robot would recommend next. Drag to rearrange.</p>
  <div class="legend" id="legend"></div>
  <div id="panel"></div>
</aside>
<script>
const DATA = __DATA__;
const COLORS = {book:"#4e79a7", author:"#f28e2b", topic:"#59a14f", broader:"#9bc78a", class:"#b07aa1", shelf:"#e15759", section:"#ff9da7", series:"#d4b020"};
const NS = "http://www.w3.org/2000/svg";
const W = 1400, H = 950;
const byId = new Map(DATA.nodes.map(n => [n.id, n]));
const edges = DATA.edges.filter(e => byId.has(e.src) && byId.has(e.dst));
const incident = new Map(DATA.nodes.map(n => [n.id, []]));
edges.forEach(e => { incident.get(e.src).push(e); incident.get(e.dst).push(e); });

// ---- force layout (Fruchterman-Reingold), deterministic start
let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
DATA.nodes.forEach((n, i) => { const a = 2 * Math.PI * i / DATA.nodes.length; n.x = W/2 + 300 * Math.cos(a) + rnd() * 20; n.y = H/2 + 220 * Math.sin(a) + rnd() * 20; n.dx = 0; n.dy = 0; });
function layout(iterations) {
  const n = DATA.nodes.length, k = 0.95 * Math.sqrt(W * H / Math.max(n, 1));
  const dir = () => { const a = rnd() * 2 * Math.PI; return [Math.cos(a), Math.sin(a)]; };
  for (let it = 0; it < iterations; it++) {
    const t = 60 * (1 - it / iterations) + 1;
    DATA.nodes.forEach(v => { v.dx = 0; v.dy = 0; });
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = DATA.nodes[i], b = DATA.nodes[j]; let dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy);
      if (d < 0.01) { [dx, dy] = dir(); d = 1; }
      if (d > 3 * k) continue;                                                       // far apart: no repulsion
      const f = k * k / d; dx = dx / d * f; dy = dy / d * f; a.dx += dx; a.dy += dy; b.dx -= dx; b.dy -= dy;
    }
    edges.forEach(e => {
      const a = byId.get(e.src), b = byId.get(e.dst); let dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy) || 0.01;
      const f = d * d / k; dx = dx / d * f; dy = dy / d * f; a.dx -= dx; a.dy -= dy; b.dx += dx; b.dy += dy;
    });
    DATA.nodes.forEach(v => {
      v.dx += (W/2 - v.x) * 0.06; v.dy += (H/2 - v.y) * 0.06;                       // pull towards the middle
      const d = Math.hypot(v.dx, v.dy) || 0.01, m = Math.min(d, t);
      v.x = Math.min(W - 30, Math.max(30, v.x + v.dx / d * m)); v.y = Math.min(H - 30, Math.max(30, v.y + v.dy / d * m));
    });
  }
  for (let pass = 0; pass < 60; pass++) {                                           // finally, never leave two dots overlapping
    let moved = false;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = DATA.nodes[i], b = DATA.nodes[j]; let dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy);
      if (d >= 34) continue;
      if (d < 0.01) { [dx, dy] = dir(); d = 1; }
      const push = (34 - d) / 2 + 0.5; a.x += dx / d * push; a.y += dy / d * push; b.x -= dx / d * push; b.y -= dy / d * push; moved = true;
    }
    DATA.nodes.forEach(v => { v.x = Math.min(W - 20, Math.max(20, v.x)); v.y = Math.min(H - 20, Math.max(20, v.y)); });
    if (!moved) break;
  }
}
layout(300);

// ---- drawing
const svg = document.getElementById("svg");
const mk = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const edgeEls = edges.map(e => { const l = mk("line", {class: "edge" + (e.kind === "curated" ? " curated" : "")}); svg.appendChild(l); return l; });
const nodeEls = new Map(), labelEls = new Map();
const alwaysLabelled = n => n.kind === "book" || n.kind === "topic" || n.kind === "broader";
const radius = n => n.kind === "book" ? 10 : 7;
DATA.nodes.forEach(n => {
  const c = mk("circle", {class: "node", r: radius(n), fill: COLORS[n.kind] || "#888"});
  const title = mk("title", {}); title.textContent = n.label + " (" + n.kind + ")"; c.appendChild(title);
  svg.appendChild(c); nodeEls.set(n.id, c);
  const t = mk("text", {class: "label", "text-anchor": "middle"}); t.textContent = n.label;
  t.style.display = alwaysLabelled(n) ? "" : "none"; svg.appendChild(t); labelEls.set(n.id, t);
});
// Put each visible label where it overlaps nothing already placed: above, below, right, then left of its dot;
// if every spot collides, the one with the least overlap. Books are placed first so they get the best spots.
const CHAR_W = 7.2, LABEL_H = 15;
function overlap(a, b) { const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x), h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y); return w > 0 && h > 0 ? w * h : 0; }
function placeLabels() {
  const dots = DATA.nodes.map(n => ({id: n.id, x: n.x - radius(n), y: n.y - radius(n), w: 2 * radius(n), h: 2 * radius(n)}));
  const placed = [], order = DATA.nodes.filter(n => labelEls.get(n.id).style.display !== "none").sort((a, b) => (a.kind !== "book") - (b.kind !== "book"));
  order.forEach(n => {
    const t = labelEls.get(n.id), w = n.label.length * CHAR_W, h = LABEL_H, r = radius(n);
    const spots = [{ax: "middle", x: n.x, y: n.y - r - 5, box: {x: n.x - w / 2, y: n.y - r - 5 - h + 3}},
                   {ax: "middle", x: n.x, y: n.y + r + h - 1, box: {x: n.x - w / 2, y: n.y + r + 2}},
                   {ax: "start", x: n.x + r + 5, y: n.y + 4, box: {x: n.x + r + 5, y: n.y - h / 2}},
                   {ax: "end", x: n.x - r - 5, y: n.y + 4, box: {x: n.x - r - 5 - w, y: n.y - h / 2}}];
    let best = null, bestCost = Infinity;
    for (const s of spots) {
      const box = {x: s.box.x, y: s.box.y, w, h};
      let cost = placed.reduce((c, p) => c + overlap(box, p), 0) + dots.reduce((c, d) => c + (d.id === n.id ? 0 : overlap(box, d)), 0);
      if (box.x < 4 || box.y < 4 || box.x + w > W - 4 || box.y + h > H - 4) cost += 5000;       // keep it on the canvas
      if (cost < bestCost) { best = {s, box}; bestCost = cost; }
      if (cost === 0) break;
    }
    t.setAttribute("x", best.s.x); t.setAttribute("y", best.s.y); t.setAttribute("text-anchor", best.s.ax); placed.push(best.box);
  });
}
function place() {
  edges.forEach((e, i) => { const a = byId.get(e.src), b = byId.get(e.dst); const l = edgeEls[i]; l.setAttribute("x1", a.x); l.setAttribute("y1", a.y); l.setAttribute("x2", b.x); l.setAttribute("y2", b.y); });
  DATA.nodes.forEach(n => { const c = nodeEls.get(n.id); c.setAttribute("cx", n.x); c.setAttribute("cy", n.y); });
  placeLabels();
}
place();

// ---- panel (built with textContent only, so no title can inject markup)
const panel = document.getElementById("panel");
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const legend = document.getElementById("legend");
Object.keys(COLORS).forEach(k => { const s = el("span"); const i = el("i"); i.style.background = COLORS[k]; s.appendChild(i); s.appendChild(document.createTextNode(k)); legend.appendChild(s); });
const describe = e => e.kind + (e.relation ? " (" + e.relation + ")" : "");
function show(n, selected) {
  panel.textContent = "";
  panel.appendChild(el("h2", n.kind));
  panel.appendChild(el("strong", n.label));
  const dl = el("dl"); n.info.slice(1).forEach(([k, v]) => { dl.appendChild(el("dt", k)); dl.appendChild(el("dd", v)); }); panel.appendChild(dl);
  if (!selected) return;
  panel.appendChild(el("h2", "Connections"));
  const ul = el("ul");
  incident.get(n.id).forEach(e => {
    const other = byId.get(e.src === n.id ? e.dst : e.src), arrow = e.src === n.id ? "→" : "←";
    ul.appendChild(el("li", describe(e) + " " + arrow + " " + other.label + (e.note ? " — " + e.note : "")));
  });
  panel.appendChild(ul);
  const recs = DATA.recs[n.id];
  if (recs) {
    panel.appendChild(el("h2", "Recommended next"));
    if (!recs.length) panel.appendChild(el("p", "Nothing scores high enough to recommend.", "hint"));
    const rl = el("ul");
    recs.forEach(r => { const li = el("li"); li.appendChild(el("strong", r.title)); li.appendChild(el("span", "  " + r.score.toFixed(2), "score")); li.appendChild(el("div", r.reasons.join("; "), "hint")); rl.appendChild(li); });
    panel.appendChild(rl);
  }
}

// ---- hover, click, drag
let selected = null;
function highlight() {
  const keep = new Set(); if (selected) { keep.add(selected); incident.get(selected).forEach(e => { keep.add(e.src); keep.add(e.dst); }); }
  DATA.nodes.forEach(n => {
    const on = !selected || keep.has(n.id); nodeEls.get(n.id).classList.toggle("dim", !on); labelEls.get(n.id).classList.toggle("dim", !on);
    if (selected && keep.has(n.id)) labelEls.get(n.id).style.display = "";
    else labelEls.get(n.id).style.display = alwaysLabelled(n) ? "" : "none";
  });
  placeLabels();
  edges.forEach((e, i) => { const hot = selected && (e.src === selected || e.dst === selected); edgeEls[i].classList.toggle("hot", !!hot); edgeEls[i].classList.toggle("dim", !!selected && !hot); });
}
function select(id) { selected = id; highlight(); if (id) show(byId.get(id), true); else panel.textContent = ""; }
let drag = null, moved = 0;
DATA.nodes.forEach(n => {
  const c = nodeEls.get(n.id);
  c.addEventListener("mouseenter", () => { if (!selected) show(n, false); });
  c.addEventListener("mousedown", ev => { drag = n; moved = 0; ev.preventDefault(); ev.stopPropagation(); });
});
function toSvg(ev) { const p = svg.createSVGPoint(); p.x = ev.clientX; p.y = ev.clientY; return p.matrixTransform(svg.getScreenCTM().inverse()); }
window.addEventListener("mousemove", ev => { if (!drag) return; const p = toSvg(ev); moved += Math.abs(p.x - drag.x) + Math.abs(p.y - drag.y); drag.x = p.x; drag.y = p.y; place(); });
window.addEventListener("mouseup", () => { if (drag && moved < 4) select(selected === drag.id ? null : drag.id); drag = null; });
svg.addEventListener("mousedown", () => { if (!drag) select(null); });
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path, default=cfg.path("data/graph.html"), help="the page to write (default data/graph.html)")
    args = p.parse_args(argv)
    try:
        graph = build_finder(cfg).graph
    except CatalogError as exc:
        print(f"Cannot load the book data: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(graph), encoding="utf-8")
    print(f"Wrote {args.out} ({len(graph.nodes)} nodes, {len(graph.edges)} edges). Open it in a browser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
