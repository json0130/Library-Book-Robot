"use strict";
// The robot's content views, one function per view, rendered from a ScreenUpdate's payload (robot/core/screen.py):
//   face, book_card, book_list, directions, options, status
// Plain DOM, no libraries, works offline. Every text goes in with textContent (never as HTML). A button POSTs its
// id to /input, which the robot receives as a UserChoice (book:<isbn>, guide:<isbn>, similar:<isbn>, section:<name>...).

const Views = (() => {
  const SVG = "http://www.w3.org/2000/svg";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function svg(tag, attrs, text) {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function button(b, send, primary) {
    const e = el("button", "btn" + (primary ? " primary" : ""), b.label || b.id);
    e.addEventListener("click", () => send(b.id));
    return e;
  }
  function buttons(list, send) {
    const row = el("div", "buttons");
    (list || []).forEach((b, i) => row.append(button(b, send, i === 0)));
    return row;
  }
  function cover(url, title) {
    const img = el("img", "cover");
    img.alt = title ? `Cover of ${title}` : "Book cover";
    img.src = url;
    return img;
  }
  function badge(text) { return el("span", "badge", text); }

  // ---- face: the face fills the screen; in a quiet zone with a caption of what the robot said
  function face(p) {
    const root = el("div", "view-face");
    document.body.classList.toggle("captioned", !!p.caption);       // the face moves up to make room
    if (p.caption) root.append(el("div", "caption", p.caption));
    return root;
  }

  // ---- book_card: cover, title, author, two-line summary, where it lives, two buttons
  function bookCard(p, send) {
    const root = el("div", "view-card");
    root.append(cover(p.cover_url, p.title));
    const info = el("div", "card-info");
    info.append(el("h1", "title clamp2", p.title));
    if (p.author) info.append(el("div", "author", p.author));
    if (p.summary) info.append(el("p", "summary clamp2", p.summary));
    const where = el("div", "badges");
    if (p.badge) where.append(badge(p.badge));
    if (p.level != null) where.append(badge(`Level ${p.level}`));
    if (p.section) where.append(badge(p.section));
    if (p.shelf) where.append(badge(`Shelf ${p.shelf}`));
    if (where.childElementCount) info.append(where);
    if ((p.buttons || []).length) info.append(buttons(p.buttons, send));
    root.append(info);
    return root;
  }

  // ---- book_list: up to three covers, a title and one short reason each; a tap opens that book's card
  function bookList(p, send, show) {
    const root = el("div", "view-list");
    root.append(el("h1", "heading clamp1", p.heading));
    const row = el("div", "list-row");
    (p.items || []).slice(0, 3).forEach(item => {
      const card = el("button", "list-item");
      card.append(cover(item.cover_url, item.title), el("div", "item-title clamp2", item.title));
      if (item.reason) card.append(el("div", "item-reason clamp2", item.reason));
      card.addEventListener("click", () => {
        send(`book:${item.isbn}`);                     // the robot makes it the topic and shows its full card...
        show({ view: "book_card", layout: "split", zone: document.body.dataset.zone,   // ...meanwhile show this much
               payload: { isbn: item.isbn, title: item.title, author: item.author, cover_url: item.cover_url,
                          buttons: [{ id: `guide:${item.isbn}`, label: "Show me the way" },
                                    { id: `similar:${item.isbn}`, label: "Something similar" }] } });
      });
      row.append(card);
    });
    root.append(row);
    return root;
  }

  // ---- directions: the floor plan as SVG, the target highlighted, an approximate path, the steps
  const ICON = { lift: "L", help_desk: "?", entrance: "E", stairs: "S", toilets: "WC", other: "•" };

  function floorPlan(p) {
    // show the part of the floor that matters (sections, landmarks, the path, the robot), zoomed in, with a margin
    const xs = [], ys = [];
    for (const s of p.sections || []) { xs.push(s.x, s.x + s.w); ys.push(s.y, s.y + s.h); }
    for (const q of [...(p.landmarks || []), ...(p.robot ? [p.robot] : []), ...(p.target ? [p.target] : [])]) { xs.push(q.x); ys.push(q.y); }
    for (const q of p.path || []) { xs.push(q[0]); ys.push(q[1]); }
    const pad = 0.8;
    const x0 = Math.max(0, Math.min(...xs) - pad), y0 = Math.max(0, Math.min(...ys) - pad - 0.4);
    const x1 = Math.min(p.width, Math.max(...xs) + pad), y1 = Math.min(p.height, Math.max(...ys) + pad);
    const box = svg("svg", { viewBox: `${x0 - 0.1} ${y0 - 0.1} ${x1 - x0 + 0.2} ${y1 - y0 + 0.2}`, class: "plan",
                             role: "img", "aria-label": `Plan of ${p.floor_name}` });
    box.append(svg("rect", { x: 0, y: 0, width: p.width, height: p.height, rx: 0.2, class: "floor" }));
    for (const s of p.sections || []) {
      box.append(svg("rect", { x: s.x, y: s.y, width: s.w, height: s.h, rx: 0.12,
                               class: "section" + (s.highlight ? " hi" : "") }));
      const tall = s.h > s.w * 1.4;                    // a long narrow section: write its name along it
      const room = tall ? s.h : s.w;
      const size = Math.max(0.3, Math.min(0.6, room / Math.max(4, s.name.length) * 1.7, (tall ? s.w : s.h) * 0.45));
      const x = tall ? s.x + s.w - size * 0.8 : s.x + s.w / 2, y = tall ? s.y + s.h / 2 : s.y + size * 1.05;
      box.append(svg("text", { x, y, "font-size": size, class: "section-label" + (s.highlight ? " hi" : ""),
                               "text-anchor": "middle", "dominant-baseline": "middle",
                               transform: tall ? `rotate(-90 ${x} ${y})` : "" }, s.name));
    }
    if ((p.path || []).length > 1) {                    // APPROXIMATE: straight or L-shaped, not a planned route
      box.append(svg("polyline", { points: p.path.map(q => q.join(",")).join(" "), class: "path" }));
    }
    for (const m of p.landmarks || []) {
      box.append(svg("rect", { x: m.x - 0.35, y: m.y - 0.35, width: 0.7, height: 0.7, rx: 0.12, class: `landmark ${m.kind}` }));
      box.append(svg("text", { x: m.x, y: m.y + 0.02, "font-size": 0.42, class: "landmark-icon", "text-anchor": "middle",
                               "dominant-baseline": "middle" }, ICON[m.kind] || "•"));
    }
    if (p.target) {
      box.append(svg("circle", { cx: p.target.x, cy: p.target.y, r: 0.38, class: "target-ring" }));
      box.append(svg("circle", { cx: p.target.x, cy: p.target.y, r: 0.18, class: "target" }));
    }
    if (p.robot) {
      box.append(svg("circle", { cx: p.robot.x, cy: p.robot.y, r: 0.32, class: "robot" }));
      box.append(svg("text", { x: p.robot.x, y: p.robot.y - 0.6, "font-size": 0.45, class: "robot-label",
                               "text-anchor": "middle" }, "You are here"));
    } else if (p.start) {
      box.append(svg("text", { x: p.start.x, y: p.start.y - 0.6, "font-size": 0.45, class: "robot-label",
                               "text-anchor": "middle" }, p.start.label));
    }
    return box;
  }

  function directionsView(p, send) {
    const root = el("div", "view-directions");
    const map = el("div", "map");
    map.append(floorPlan(p));
    root.append(map);
    const side = el("div", "side");
    side.append(el("h1", "title clamp2", p.title.charAt(0).toUpperCase() + p.title.slice(1)));
    const where = el("div", "badges");
    where.append(badge(`Level ${p.level}`));
    if (p.section && p.kind !== "section") where.append(badge(p.section));
    if (p.shelf) where.append(badge(`Shelf ${p.shelf}`));
    side.append(where);
    const steps = el("ol", "steps");
    (p.steps || []).forEach(s => steps.append(el("li", "", s)));
    side.append(steps);
    if (p.distance_m != null) side.append(el("div", "distance", `About ${p.distance_m} m`));
    if ((p.buttons || []).length) side.append(buttons(p.buttons, send));
    root.append(side);
    return root;
  }

  // ---- options: the current choices as big buttons
  function options(p, send) {
    const root = el("div", "view-options");
    if (p.prompt) root.append(el("h1", "heading clamp2", p.prompt));
    const grid = el("div", "option-grid" + ((p.options || []).length > 4 ? " many" : ""));
    (p.options || []).forEach(o => grid.append(button(o, send, false)));
    root.append(grid);
    return root;
  }

  // ---- status: returning books
  function status(p) {
    const root = el("div", "view-status");
    root.append(el("h1", "heading", p.heading));
    if (p.destination) root.append(el("div", "destination clamp2", `To ${p.destination}`));
    const left = el("div", "books-left");
    left.append(el("span", "count", String(p.books_left)), el("span", "", p.books_left === 1 ? " book left" : " books left"));
    root.append(left);
    if (p.detail) root.append(el("div", "detail clamp2", p.detail));
    return root;
  }

  const RENDER = { face, book_card: bookCard, book_list: bookList, directions: directionsView, options, status };

  function render(update, target, send, show) {
    const fn = RENDER[update.view] || face;
    target.replaceChildren(fn(update.payload || {}, send, show));
  }

  return { render, names: Object.keys(RENDER) };
})();
