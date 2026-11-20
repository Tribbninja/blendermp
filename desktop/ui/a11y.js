/* Shared accessibility preferences for the main app and dashboard.
 * Preferences live in localStorage under the same bmp.* namespace as the rest
 * of the UI, so both windows stay in sync.
 */
(() => {
  const KEYS = {
    contrast: "bmp.contrast",
    font: "bmp.fontScale",
    motion: "bmp.reducedMotion",
    sound: "bmp.sound",
    dnd: "bmp.dnd",
  };
  const DEFAULTS = {
    contrast: "normal",
    font: "normal",
    motion: "0",
    sound: "1",
    dnd: "0",
  };

  function read(key) {
    try {
      const value = localStorage.getItem(KEYS[key]);
      return value === null ? DEFAULTS[key] : value;
    } catch {
      return DEFAULTS[key];
    }
  }

  function write(key, value) {
    try {
      localStorage.setItem(KEYS[key], String(value));
    } catch {
      /* localStorage is optional */
    }
  }

  function apply() {
    const root = document.documentElement;
    const contrast = read("contrast") === "high" ? "high" : "normal";
    const font = read("font") === "large" ? "large" : "normal";
    const reduced = read("motion") === "1";
    root.dataset.contrast = contrast;
    root.dataset.font = font;
    root.dataset.motion = reduced ? "reduced" : "full";
    document.dispatchEvent(
      new CustomEvent("bmp-a11y", {
        detail: { contrast, font, reduced, sound: read("sound") === "1", dnd: read("dnd") === "1" },
      }),
    );
  }

  function set(key, value) {
    if (!(key in KEYS)) return;
    write(key, value);
    apply();
  }

  function get(key) {
    return read(key);
  }

  window.BMPA11y = {
    apply,
    set,
    get,
    get contrast() { return read("contrast"); },
    get font() { return read("font"); },
    get reducedMotion() { return read("motion") === "1"; },
    get sound() { return read("sound") === "1"; },
    get dnd() { return read("dnd") === "1"; },
  };

  apply();
  window.addEventListener("storage", (event) => {
    if (event.key && event.key.startsWith("bmp.")) apply();
  });
})();
