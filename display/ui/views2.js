"use strict";
// The second look of the screen (open the page with ?ui=2): the same ScreenUpdate views as views.js, laid out afresh.
//   - every content view is a title and, under it, the whole width for the content; the eyes sit in the top-right corner
//     and nothing is pushed under or around them
//   - one primary action per view; chips only for facts (level, section, shelf), a numbered route only for a sequence
// It takes the helpers of views.js (Views.parts) and replaces Views.render, so the buttons, the quiet-zone fitting and
// the events are exactly the same. Every text goes in with textContent.

(() => {
  const { el, svg, buttons, cover, message, floorPlan, fit } = Views.parts;

  // ---- small line icons (24 x 24, one stroke weight) ----
  const PATHS = {
    level:   ["M12 3l9 5-9 5-9-5 9-5z", "M3 13l9 5 9-5"],
    section: ["M6 3h12v18l-6-4-6 4z"],
    shelf:   ["M3 21h18", "M6 21V9", "M11 21V4", "M16 21V8", "M20 21V11"],
    box:     ["M21 8l-9-5-9 5v8l9 5 9-5z", "M3 8l9 5 9-5", "M12 13v8"],
    walk:    ["M13 4.5a1.5 1.5 0 1 0 0 .01", "M9 21l2-6 3 2v4", "M11 15l1-5-3 1-1 3", "M12 10l3 2 3-1"],
    chevron: ["M9 5l7 7-7 7"],
    ret:     ["M12 3v11", "M8 10l4 4 4-4", "M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"],
    ask:     ["M20 20l-4.2-4.2", "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z"],
    recommend: ["M12 3l2.8 5.9 6.2.8-4.6 4.3 1.2 6.4L12 17.3 6.4 20.4l1.2-6.4L3 9.7l6.2-.8z"],
  };
  function icon(name, cls) {
    const s = svg("svg", { viewBox: "0 0 24 24", class: "ic" + (cls ? " " + cls : ""), fill: "none", stroke: "currentColor",
                           "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
    (PATHS[name] || []).forEach(d => s.append(svg("path", { d })));
    return s;
  }
  function chip(name, text) {
    const c = el("span", "chip");
    c.append(icon(name), el("span", "", text));
    return c;
  }
  function chips(items) {
    const row = el("div", "chips");
    items.filter(Boolean).forEach(c => row.append(c));
    return row;
  }

  // ---- the title ----
  function head(title, cls) {
    const h = el("header", "head");
    if (title) h.append(el("h1", cls || "heading clamp2", title));
    return h;
  }
  // the children of a container arrive one after another: --i is their place in line (see the motion rules of ui2.css)
  function stagger(parent, marked = true) {
    if (marked) parent.dataset.stagger = "";
    [...parent.children].forEach((child, i) => child.style.setProperty("--i", i));
    return parent;
  }
  const calm = () => document.body.classList.contains("still") || matchMedia("(prefers-reduced-motion: reduce)").matches;

  const page = (cls, ...kids) => { const r = el("div", "v2 " + cls); r.append(...kids.filter(Boolean)); return r; };
  const actions = (list, send) => (list || []).length ? buttons(list, send) : null;

  // a cover, or when there is no picture the title set on a plain tile (never a broken image)
  function coverOrTile(url, title) {
    if (url) return cover(url, title);
    const tile = el("div", "cover blank");
    tile.append(el("span", "", title || "Book"));
    return tile;
  }

  // ---- face: just the face, and in a quiet zone what the robot said, as a panel ----
  function face(p) {
    const root = el("div", "view-face");
    const long = (p.caption || "").length > 70;
    document.body.classList.toggle("captioned", !!p.caption);
    document.body.classList.toggle("captioned-long", long);
    if (p.caption) root.append(el("div", "caption" + (long ? " long" : ""), p.caption));
    return root;
  }

  // ---- options: the main choices as one list, the rest as quiet buttons ----
  const CHOICES = {
    return:    { icon: "ret", hint: "Hand in a book to be shelved" },
    ask:       { icon: "ask", hint: "Find out about a book or where it is" },
    recommend: { icon: "recommend", hint: "Get suggestions you will enjoy" },
  };
  function options(p, send) {
    const all = p.options || [];
    const main = all.filter(o => CHOICES[o.id]);
    const rest = all.filter(o => !CHOICES[o.id]);
    const body = el("div", "body");
    if (main.length) {
      const list = el("div", "choice-list");
      main.forEach(o => {
        const c = el("button", "choice");
        const text = el("span", "choice-text");
        text.append(el("span", "choice-name", o.label), el("span", "choice-hint", CHOICES[o.id].hint));
        c.append(icon(CHOICES[o.id].icon, "lead"), text, icon("chevron", "go"));
        c.addEventListener("click", () => send(o.id));
        list.append(c);
      });
      body.append(stagger(list));
      if (rest.length) {
        const row = el("div", "quiet-actions");
        rest.forEach(o => { const b = el("button", "btn ghost", o.label || o.id); b.addEventListener("click", () => send(o.id)); row.append(b); });
        body.append(row);
      }
    } else {
      const grid = el("div", "choices" + (all.length > 4 ? " many" : ""));
      all.forEach(o => { const b = el("button", "btn big", o.label || o.id); b.addEventListener("click", () => send(o.id)); grid.append(b); });
      body.append(grid);
    }
    return page("v2-options" + (main.length ? " home" : ""),
                head(p.prompt, "heading" + ((p.prompt || "").length > 60 ? " long" : "")), body);
  }

  // ---- book_card: the cover large, then where it is, a summary (or the message in a quiet zone), the actions ----
  function bookCard(p, send) {
    const h = head(p.title, "title clamp2");
    if (p.author) h.append(el("div", "author", p.author));
    const info = el("div", "info");
    info.append(chips([
      p.badge ? chip("box", p.badge) : null,
      p.level != null ? chip("level", `Level ${p.level}`) : null,
      p.section ? chip("section", p.section) : null,
      p.shelf ? chip("shelf", `Shelf ${p.shelf}`) : null,
    ]));
    if (p.message) info.append(message(p.message));
    else if (p.summary) info.append(el("p", "summary clamp3", p.summary));
    const act = actions(p.buttons, send);
    if (act) info.append(act);
    const body = el("div", "body");
    body.append(coverOrTile(p.cover_url, p.title), stagger(info));
    return page("v2-card" + (p.message ? " has-message" : ""), h, body);
  }

  // ---- book_list: up to three picks, covers on the page like a shelf; a tap opens that book ----
  function bookList(p, send, show) {
    const items = (p.items || []).slice(0, 3);
    const body = el("div", "body");
    if (!items.length) {
      body.append(message(p.message || "No suggestions right now."));
      return page("v2-list", head(p.heading, "heading clamp2"), body);
    }
    const grid = el("div", "picks n" + items.length);
    items.forEach(item => {
      const c = el("button", "pick");
      const text = el("div", "pick-text");
      text.append(el("div", "item-title clamp2", item.title));
      if (item.author) text.append(el("div", "pick-author clamp1", item.author));
      if (item.reason) text.append(el("div", "item-reason clamp3", item.reason));
      const more = el("span", "pick-more");
      more.append(el("span", "", "Details"), icon("chevron"));
      text.append(more);
      c.append(coverOrTile(item.cover_url, item.title), text);
      c.addEventListener("click", () => {
        send(`book:${item.isbn}`);
        show({ view: "book_card", layout: "split", zone: document.body.dataset.zone,
               payload: { isbn: item.isbn, title: item.title, author: item.author, cover_url: item.cover_url,
                          buttons: [{ id: `guide:${item.isbn}`, label: "Show me the way" },
                                    { id: `similar:${item.isbn}`, label: "Something similar" }] } });
      });
      grid.append(c);
    });
    body.append(stagger(grid));
    if (p.message) body.append(message(p.message));
    return page("v2-list" + (p.message ? " has-message" : ""), head(p.heading, "heading clamp2"), body);
  }

  // ---- directions: the map large, the steps as a numbered route ----
  // The plan arrives in order: sections and landmarks settle in, the route is drawn from where you are to where you are
  // going (its length sets how long that takes), the target answers with a ripple, and an arrow then walks the route again
  // and again. ui2.css has the timing; --i, --draw and --after are set here.
  function animatePlan(box, p) {
    ["section", "section-label", "landmark", "landmark-icon"].forEach(cls =>
      box.querySelectorAll("." + cls).forEach((e, i) => e.style.setProperty("--i", i)));
    const poly = box.querySelector(".path"), pts = p.path || [];
    if (!poly) return;
    poly.setAttribute("pathLength", "1");                       // so the drawing-in can run over 0..1 whatever the length
    let len = 0;
    for (let i = 1; i < pts.length; i++) len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    const draw = Math.min(2.4, Math.max(0.9, 0.5 + len * 0.16));
    box.style.setProperty("--draw", `${draw}s`);
    box.style.setProperty("--after", `${(0.7 + draw).toFixed(2)}s`);
    if (calm()) return;
    const Y = y => p.base_map ? -y : y;
    const lap = Math.min(7, Math.max(2.6, 1.4 + len * 0.45));
    const walker = svg("g", { class: "traveller", opacity: 0 });
    walker.append(svg("circle", { r: 0.42, class: "halo" }), svg("path", { d: "M0.42 0L-0.28 0.3L-0.11 0L-0.28 -0.3Z", class: "arrow" }));
    const along = svg("animateMotion", { dur: `${lap}s`, begin: "indefinite", repeatCount: "indefinite", rotate: "auto",
                                         calcMode: "linear", keyPoints: "0;1;1", keyTimes: "0;0.8;1",
                                         path: "M" + pts.map(q => `${q[0]} ${Y(q[1])}`).join("L") });
    const shown = svg("animate", { attributeName: "opacity", dur: `${lap}s`, begin: "indefinite", repeatCount: "indefinite",
                                   values: "0;1;1;0", keyTimes: "0;0.06;0.8;1" });
    walker.append(along, shown);
    box.append(walker);
    setTimeout(() => {                                          // once the route is drawn (the page may have moved on by then)
      if (!box.isConnected) return;
      try { along.beginElement(); shown.beginElement(); } catch (_) { /* no SMIL: the route simply stays drawn */ }
    }, (0.7 + draw + 0.25) * 1000);
  }

  function directions(p, send) {
    const map = el("div", "map-card");
    const plan = floorPlan(p);
    animatePlan(plan, p);
    map.append(plan);
    const side = el("div", "side");
    if (!(p.steps || []).length) {                    // the section and shelf are in the steps when there are any
      side.append(chips([
        p.kind === "floor" ? null : chip("level", `Level ${p.level}`),
        p.section && p.kind !== "section" ? chip("section", p.section) : null,
        p.shelf ? chip("shelf", `Shelf ${p.shelf}`) : null,
      ]));
    }
    const steps = el("ol", "route");
    (p.steps || []).forEach(s => steps.append(el("li", "", s)));
    side.append(stagger(steps, false));
    if (p.distance_m != null) {
      const d = el("div", "distance");
      d.append(icon("walk"), el("span", "", "About "), el("strong", "", `${p.distance_m} m`));
      side.append(d);
    }
    const act = actions(p.buttons, send);
    if (act) side.append(act);
    const body = el("div", "body");
    body.append(map, stagger(side, false));
    return page("v2-directions" + (p.message ? " has-message" : ""), head(`Directions to ${p.title}`, "title clamp2"),
                body, p.message ? message(p.message) : null);
  }

  // ---- status: returning books ----
  let lastLeft = null;
  function status(p) {
    const body = el("div", "body");
    if (p.destination) body.append(el("div", "destination clamp2", `To ${p.destination}`));
    if (p.detail) body.append(el("div", "detail clamp2", p.detail));
    const left = el("div", "books-left" + (lastLeft !== null && lastLeft !== p.books_left ? " tick" : ""));   // the number changed: it ticks over
    lastLeft = p.books_left;
    left.append(el("strong", "", String(p.books_left)), el("span", "", p.books_left === 1 ? " book left" : " books left"));
    body.append(left);
    return page("v2-status", head(p.heading, "heading clamp2"), body);
  }

  const RENDER = { face, book_card: bookCard, book_list: bookList, directions, options, status };

  // Views.fit only looks at the whole view; a column inside it (the steps beside the map, the text beside the cover) can
  // outgrow its own box without the view overflowing. Anything inside the content that sticks out below it (or past the
  // right edge, or a box whose content is taller than the box, has its long texts shrunk a step at a time, down to a readable
  // floor. A <button> does not report what overflows inside it, so its children are measured instead.
  const LONG_TEXT = ".message, .summary, .item-reason, .item-title, .pick-author, .route li, .detail, .destination, .author";
  function tighten(target) {
    const root = target.firstElementChild;
    const body = root && root.querySelector(".body");
    if (!body) return;
    const inside = [...body.querySelectorAll("*")].filter(e => !e.closest("svg, .cover") || e.tagName === "svg");   // the map and the cover tile draw inside their own box
    const over = () => {
      const box = body.getBoundingClientRect();
      return inside.some(e => {
        const r = e.getBoundingClientRect();
        if (r.height && (r.bottom > box.bottom + 1 || r.right > box.right + 1)) return true;
        // a box whose own content is taller than it (a flex child squeezed to fit): not a <button>, not one that clips on purpose
        // (an inline element has no client box: clientHeight is 0 however much text it holds; a big line may reach a few
        // pixels past its own line box without being cut, so only a third of a line counts)
        if (e.tagName === "BUTTON" || !e.clientHeight) return false;
        const style = getComputedStyle(e);
        return e.scrollHeight > e.clientHeight + Math.max(3, parseFloat(style.fontSize) * 0.33) && style.overflow !== "hidden";
      });
    };
    const floor = 16 * Math.min(window.innerWidth / 1024, window.innerHeight / 600);
    const texts = [...root.querySelectorAll(LONG_TEXT)];
    for (let i = 0; i < 12 && over(); i++) {
      let shrunk = false;
      for (const t of texts) {
        const px = parseFloat(getComputedStyle(t).fontSize);
        if (px > floor) { t.style.fontSize = `${Math.max(floor, px * 0.93)}px`; shrunk = true; }
      }
      if (!shrunk) break;
    }
  }

  Views.render = function render(update, target, send, show) {
    const fn = RENDER[update.view] || face;
    if (update.view !== "status") lastLeft = null;
    target.replaceChildren(fn(update.payload || {}, send, show, update));
    fit(target);
    tighten(target);
  };
})();
