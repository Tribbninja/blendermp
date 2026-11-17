/* Dockable tile layout: drag tiles, snap to edges, split or swap, resize splits. */
(() => {
  const TITLES = { session: "Session", viewport: "Viewport", chat: "Chat", diagnostics: "Diagnostics" };
  const LS_KEY = "bmp.layout.v3";
  const EDGE = 0.26;

  const DEFAULT_TREE = {
    type: "split",
    dir: "row",
    ratio: 0.19,
    a: { type: "leaf", id: "session" },
    b: {
      type: "split",
      dir: "row",
      ratio: 0.75,
      a: { type: "leaf", id: "viewport" },
      b: { type: "leaf", id: "chat" },
    },
  };

  let tree = null;
  let content = {};
  let app = null;
  let workspace = null;
  let bank = null;
  let callbacks = {};
  let editMode = false;
  let drag = null;
  let ghost = null;
  let preview = null;
  let drop = null;

  const leaf = (id) => ({ type: "leaf", id });
  const split = (dir, ratio, a, b) => ({ type: "split", dir, ratio, a, b });
  const clone = (value) => JSON.parse(JSON.stringify(value));

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function save() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(tree)); } catch { /* ignore */ }
  }

  function load() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return valid(parsed) ? parsed : null;
    } catch { return null; }
  }

  function valid(node) {
    if (!node || typeof node !== "object") return false;
    if (node.type === "leaf") return typeof node.id === "string" && Boolean(TITLES[node.id]);
    if (node.type !== "split") return false;
    if (node.dir !== "row" && node.dir !== "col") return false;
    if (!Number.isFinite(node.ratio) || node.ratio < 0.05 || node.ratio > 0.95) return false;
    return valid(node.a) && valid(node.b);
  }

  function hasLeaf(node, id) {
    if (!node) return false;
    if (node.type === "leaf") return node.id === id;
    return hasLeaf(node.a, id) || hasLeaf(node.b, id);
  }

  function findLeaf(node, id) {
    if (!node) return null;
    if (node.type === "leaf") return node.id === id ? node : null;
    return findLeaf(node.a, id) || findLeaf(node.b, id);
  }

  function removeLeaf(node, id) {
    if (!node) return null;
    if (node.type === "leaf") return node.id === id ? null : node;
    const a = removeLeaf(node.a, id);
    const b = removeLeaf(node.b, id);
    if (!a) return b;
    if (!b) return a;
    node.a = a;
    node.b = b;
    return node;
  }

  function replaceLeaf(node, id, replacement) {
    if (!node) return null;
    if (node.type === "leaf") return node.id === id ? replacement : node;
    node.a = replaceLeaf(node.a, id, replacement);
    node.b = replaceLeaf(node.b, id, replacement);
    return node;
  }

  function swapLeaves(node, first, second) {
    if (!node) return;
    if (node.type === "leaf") {
      if (node.id === first) node.id = second;
      else if (node.id === second) node.id = first;
      return;
    }
    swapLeaves(node.a, first, second);
    swapLeaves(node.b, first, second);
  }

  function wrapRoot(side, id, ratio = 0.2) {
    const dir = side === "left" || side === "right" ? "row" : "col";
    if (side === "left" || side === "top") tree = split(dir, ratio, leaf(id), tree);
    else tree = split(dir, 1 - ratio, tree, leaf(id));
  }

  function ensureViewport() {
    if (!hasLeaf(tree, "viewport")) {
      tree = split("row", 0.7, tree || leaf("chat"), leaf("viewport"));
    }
  }

  // ------------------------------------------------------------------ render
  function makeTile(id) {
    const tile = document.createElement("section");
    tile.className = "tile";
    tile.dataset.tile = id;

    const bar = document.createElement("div");
    bar.className = "tile-bar";
    const grip = document.createElement("span");
    grip.className = "tile-grip";
    const title = document.createElement("span");
    title.className = "tile-title";
    title.textContent = TITLES[id] || id;
    bar.append(grip, title);

    if (id !== "viewport") {
      const close = document.createElement("button");
      close.className = "tile-close";
      close.type = "button";
      close.textContent = "x";
      close.title = `Close ${TITLES[id]}`;
      close.addEventListener("pointerdown", (event) => event.stopPropagation());
      close.addEventListener("click", () => toggleTile(id));
      bar.append(close);
    }
    bar.addEventListener("pointerdown", (event) => beginDrag(event, id));
    tile.append(bar);

    const body = content[id];
    if (body) tile.append(body);
    return tile;
  }

  function build(node) {
    if (node.type === "leaf") return makeTile(node.id);

    const element = document.createElement("div");
    element.className = `split ${node.dir}`;
    element.style.display = "flex";
    element.style.flexDirection = node.dir === "row" ? "row" : "column";
    element.style.minWidth = "0";
    element.style.minHeight = "0";
    element.style.flex = "1 1 0";

    const first = build(node.a);
    const second = build(node.b);
    first.style.flex = `${node.ratio} 1 0`;
    second.style.flex = `${1 - node.ratio} 1 0`;

    const bar = document.createElement("div");
    bar.className = "split-bar";
    bar.addEventListener("pointerdown", (event) => beginRatio(event, node, first, second, element));
    bar.addEventListener("dblclick", () => {
      node.ratio = 0.5;
      first.style.flex = "0.5 1 0";
      second.style.flex = "0.5 1 0";
      save();
      callbacks.onBounds?.();
    });

    element.append(first, bar, second);
    return element;
  }

  function render() {
    if (!bank) {
      bank = document.createElement("div");
      bank.id = "tileBank";
      bank.hidden = true;
      document.body.append(bank);
    }
    for (const element of Object.values(content)) {
      if (element) bank.append(element);
    }
    workspace.replaceChildren();
    workspace.append(build(tree));
    refreshRail();
    callbacks.onBounds?.();
  }

  function refreshRail() {
    document.querySelectorAll(".rail-btn").forEach((button) => {
      const view = button.dataset.view;
      if (view === "settings") {
        button.classList.toggle("active", callbacks.getPanel?.() === "settings");
        return;
      }
      button.classList.toggle("active", hasLeaf(tree, view));
    });
  }

  // ------------------------------------------------------------------ splitters
  function beginRatio(event, node, first, second, element) {
    event.preventDefault();
    const rect = element.getBoundingClientRect();
    const move = (moveEvent) => {
      const value = node.dir === "row"
        ? (moveEvent.clientX - rect.left) / rect.width
        : (moveEvent.clientY - rect.top) / rect.height;
      node.ratio = clamp(value, 0.12, 0.88);
      first.style.flex = `${node.ratio} 1 0`;
      second.style.flex = `${1 - node.ratio} 1 0`;
      callbacks.onBounds?.();
    };
    const up = () => {
      document.body.classList.remove("resizing");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      save();
      callbacks.onBounds?.();
    };
    document.body.classList.add("resizing");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  // ------------------------------------------------------------------ tiles
  function toggleTile(id) {
    if (id === "viewport") return;
    setTile(id, !hasLeaf(tree, id));
  }

  function setTile(id, open) {
    if (id === "viewport") return;
    if (open) {
      ensureTile(id);
    } else {
      const next = removeLeaf(tree, id);
      if (next) tree = next;
    }
    ensureViewport();
    save();
    render();
  }

  function ensureTile(id) {
    if (hasLeaf(tree, id) || id === "viewport") return;
    if (id === "session") wrapRoot("left", id, 0.19);
    else if (id === "chat") wrapRoot("right", id, 0.22);
    else if (id === "diagnostics") {
      tree = replaceLeaf(tree, "viewport", split("col", 0.7, leaf("viewport"), leaf("diagnostics")));
    }
  }

  function focusView() {
    if (hasLeaf(tree, "session") || hasLeaf(tree, "chat") || hasLeaf(tree, "diagnostics")) {
      for (const id of ["session", "chat", "diagnostics"]) {
        const next = removeLeaf(tree, id);
        if (next) tree = next;
      }
    } else {
      ensureTile("session");
      ensureTile("chat");
    }
    ensureViewport();
    save();
    render();
  }

  // ------------------------------------------------------------------ edit mode
  function setEditMode(value) {
    editMode = value;
    document.body.classList.toggle("layout-editing", value);
    if (app) app.classList.toggle("layout-editing", value);
    const hint = document.getElementById("layoutHint");
    if (hint) hint.hidden = !value;
    callbacks.setBlenderVisible?.(!value);
    if (!value) callbacks.onBounds?.();
    refreshRail();
  }

  function bindEdit() {
    const button = document.getElementById("layoutBtn");
    if (button) button.addEventListener("click", () => setEditMode(!editMode));
    const done = document.getElementById("layoutDone");
    if (done) done.addEventListener("click", () => setEditMode(false));
    window.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (drag) cancelDrag();
      else if (editMode) setEditMode(false);
    });
  }

  // ------------------------------------------------------------------ drag
  function beginDrag(event, id) {
    if (!editMode || event.button !== 0) return;
    if (id === "viewport" && !event.shiftKey) {
      // The viewport can be moved too, but keep the layout stable if the user
      // just clicks its bar.
    }
    event.preventDefault();
    drag = { id, targetId: null, side: null, rootSide: null };
    ghost = document.createElement("div");
    ghost.className = "tile-ghost";
    ghost.textContent = TITLES[id] || id;
    document.body.append(ghost);
    preview = document.createElement("div");
    preview.className = "drop-preview";
    preview.hidden = true;
    document.body.append(preview);
    document.body.classList.add("dragging-tile");
    moveGhost(event);
    window.addEventListener("pointermove", moveDrag);
    window.addEventListener("pointerup", endDrag);
  }

  function moveGhost(event) {
    if (!ghost) return;
    ghost.style.transform = `translate(${event.clientX + 14}px, ${event.clientY + 14}px)`;
  }

  function moveDrag(event) {
    if (!drag) return;
    moveGhost(event);
    drop = null;
    const element = document.elementFromPoint(event.clientX, event.clientY);
    const tile = element?.closest?.(".tile");
    if (tile && tile.dataset.tile !== drag.id) {
      const rect = tile.getBoundingClientRect();
      const rx = (event.clientX - rect.left) / rect.width;
      const ry = (event.clientY - rect.top) / rect.height;
      let side = "center";
      if (rx < EDGE) side = "left";
      else if (rx > 1 - EDGE) side = "right";
      else if (ry < EDGE) side = "top";
      else if (ry > 1 - EDGE) side = "bottom";
      drop = { targetId: tile.dataset.tile, side };
      const box = { ...rect };
      if (side === "left") box.width = rect.width / 2;
      else if (side === "right") { box.x = rect.left + rect.width / 2; box.width = rect.width / 2; }
      else if (side === "top") box.height = rect.height / 2;
      else if (side === "bottom") { box.y = rect.top + rect.height / 2; box.height = rect.height / 2; }
      showPreview(box);
      return;
    }
    const bounds = workspace.getBoundingClientRect();
    const inside = event.clientX >= bounds.left && event.clientX <= bounds.right &&
      event.clientY >= bounds.top && event.clientY <= bounds.bottom;
    if (inside) {
      const distances = {
        left: event.clientX - bounds.left,
        right: bounds.right - event.clientX,
        top: event.clientY - bounds.top,
        bottom: bounds.bottom - event.clientY,
      };
      const side = Object.keys(distances).reduce((best, key) => distances[key] < distances[best] ? key : best, "left");
      if (distances[side] < Math.min(bounds.width, bounds.height) * 0.18) {
        drop = { targetId: null, side: null, rootSide: side };
        const box = { ...bounds };
        if (side === "left") box.width = bounds.width * 0.22;
        else if (side === "right") { box.x = bounds.right - bounds.width * 0.22; box.width = bounds.width * 0.22; }
        else if (side === "top") box.height = bounds.height * 0.22;
        else { box.y = bounds.bottom - bounds.height * 0.22; box.height = bounds.height * 0.22; }
        showPreview(box);
        return;
      }
    }
    if (preview) preview.hidden = true;
  }

  function showPreview(rect) {
    if (!preview) return;
    preview.hidden = false;
    preview.style.left = `${rect.left ?? rect.x}px`;
    preview.style.top = `${rect.top ?? rect.y}px`;
    preview.style.width = `${rect.width}px`;
    preview.style.height = `${rect.height}px`;
  }

  function endDrag() {
    window.removeEventListener("pointermove", moveDrag);
    window.removeEventListener("pointerup", endDrag);
    document.body.classList.remove("dragging-tile");
    if (ghost) ghost.remove();
    if (preview) preview.remove();
    ghost = null;
    preview = null;
    document.body.classList.remove("resizing");

    if (!drag || !drop) {
      drag = null;
      drop = null;
      return;
    }
    applyDrop(drag.id, drop);
    drag = null;
    drop = null;
  }

  function cancelDrag() {
    window.removeEventListener("pointermove", moveDrag);
    window.removeEventListener("pointerup", endDrag);
    document.body.classList.remove("dragging-tile");
    if (ghost) ghost.remove();
    if (preview) preview.remove();
    ghost = null;
    preview = null;
    drag = null;
    drop = null;
  }

  function applyDrop(id, target) {
    if (target.rootSide) {
      const next = removeLeaf(tree, id);
      if (!next) return;
      tree = next;
      wrapRoot(target.rootSide, id, 0.22);
    } else if (target.targetId) {
      if (target.side === "center") {
        if (target.targetId === id) return;
        swapLeaves(tree, id, target.targetId);
      } else {
        const next = removeLeaf(tree, id);
        if (!next || !hasLeaf(next, target.targetId)) return;
        tree = next;
        const dir = target.side === "left" || target.side === "right" ? "row" : "col";
        const first = target.side === "left" || target.side === "top" ? id : target.targetId;
        const second = first === id ? target.targetId : id;
        tree = replaceLeaf(tree, target.targetId, split(dir, 0.5, leaf(first), leaf(second)));
      }
    } else {
      return;
    }
    ensureViewport();
    save();
    render();
    setEditMode(true);
  }

  // ------------------------------------------------------------------ init
  function init(options) {
    content = options.content || {};
    callbacks = options;
    app = document.getElementById("app");
    workspace = document.getElementById("workspace");
    if (!workspace) return;

    // Diagnostics lives in the session panel in the markup; detach it into a
    // tile of its own.
    if (!content.diagnostics) {
      const section = document.getElementById("diagnosticsSection");
      if (section) {
        const holder = document.createElement("div");
        holder.className = "diagnostics-content";
        holder.append(section);
        content.diagnostics = holder;
      }
    }

    tree = load() || clone(DEFAULT_TREE);
    ensureViewport();
    bindEdit();
    render();
  }

  window.BMPLayout = {
    init,
    render,
    toggleTile,
    setTile,
    ensureTile,
    setEditMode,
    focusView,
    refreshRail,
    isEditing: () => editMode,
  };
})();
