/* Fixed dock layout: Left / Main / Right / Bottom.
 * Panels can only live in those slots. Drag a panel over a slot to preview the
 * swap live, release to commit, and every layout change animates with FLIP.
 */
(() => {
  const TITLES = { session: "Session", viewport: "Viewport", chat: "Chat", diagnostics: "Diagnostics" };
  const SIDES = ["left", "right", "bottom"];
  const DEFAULT_SLOTS = { left: "session", main: "viewport", right: "chat", bottom: null };
  const PREFERRED = {
    session: ["left", "right", "bottom"],
    chat: ["right", "left", "bottom"],
    diagnostics: ["bottom", "right", "left"],
  };
  const LS_KEY = "bmp.slots.v1";
  const DURATION = 240;

  function storeKey() {
    return LS_KEY.startsWith("bmp.") ? LS_KEY.slice(4) : LS_KEY;
  }

  let slots = { ...DEFAULT_SLOTS };
  let sizes = { left: 300, right: 300, bottom: 220 };
  let tiles = {};
  let content = {};
  let zones = {};
  let splitters = {};
  let workspace = null;
  let bank = null;
  let callbacks = {};
  let editMode = false;
  let drag = null;
  let originalSlots = null;
  let hoverZone = null;
  let ghost = null;

  function save() {
    const raw = JSON.stringify({ slots, sizes });
    const shared = window.BMPStore;
    if (shared) {
      shared.set(storeKey(), raw);
      return;
    }
    try {
      localStorage.setItem(LS_KEY, raw);
    } catch { /* ignore */ }
  }

  function load() {
    const shared = window.BMPStore;
    let raw = null;
    try {
      raw = shared ? shared.get(storeKey(), null) : localStorage.getItem(LS_KEY);
    } catch {
      raw = null;
    }
    if (!raw) return false;
    try {
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || !parsed.slots) return false;
      const next = { ...DEFAULT_SLOTS };
      for (const key of ["left", "right", "bottom"]) {
        const value = parsed.slots[key];
        next[key] = TITLES[value] && value !== "viewport" ? value : null;
      }
      next.main = "viewport";
      const used = new Set(["viewport"]);
      for (const key of SIDES) {
        if (next[key] && used.has(next[key])) next[key] = null;
        if (next[key]) used.add(next[key]);
      }
      slots = next;
      for (const key of ["left", "right", "bottom"]) {
        const value = Number(parsed.sizes?.[key]);
        if (Number.isFinite(value)) sizes[key] = Math.max(140, Math.min(560, value));
      }
      return true;
    } catch {
      return false;
    }
  }

  function slotOf(id) {
    for (const key of SIDES) if (slots[key] === id) return key;
    return null;
  }

  function cloneSlots(value) {
    return { left: value.left, main: "viewport", right: value.right, bottom: value.bottom };
  }

  function assignCopy(base, id, zone) {
    const next = cloneSlots(base);
    if (!SIDES.includes(zone) || id === "viewport") return next;
    const current = SIDES.find((key) => next[key] === id) || null;
    if (current === zone) return next;
    const occupant = next[zone];
    next[zone] = id;
    if (current) next[current] = occupant && occupant !== id ? occupant : null;
    return next;
  }

  function makeTile(id) {
    const tile = document.createElement("section");
    tile.className = "tile";
    tile.dataset.tile = id;

    if (id !== "viewport") {
      const bar = document.createElement("div");
      bar.className = "tile-bar";
      const grip = document.createElement("span");
      grip.className = "tile-grip";
      const title = document.createElement("span");
      title.className = "tile-title";
      title.textContent = TITLES[id];
      bar.append(grip, title);

      const close = document.createElement("button");
      close.className = "tile-close";
      close.type = "button";
      close.textContent = "x";
      close.title = `Hide ${TITLES[id]}`;
      close.addEventListener("pointerdown", (event) => event.stopPropagation());
      close.addEventListener("click", () => setTile(id, false));
      bar.append(close);

      bar.addEventListener("pointerdown", (event) => beginDrag(event, id));
      tile.append(bar);
    }

    if (content[id]) tile.append(content[id]);
    return tile;
  }

  function updateVariables() {
    const has = {
      left: Boolean(slots.left),
      right: Boolean(slots.right),
      bottom: Boolean(slots.bottom),
    };
    // In edit mode an empty dock opens into a labelled drop target.
    const empty = editMode ? 80 : 0;
    workspace.style.setProperty("--left-col", `${has.left ? sizes.left : empty}px`);
    workspace.style.setProperty("--left-gap", `${has.left || editMode ? 5 : 0}px`);
    workspace.style.setProperty("--right-col", `${has.right ? sizes.right : empty}px`);
    workspace.style.setProperty("--right-gap", `${has.right || editMode ? 5 : 0}px`);
    workspace.style.setProperty("--bottom-row", `${has.bottom ? sizes.bottom : empty}px`);
    workspace.style.setProperty("--bottom-gap", `${has.bottom || editMode ? 5 : 0}px`);
    for (const key of SIDES) {
      if (splitters[key]) splitters[key].style.display = has[key] ? "" : "none";
      zones[key]?.classList.toggle("zone-empty", !has[key] && editMode);
    }
  }

  function animateLayout() {
    const before = new Map();
    for (const [id, tile] of Object.entries(tiles)) {
      if (tile.isConnected) before.set(id, tile.getBoundingClientRect());
    }

    const placed = new Set(["viewport"]);
    zones.main.append(tiles.viewport);

    for (const key of SIDES) {
      const id = slots[key];
      const zone = zones[key];
      if (id && tiles[id]) {
        zone.append(tiles[id]);
        placed.add(id);
      }
    }
    for (const [id, tile] of Object.entries(tiles)) {
      if (!placed.has(id)) bank.append(tile);
    }

    updateVariables();

    for (const [id, tile] of Object.entries(tiles)) {
      if (!tile.isConnected) continue;
      for (const animation of tile.getAnimations()) animation.cancel();
      const previous = before.get(id);
      const next = tile.getBoundingClientRect();
      if (!previous || previous.width < 2 || previous.height < 2) {
        tile.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 180, easing: "ease-out" });
        continue;
      }
      const dx = previous.left - next.left;
      const dy = previous.top - next.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
      tile.animate(
        [
          { transform: `translate(${dx}px, ${dy}px)` },
          { transform: "none" },
        ],
        { duration: DURATION, easing: "cubic-bezier(.2,.75,.25,1)" },
      );
    }

    refreshRail();
    callbacks.onBounds?.();
  }

  function refreshRail() {
    const panel = callbacks.getPanel?.();
    document.querySelectorAll(".rail-btn").forEach((button) => {
      const view = button.dataset.view;
      if (view === "servers" || view === "settings") {
        button.classList.toggle("active", panel === view);
        return;
      }
      if (view === "session") {
        button.classList.toggle("active", panel === "session" && Boolean(slotOf("session")));
        return;
      }
      button.classList.toggle("active", view === "viewport" || Boolean(slotOf(view)));
    });
  }

  function setTile(id, open) {
    if (id === "viewport") return;
    if (open) {
      if (slotOf(id)) return;
      const preference = PREFERRED[id] || SIDES;
      let target = preference.find((key) => !slots[key]);
      if (!target) target = preference[0];
      slots = assignCopy(slots, id, target);
    } else {
      const current = slotOf(id);
      if (current) slots[current] = null;
    }
    save();
    animateLayout();
  }

  function toggleTile(id) {
    setTile(id, !slotOf(id));
  }

  function resetLayout() {
    slots = { ...DEFAULT_SLOTS };
    sizes = { left: 300, right: 300, bottom: 220 };
    save();
    animateLayout();
  }

  function focusView() {
    if (SIDES.some((key) => slots[key])) {
      for (const key of SIDES) slots[key] = null;
    } else {
      slots.left = "session";
      slots.right = "chat";
    }
    save();
    animateLayout();
  }

  // ------------------------------------------------------------------ edit mode
  function setEditMode(value) {
    editMode = value;
    document.body.classList.toggle("layout-editing", value);
    const hint = document.getElementById("layoutHint");
    if (hint) hint.hidden = !value;
    callbacks.setBlenderVisible?.(!value);
    updateVariables();
    animateLayout();
    refreshRail();
  }

  function bindEdit() {
    const button = document.getElementById("layoutBtn");
    if (button) button.addEventListener("click", () => setEditMode(!editMode));
    const done = document.getElementById("layoutDone");
    if (done) done.addEventListener("click", () => setEditMode(false));
    const reset = document.getElementById("layoutReset");
    if (reset) reset.addEventListener("click", resetLayout);
    const resetHint = document.getElementById("layoutResetHint");
    if (resetHint) resetHint.addEventListener("click", resetLayout);
    window.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (drag) cancelDrag();
      else if (editMode) setEditMode(false);
    });
    window.addEventListener("pointercancel", () => {
      if (drag) cancelDrag();
    });
    window.addEventListener("blur", () => {
      if (drag) cancelDrag();
    });
  }

  // ------------------------------------------------------------------ drag
  function beginDrag(event, id) {
    if (!editMode || event.button !== 0) return;
    event.preventDefault();
    drag = { id };
    originalSlots = cloneSlots(slots);
    hoverZone = null;
    ghost = document.createElement("div");
    ghost.className = "tile-ghost";
    ghost.textContent = TITLES[id];
    document.body.append(ghost);
    document.body.classList.add("dragging-tile");
    moveGhost(event);
    window.addEventListener("pointermove", moveDrag);
    window.addEventListener("pointerup", endDrag);
  }

  function moveGhost(event) {
    if (ghost) ghost.style.transform = `translate(${event.clientX + 14}px, ${event.clientY + 14}px)`;
  }

  function zoneAt(x, y) {
    const element = document.elementFromPoint(x, y);
    const zone = element?.closest?.(".zone");
    if (!zone) return null;
    const key = zone.dataset.zone;
    return SIDES.includes(key) ? key : null;
  }

  function moveDrag(event) {
    if (!drag) return;
    moveGhost(event);
    const zone = zoneAt(event.clientX, event.clientY);
    if (zone === hoverZone) return;
    if (hoverZone) zones[hoverZone].classList.remove("drop-hover");
    hoverZone = zone;
    if (zone) {
      zones[zone].classList.add("drop-hover");
      slots = assignCopy(originalSlots, drag.id, zone);
    } else {
      slots = cloneSlots(originalSlots);
    }
    animateLayout();
  }

  function endDrag() {
    const movedId = hoverZone && drag ? drag.id : null;
    window.removeEventListener("pointermove", moveDrag);
    window.removeEventListener("pointerup", endDrag);
    document.body.classList.remove("dragging-tile");
    if (ghost) ghost.remove();
    ghost = null;
    if (hoverZone) zones[hoverZone].classList.remove("drop-hover");
    hoverZone = null;
    drag = null;
    originalSlots = null;
    save();
    animateLayout();
    if (movedId && tiles[movedId]) {
      tiles[movedId].animate(
        [{ transform: "scale(.992)" }, { transform: "none" }],
        { duration: 160, easing: "cubic-bezier(.2,.75,.25,1)" },
      );
    }
  }

  function cancelDrag() {
    window.removeEventListener("pointermove", moveDrag);
    window.removeEventListener("pointerup", endDrag);
    document.body.classList.remove("dragging-tile");
    if (ghost) ghost.remove();
    ghost = null;
    if (hoverZone) zones[hoverZone].classList.remove("drop-hover");
    hoverZone = null;
    drag = null;
    if (originalSlots) slots = originalSlots;
    originalSlots = null;
    animateLayout();
  }

  // ------------------------------------------------------------------ splitters
  function beginSize(event, key) {
    event.preventDefault();
    const startX = event.clientX;
    const startY = event.clientY;
    const start = sizes[key];
    const move = (moveEvent) => {
      if (key === "left") sizes.left = clamp(start + (moveEvent.clientX - startX), 200, 480);
      else if (key === "right") sizes.right = clamp(start - (moveEvent.clientX - startX), 220, 480);
      else sizes.bottom = clamp(start - (moveEvent.clientY - startY), 120, 520);
      updateVariables();
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

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  // ------------------------------------------------------------------ init
  function init(options) {
    callbacks = options;
    content = options.content || {};
    workspace = document.getElementById("workspace");
    zones = {
      left: document.getElementById("zoneLeft"),
      main: document.getElementById("zoneMain"),
      right: document.getElementById("zoneRight"),
      bottom: document.getElementById("zoneBottom"),
    };
    splitters = {
      left: document.getElementById("splitLeft"),
      right: document.getElementById("splitRight"),
      bottom: document.getElementById("splitBottom"),
    };
    if (zones.left) zones.left.dataset.label = "Left";
    if (zones.right) zones.right.dataset.label = "Right";
    if (zones.bottom) zones.bottom.dataset.label = "Bottom";
    bank = document.createElement("div");
    bank.id = "tileBank";
    bank.hidden = true;
    document.body.append(bank);

    if (!content.diagnostics) {
      const section = document.getElementById("diagnosticsSection");
      if (section) {
        const holder = document.createElement("div");
        holder.className = "diagnostics-content";
        holder.append(section);
        content.diagnostics = holder;
      }
    }

    for (const id of ["session", "viewport", "chat", "diagnostics"]) {
      if (content[id]) tiles[id] = makeTile(id);
    }

    load();
    workspace.classList.add("layout-ready");
    bindRail();
    splitters.left?.addEventListener("pointerdown", (event) => beginSize(event, "left"));
    splitters.right?.addEventListener("pointerdown", (event) => beginSize(event, "right"));
    splitters.bottom?.addEventListener("pointerdown", (event) => beginSize(event, "bottom"));
    // The viewport tile (and the Layout/Reset chips inside it) only enters the
    // document once animateLayout places it, so bind the edit controls after.
    animateLayout();
    bindEdit();
  }

  function bindRail() {
    document.querySelectorAll(".rail-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const view = button.dataset.view;
        if (view === "settings" || view === "servers") {
          setTile("session", true);
          callbacks.setPanel?.(view);
          refreshRail();
          return;
        }
        if (view === "session") {
          if (callbacks.getPanel?.() !== "session") {
            setTile("session", true);
            callbacks.setPanel?.("session");
            refreshRail();
            return;
          }
          toggleTile("session");
          return;
        }
        if (view === "chat" || view === "diagnostics") {
          toggleTile(view);
        }
      });
    });
  }

  window.BMPLayout = {
    init,
    toggleTile,
    setTile,
    resetLayout,
    focusView,
    setEditMode,
    refreshRail,
    isEditing: () => editMode,
  };
})();
