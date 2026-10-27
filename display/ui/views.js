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

  // ---- options: the main choices as cards (icon, name, one line of help), the rest as quiet buttons
  const CARDS = {
    return:    { hint: "Hand in a book to be shelved",
                 icon: ["M12 3v11", "M8 10l4 4 4-4", "M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"] },
    ask:       { hint: "Find out about a book or where it is", icon: ["M20 20l-4.2-4.2", "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z"] },
    recommend: { hint: "Get suggestions you will enjoy", icon: ["M12 3l2.8 5.9 6.2.8-4.6 4.3 1.2 6.4L12 17.3 6.4 20.4l1.2-6.4L3 9.7l6.2-.8z"] },
  };
  function icon(paths) {
    const s = svg("svg", { viewBox: "0 0 24 24", class: "card-icon", fill: "none", stroke: "currentColor", "stroke-width": "1.7",
                           "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
    paths.forEach(d => s.append(svg("path", { d })));
    return s;
  }
  function options(p, send) {
    const root = el("div", "view-options");
    const all = p.options || [];
    const cards = all.filter(o => CARDS[o.id]);
    const rest = all.filter(o => !CARDS[o.id]);
    if (p.prompt || cards.length) {
      const head = el("div", "opt-head");
      if (cards.length) head.append(el("div", "eyebrow", "Stuart \u00b7 Library Book Robot"));
      if (p.prompt) head.append(el("h1", "heading clamp2", p.prompt));
      root.append(head);
    }
    if (cards.length) {
      const row = el("div", "opt-cards n" + cards.length);
      cards.forEach(o => {
        const c = el("button", "opt-card");
        c.append(icon(CARDS[o.id].icon), el("span", "card-name", o.label), el("span", "card-hint", CARDS[o.id].hint));
        c.addEventListener("click", () => send(o.id));
        row.append(c);
      });
      root.append(row);
      if (rest.length) {
        const extra = el("div", "opt-extra");
        rest.forEach(o => extra.append(button(o, send, false)));
        root.append(extra);
      }
      return root;
    }
    const grid = el("div", "option-grid" + (all.length > 4 ? " many" : ""));
    all.forEach(o => grid.append(button(o, send, false)));
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
