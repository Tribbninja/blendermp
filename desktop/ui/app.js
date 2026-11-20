/* Blendermp desktop shell.
 *
 * No framework and no bundler: the webview loads this file directly, which keeps
 * the attack surface small (strict CSP, no inline script) and the install tiny.
 * Every string that comes from the addon is rendered with textContent, never
 * innerHTML.
 */

(() => {
const $ = (id) => document.getElementById(id);
const app = $("app");
const tauriInternals = window.__TAURI_INTERNALS__;
const tauriGlobal = window.__TAURI__;
const isTauri = Boolean((tauriInternals && tauriInternals.invoke) || (tauriGlobal && tauriGlobal.core));
const demoMode = new URLSearchParams(location.search).get("demo") === "1";
const dashboardMode = Boolean(window.__BMP_DASHBOARD__) || new URLSearchParams(location.search).get("dashboard") === "1";
if (dashboardMode) {
  // The dashboard window loads index.html with an initialization flag. Let
  // dashboard.js own that window instead of starting the main app twice.
  return;
}

async function invoke(command, args = {}) {
  if (tauriInternals && tauriInternals.invoke) return tauriInternals.invoke(command, args);
  if (tauriGlobal && tauriGlobal.core) return tauriGlobal.core.invoke(command, args);
  return mockInvoke(command, args);
}

function log(message) {
  if (!isTauri) return;
  try {
    invoke("log_client", { message: String(message) }).catch(() => {});
  } catch {
    /* logging must never break the UI */
  }
}

log(`boot tauri=${isTauri} internals=${Boolean(tauriInternals)} global=${Boolean(tauriGlobal)}`);
try { fetch("http://127.0.0.1:8090/beacon?tauri=" + isTauri).catch(() => {}); } catch {}

// Visible bridge state, also handy in screenshots and bug reports.
function markBridge() {
  const pill = $("bridgePill");
  if (!pill) return;
  pill.textContent = isTauri ? "bridge: app" : "bridge: browser";
  pill.classList.toggle("mock", !isTauri);
  document.title = isTauri ? "Blendermp" : "Blendermp (preview)";
}

const store = (() => {
  const memory = new Map();
  let persistTimer = 0;
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key && key.startsWith("bmp.")) {
        memory.set(key.slice(4), localStorage.getItem(key) || "");
      }
    }
  } catch {
    /* localStorage is optional (privacy mode, tests) */
  }
  function localSet(key, value) {
    try {
      localStorage.setItem(`bmp.${key}`, value);
    } catch {
      /* ignore */
    }
  }
  function persist() {
    clearTimeout(persistTimer);
    persistTimer = 0;
    if (!isTauri || !store.loaded) return;
    invoke("write_settings", { settings: store.all() }).catch((error) => {
      log(`settings write failed: ${error}`);
    });
  }
  function schedule() {
    if (!store.loaded) return;
    clearTimeout(persistTimer);
    persistTimer = setTimeout(persist, 150);
  }
  return {
    loaded: false,
    get(key, fallback) {
      if (memory.has(key)) return memory.get(key);
      let value = null;
      try {
        value = localStorage.getItem(`bmp.${key}`);
      } catch {
        value = null;
      }
      if (value !== null) {
        memory.set(key, value);
        return value;
      }
      return fallback;
    },
    set(key, value) {
      const text = String(value);
      memory.set(key, text);
      localSet(key, text);
      schedule();
    },
    all() {
      return Object.fromEntries(memory);
    },
    hydrate(values) {
      if (!values || typeof values !== "object" || Array.isArray(values)) return;
      for (const [key, value] of Object.entries(values)) {
        if (typeof key !== "string" || !key || key.length > 64) continue;
        if (typeof value !== "string" || value.length > 8192) continue;
        memory.set(key, value);
        localSet(key, value);
      }
    },
    markLoaded() {
      store.loaded = true;
    },
    flush: persist,
  };
})();

window.BMPStore = store;

async function loadSettings() {
  if (!isTauri) {
    store.markLoaded();
    return;
  }
  let saved = null;
  try {
    saved = await invoke("read_settings");
    if (saved) store.hydrate(saved);
  } catch (error) {
    log(`settings read failed: ${error}`);
  }
  store.markLoaded();
  if (!saved) store.flush();
}

const DEFAULT_HUB = "https://blendermp-hub.tribbeisaac.workers.dev";
const APP_VERSION = "0.9.0";
const PROTOCOL_VERSION = 3;
const CAPABILITIES = 1 | (1 << 10) | (1 << 11) | (1 << 12); // presence, bookmarks, bundles, chunks
const settings = {
  name: "",
  port: 51999,
  pin: "",
  serverName: "",
  public: true,
  directory: DEFAULT_HUB,
  workerRelay: true,
  relay: "",
  useRelay: false,
  blender: "",
  hostKey: "",
};

function applyStoredSettings() {
  const fallbackName = `user-${Math.random().toString(36).slice(2, 6)}`;
  const storedDirectory = store.get("directory", DEFAULT_HUB);
  // Drop the localhost values used while developing the Worker hub; a deployment
  // URL (or a deliberately configured hub) is kept.
  const directory = /^https?:\/\/(127\.0\.0\.1|localhost):(7101|8787)\/?$/.test(storedDirectory)
    ? DEFAULT_HUB
    : storedDirectory;
  settings.name = String(store.get("name", fallbackName) || fallbackName);
  settings.port = Math.max(1024, Math.min(65535, Number(store.get("port", 51999)) || 51999));
  settings.pin = String(store.get("pin", ""));
  settings.serverName = String(store.get("serverName", ""));
  settings.public = store.get("public", "1") === "1";
  settings.directory = directory;
  settings.workerRelay = store.get("workerRelay", "1") === "1";
  settings.relay = String(store.get("relay", ""));
  settings.useRelay = store.get("useRelay", "0") === "1";
  settings.blender = String(store.get("blender", ""));
  settings.hostKey = String(store.get("hostKey", ""));
  if (!settings.hostKey) settings.hostKey = randomHex(32);
  // Materialise every key so the durable settings file is complete and a
  // reinstall cannot silently drop the host key or the public toggle.
  for (const [key, value] of Object.entries({
    name: settings.name,
    port: settings.port,
    pin: settings.pin,
    serverName: settings.serverName,
    public: settings.public ? "1" : "0",
    directory: settings.directory,
    workerRelay: settings.workerRelay ? "1" : "0",
    relay: settings.relay,
    useRelay: settings.useRelay ? "1" : "0",
    blender: settings.blender,
    hostKey: settings.hostKey,
  })) {
    store.set(key, value);
  }
}

function randomHex(bytes) {
  const data = new Uint8Array(bytes);
  if (window.crypto?.getRandomValues) {
    window.crypto.getRandomValues(data);
  } else {
    for (let index = 0; index < data.length; index += 1) {
      data[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(data, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const state = {
  status: null,
  embedded: false,
  dockEnabled: true,
  embedError: "",
  blenderHidden: false,
  verifyAt: 0,
  verifyTries: 0,
  verifyBlack: 0,
  panel: "session",
  latency: [],
  lastBytes: null,
  lastStamp: 0,
  rates: { down: 0, up: 0 },
  messagesKey: "",
  blenderBusy: false,
  lastError: "",
  addonWarned: false,
  servers: [],
  pendingServer: null,
  serverQuery: "",
  serverId: "",
  joinedServerName: "",
  localDirectory: "",
  lanDirectory: "",
  discovered: [],
  announceTargets: [],
  lastAnnounce: 0,
  directoryError: "",
  wasHosting: false,
  offlineStreak: 0,
  demo: demoMode,
  boundsKey: "",
};

let serverSearchTimer = 0;

/* ------------------------------------------------------------------ tauri */
const demoStatus = {
  state: "connected",
  code: "K7M2Q-X9ABC-DEFGH",
  verify_code: "A1B2 C3D4 E5F6 7890",
  is_host: true,
  name: "isaac",
  rtt_ms: 24,
  bytes_in: 184320,
  bytes_out: 92160,
  blender: "5.3.0",
  addon: APP_VERSION,
  users: [
    { name: "isaac", color: [0.37, 0.91, 0.83, 1], ping_ms: 0, host: true },
    { name: "maya", color: [0.55, 0.63, 0.98, 1], ping_ms: 24, host: false },
    { name: "rowan", color: [0.98, 0.72, 0.42, 1], ping_ms: 41, host: false },
  ],
  messages: [
    { author: "maya", text: "the hand retopo is on the right, take a look", stamp: "14:02" },
    { author: "rowan", text: "pinging the ear seam - one sec", stamp: "14:03" },
    { author: "isaac", text: "sending the updated blockout now", stamp: "14:04" },
  ],
};

async function mockInvoke(command) {
  switch (command) {
    case "detect_blender":
      return "C:\\Program Files\\Blender Foundation\\Blender 5.3\\blender.exe";
    case "read_status":
      return state.demo ? demoStatus : null;
    case "read_settings":
      return null;
    case "write_settings":
      return null;
    case "export_diagnostics":
      return "C:\\Users\\you\\AppData\\Local\\Blendermp\\diagnostics\\blendermp-diagnostics-demo.zip";
    case "launch_blender":
      return 4242;
    case "find_blender_window":
      return null;
    case "write_session":
      return null;
    case "embed_blender":
      throw new Error("embedding is only available in the desktop app");
    case "set_embed_bounds":
    case "detach_blender":
    case "focus_blender":
      return null;
    default:
      return null;
  }
}

/* ------------------------------------------------------------------ helpers */
function toast(message, kind = "") {
  const host = $("toasts");
  const item = document.createElement("div");
  item.className = `toast ${kind}`.trim();
  item.textContent = message;
  host.prepend(item);
  while (host.children.length > 3) host.lastElementChild.remove();
  setTimeout(() => item.remove(), 4200);
}

let notificationAudio = null;
function notificationBeep() {
  if (!window.BMPA11y?.sound || window.BMPA11y?.dnd) return;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    notificationAudio = notificationAudio || new AudioCtx();
    const oscillator = notificationAudio.createOscillator();
    const gain = notificationAudio.createGain();
    oscillator.frequency.value = 740;
    gain.gain.value = 0.03;
    oscillator.connect(gain);
    gain.connect(notificationAudio.destination);
    oscillator.start();
    oscillator.stop(notificationAudio.currentTime + 0.09);
  } catch {
    /* sound is optional */
  }
}

function initials(name) {
  return (name || "?").slice(0, 2).toUpperCase();
}

function colorOf(user) {
  const color = Array.isArray(user.color) && user.color.length >= 3 ? user.color : [0.4, 0.8, 1];
  const channel = (value) => Math.max(0, Math.min(255, Math.round(Number(value) * 255) || 0));
  return `rgb(${channel(color[0])}, ${channel(color[1])}, ${channel(color[2])})`;
}

function isActive(status) {
  return Boolean(status && ["hosting", "joining", "connected"].includes(status.state));
}

function directoryBase() {
  const value = String(settings.directory || "").trim();
  return value ? value.replace(/\/+$/, "") : "";
}

function hubRelayUrl() {
  if (!settings.workerRelay) return "";
  const base = directoryBase();
  if (!base) return "";
  if (base.startsWith("https://")) return "wss://" + base.slice(8);
  if (base.startsWith("http://")) return "ws://" + base.slice(7);
  return "";
}

function makeServerId() {
  return randomHex(16);
}

function serverDisplayName() {
  return settings.serverName.trim() || `${settings.name}'s server`;
}

function versionParts(value) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(value || "").trim());
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : [0, 0, 0];
}

function compareVersions(left, right) {
  const a = versionParts(left);
  const b = versionParts(right);
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
  }
  return 0;
}

function majorMinor(value) {
  const parts = versionParts(value);
  return `${parts[0]}.${parts[1]}`;
}

function serverCompatibility(server) {
  if (!server) return { ok: false, reason: "No server selected." };
  const protocol = Number(server.protocol || 0);
  if (protocol && protocol !== PROTOCOL_VERSION) {
    return {
      ok: false,
      reason: `Host uses protocol ${protocol}; update Blendermp to ${APP_VERSION}.`,
    };
  }
  const minApp = String(server.min_app || "").trim();
  if (minApp && compareVersions(APP_VERSION, minApp) < 0) {
    return { ok: false, reason: `Host requires Blendermp ${minApp} or newer.` };
  }
  const hostVersion = String(server.version || "").trim();
  if (hostVersion) {
    const exact = APP_VERSION.startsWith("0.");
    const mismatch = exact
      ? compareVersions(hostVersion, APP_VERSION) !== 0
      : majorMinor(hostVersion) !== majorMinor(APP_VERSION);
    if (mismatch) {
      return {
        ok: false,
        reason: `Host is on ${hostVersion}; you are on ${APP_VERSION}. Both PCs need the same build.`,
      };
    }
  }
  return { ok: true, reason: "" };
}

/* ------------------------------------------------------------ deep links */
function inviteLink(code) {
  const clean = String(code || "").replace(/[^0-9A-Za-z]/g, "").toUpperCase();
  return clean.length === 15 ? `blender://join/${clean}` : "";
}

function parseInviteLink(value) {
  const raw = String(value || "").trim();
  if (!/^(blender|blendermp):\/\//i.test(raw)) return null;
  let url;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  const host = String(url.hostname || "").trim();
  const path = String(url.pathname || "").replace(/^\/+/, "");
  let code = "";
  if (host.toLowerCase() === "join" || host.toLowerCase() === "host") {
    code = path || url.searchParams.get("code") || "";
  } else if (host) {
    code = host + path;
  } else {
    code = path || url.searchParams.get("code") || "";
  }
  code = code
    .replace(/[^0-9A-Za-z]/g, "")
    .toUpperCase()
    .replace(/[IL]/g, "1")
    .replace(/O/g, "0");
  if (code.length !== 15) return null;
  const pin = url.searchParams.get("pin") || url.searchParams.get("password") || "";
  const relay = url.searchParams.get("relay") || url.searchParams.get("relay_url") || "";
  return {
    code,
    pin: pin.slice(0, 64),
    relayUrl: /^wss?:\/\//i.test(relay) ? relay.slice(0, 256) : "",
  };
}

async function handleInviteLink(value) {
  const invite = parseInviteLink(value);
  if (!invite) {
    toast("That invite link is not valid", "error");
    return;
  }
  if (isActive(state.status)) {
    const current = String(state.status?.code || "").replace(/[^0-9A-Z]/g, "");
    if (current === invite.code) {
      toast("You are already in this room", "ok");
      return;
    }
    if (!window.confirm("Join this room? It replaces the current Blender scene.")) return;
  }
  await joinServer(
    {
      code: invite.code,
      name: `Invite ${invite.code.slice(0, 5)}`,
      host: "invite link",
      password: Boolean(invite.pin),
      relay_url: invite.relayUrl,
      lan: false,
    },
    invite.pin,
  );
}

async function pollDeepLink() {
  try {
    const pending = await invoke("take_deep_link");
    if (pending && pending.link) await handleInviteLink(pending.link);
  } catch {
    /* optional while the app is starting */
  }
}

/* ------------------------------------------------------- recent rooms */
const HISTORY_KEY = "roomHistory";

function loadRoomHistory() {
  try {
    const parsed = JSON.parse(store.get(HISTORY_KEY, "[]"));
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item) => item && typeof item.code === "string" && item.code.length >= 15)
      .slice(0, 16)
      .map((item) => ({
        code: String(item.code).replace(/[^0-9A-Za-z]/g, "").toUpperCase(),
        name: String(item.name || "Room").slice(0, 48),
        host: String(item.host || "").slice(0, 48),
        password: Boolean(item.password),
        favorite: Boolean(item.favorite),
        relay_url: String(item.relay_url || "").slice(0, 256),
        direct_code: String(item.direct_code || "").slice(0, 32),
        direct_codes: Array.isArray(item.direct_codes)
          ? item.direct_codes.filter((value) => typeof value === "string").slice(0, 4)
          : [],
        lan: Boolean(item.lan),
        stamp: Number(item.stamp) || 0,
      }));
  } catch {
    return [];
  }
}

function saveRoomHistory(entries) {
  store.set(HISTORY_KEY, JSON.stringify(entries.slice(0, 16)));
}

function rememberRoom(server) {
  const code = String(server?.code || "").replace(/[^0-9A-Za-z]/g, "").toUpperCase();
  if (code.length !== 15) return;
  const entries = loadRoomHistory().filter((item) => item.code !== code);
  entries.unshift({
    code,
    name: String(server.name || "Room").slice(0, 48),
    host: String(server.host || settings.name).slice(0, 48),
    password: Boolean(server.password),
    favorite: false,
    relay_url: String(server.relay_url || "").slice(0, 256),
    direct_code: String(server.direct_code || "").slice(0, 32),
    lan: Boolean(server.lan),
    stamp: Date.now(),
  });
  saveRoomHistory(entries);
  renderRecent();
}

function toggleFavorite(code) {
  const clean = String(code || "").replace(/[^0-9A-Za-z]/g, "").toUpperCase();
  const entries = loadRoomHistory();
  const entry = entries.find((item) => item.code === clean);
  if (!entry) return;
  entry.favorite = !entry.favorite;
  saveRoomHistory(entries);
  renderRecent();
}

function renderRecent() {
  const list = $("recentList");
  if (!list) return;
  const entries = loadRoomHistory();
  list.replaceChildren();
  $("recentSection").hidden = entries.length === 0;
  if (!entries.length) return;
  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "server-row";
    const main = document.createElement("div");
    main.className = "server-main";
    const name = document.createElement("div");
    name.className = "server-name";
    name.textContent = entry.name || entry.code;
    main.append(name);
    const meta = document.createElement("div");
    meta.className = "server-meta";
    meta.textContent = `${entry.code.slice(0, 5)}-${entry.code.slice(5, 10)}-${entry.code.slice(10)}${entry.favorite ? " - saved" : ""}`;
    main.append(meta);
    row.append(main);
    const star = document.createElement("button");
    star.className = "mini-btn";
    star.type = "button";
    star.textContent = entry.favorite ? "Saved" : "Save";
    star.addEventListener("click", () => toggleFavorite(entry.code));
    row.append(star);
    const join = document.createElement("button");
    join.className = "mini-btn";
    join.type = "button";
    join.textContent = "Join";
    join.addEventListener("click", () => {
      if (entry.password) openServerJoin({ ...entry, password: true });
      else joinServer({ ...entry, password: false });
    });
    row.append(join);
    list.append(row);
  }
}

function openInviteLink() {
  const value = window.prompt("Paste a blender:// invite link");
  if (value) handleInviteLink(value);
}

function setPanel(name) {
  state.panel = name;
  for (const key of ["session", "servers", "settings"]) {
    $(`panel-${key}`).hidden = key !== name;
  }
  window.BMPLayout?.setTile("session", true);
  window.BMPLayout?.refreshRail();
  if (name === "servers") {
    $("serverSearch").value = state.serverQuery;
    refreshServerBrowser();
    setTimeout(() => $("serverSearch").focus(), 30);
  }
  if (name === "settings") {
    $("setName").value = settings.name;
    $("setPort").value = settings.port;
    $("setDirectory").value = settings.directory;
    $("setWorkerRelay").checked = settings.workerRelay;
    $("setRelay").value = settings.relay;
    $("setUseRelay").checked = settings.useRelay;
    $("setBlender").value = settings.blender;
  }
}

function scheduleBounds() {
  if (scheduleBounds.raf) return;
  scheduleBounds.raf = requestAnimationFrame(() => {
    scheduleBounds.raf = 0;
    updateBounds();
  });
}

let boundsBusy = false;
let boundsPending = false;

async function updateBounds() {
  if (!isTauri || !state.embedded) return;
  if (boundsBusy) {
    boundsPending = true;
    return;
  }
  const box = insets();
  const key = [
    Math.round(box.x),
    Math.round(box.y),
    Math.round(box.width),
    Math.round(box.height),
    Math.round(box.viewportW),
    Math.round(box.viewportH),
  ].join(":");
  if (key === state.boundsKey) return;
  state.boundsKey = key;
  boundsBusy = true;
  try {
    await invoke("set_embed_bounds", {
      xPx: Math.round(box.x),
      yPx: Math.round(box.y),
      widthPx: Math.round(box.width),
      heightPx: Math.round(box.height),
      viewportW: Math.round(box.viewportW),
      viewportH: Math.round(box.viewportH),
    });
  } catch (error) {
    state.boundsKey = "";
    log(`bounds failed: ${error}`);
    const alive = await invoke("blender_running").catch(() => true);
    if (alive === false) {
      state.embedded = false;
      renderEmbed();
    }
  } finally {
    boundsBusy = false;
    if (boundsPending) {
      boundsPending = false;
      scheduleBounds();
    }
  }
}

function insets() {
  const rect = document.querySelector(".stage-body").getBoundingClientRect();
  return {
    x: Math.round(rect.left),
    y: Math.round(rect.top),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
    viewportW: window.innerWidth,
    viewportH: window.innerHeight,
  };
}

/* ------------------------------------------------------------------ render */
function renderState(status) {
  const value = status?.state || "offline";
  const labels = { offline: "Offline", hosting: "Hosting", joining: "Connecting", connected: "Connected" };
  $("stateLabel").textContent = labels[value] || "Offline";
  $("stateDot").className = `dot ${value === "connected" || value === "hosting" ? "on" : value === "joining" ? "busy" : ""}`;
  $("railState").className = `rail-pill ${value === "connected" || value === "hosting" ? "on" : value === "joining" ? "busy" : ""}`;
  $("liveLabel").textContent = value === "connected" ? "live" : value;
  $("liveLabel").className = `live ${value === "connected" ? "on" : value === "joining" ? "busy" : ""}`;

  const active = isActive(status);
  const hosting = active && Boolean(status?.is_host);
  $("hostActions").hidden = active;
  $("hostSetup").hidden = active;
  $("activeActions").hidden = !active;
  $("embedActions").hidden = !active;
  $("embedChip").hidden = !active || state.embedded;
  $("detachChip").hidden = !active || !state.embedded;
  $("focusBtn").hidden = !state.embedded;

  $("heroEyebrow").textContent = active
    ? value === "joining"
      ? "Connecting"
      : "Server live"
    : "Get started";
  $("heroTitle").textContent = active
    ? value === "connected"
      ? "You're in the server"
      : value === "hosting"
        ? "Your server is live"
        : "Connecting to the server"
    : "Start a server";
  $("heroSub").textContent = active
    ? value === "hosting"
      ? settings.public
        ? "Your server is in the public browser. The host validates every edit."
        : "Private server. It is not listed in the public browser."
      : "Connected over TLS 1.3. The host validates every edit."
    : "Host a public server, or browse servers other people are hosting.";

  $("serverBox").hidden = !active;
  if (active) {
    $("serverLabel").textContent = hosting ? "Your server" : "Server";
    $("serverTitle").textContent = hosting
      ? serverDisplayName()
      : state.joinedServerName || "Connected";
    if (hosting) {
      if (!settings.public) {
        $("serverState").textContent = "private";
        $("serverState").className = "badge";
      } else if (!directoryBase()) {
        $("serverState").textContent = "directory not set";
        $("serverState").className = "badge warn";
      } else {
        $("serverState").textContent = "public";
        $("serverState").className = "badge on";
      }
    } else {
      $("serverState").textContent = "connected";
      $("serverState").className = "badge on";
    }
  }
  const verify = status?.verify_code || "";
  $("verifyRow").hidden = !verify;
  if (verify) $("verifyCode").textContent = verify;

  const hub = directoryBase();
  const shareDirectory = hub || state.lanDirectory || state.localDirectory;
  const showDirectory = hosting && Boolean(shareDirectory);
  $("directoryRow").hidden = !showDirectory;
  if (showDirectory) {
    $("directoryLabel").textContent = hub ? "HUB" : "LAN";
    $("directoryUrl").textContent = shareDirectory;
  }

  const name = status?.name || settings.name;
  $("meAvatar").textContent = initials(name);
  $("meAvatar").title = name;

  const rtt = Number(status?.rtt_ms || 0);
  $("pingValue").textContent = rtt > 0 ? String(Math.round(rtt)) : "-";
  $("latency").textContent = rtt > 0 ? `${Math.round(rtt)} ms` : active ? "measuring" : "no session";

  const blender = status?.blender;
  $("blenderLabel").textContent = blender ? `Blender ${blender}` : settings.blender ? "Blender ready" : "Blender 5.x not detected";
  $("statusRight").textContent = blender ? `Blender ${blender} Â· v${APP_VERSION}` : `v${APP_VERSION}`;
  const secure = $("secureLabel");
  if (secure) secure.textContent = status?.identity_public ? "TLS 1.3 + signed host" : "TLS 1.3 + pinned";
  $("stageMode").textContent = state.embedded ? "Blender docked" : active ? "Blender separate" : "no Blender window";
  $("statusLeft").textContent = active
    ? `${labels[value]} - ${hosting ? serverDisplayName() : state.joinedServerName || "server"}`
    : "Ready";
  $("statusMid").textContent = status?.relay ? "relay mode" : state.embedded ? "GPU passthrough" : "";
  $("stageHint").textContent = settings.blender ? "Blender is ready to launch." : "Blender 5.x not detected - install Blender 5 or set the path in Settings.";

  if (!active) {
    $("chatSub").textContent = "Messages sync with Blender";
  } else {
    $("chatSub").textContent = `${status.users?.length || 0} in the room`;
  }
}

function renderMembers(status) {
  const list = $("members");
  const users = Array.isArray(status?.users) ? status.users : [];
  $("memberCount").textContent = String(users.length);
  list.replaceChildren();

  if (!users.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = isActive(status) ? "Waiting for people to join." : "No one here yet.";
    list.append(empty);
    return;
  }

  for (const user of users.slice(0, 24)) {
    const item = document.createElement("li");

    const avatar = document.createElement("span");
    avatar.className = "mini";
    avatar.style.background = colorOf(user);
    avatar.textContent = initials(user.name);
    item.append(avatar);

    const who = document.createElement("span");
    who.className = "who";
    who.textContent = user.name || "user";
    item.append(who);

    if (user.active) {
      const editing = document.createElement("span");
      editing.className = "ms";
      editing.textContent = `editing ${String(user.active).slice(0, 28)}`;
      item.append(editing);
    }

    if (user.host) {
      const badge = document.createElement("span");
      badge.className = "host-badge";
      badge.textContent = "host";
      item.append(badge);
    }

    const role = String(user.role || "editor");
    if (status?.is_host && !user.host && user.name !== settings.name) {
      const select = document.createElement("select");
      select.className = "role-select";
      select.title = `Role for ${user.name}`;
      for (const value of ["viewer", "commenter", "editor", "admin"]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        if (value === role) option.selected = true;
        select.append(option);
      }
      select.addEventListener("change", async () => {
        try {
          await request({ action: "role", target: user.name, role: select.value });
          toast(`${user.name} is now ${select.value}`, "ok");
        } catch (error) {
          toast(String(error), "error");
        }
      });
      item.append(select);
    } else {
      const badge = document.createElement("span");
      badge.className = "ms";
      badge.textContent = role === "owner" ? "owner" : role;
      item.append(badge);
    }

    const ms = document.createElement("span");
    ms.className = "ms";
    ms.textContent = user.ping_ms ? `${Math.round(user.ping_ms)} ms` : "";
    item.append(ms);

    if (status?.is_host && !user.host && user.name) {
      const kick = document.createElement("button");
      kick.className = "kick-btn";
      kick.type = "button";
      kick.textContent = "Kick";
      kick.title = `Kick ${user.name}`;
      kick.addEventListener("click", () => kickPlayer(user.name));
      item.append(kick);
    }

    list.append(item);
  }
}

function renderMessages(status) {
  const messages = Array.isArray(status?.messages) ? status.messages.slice(-60) : [];
  const key = `${messages.length}:${messages[messages.length - 1]?.text || ""}:${messages[messages.length - 1]?.stamp || ""}`;
  if (key === state.messagesKey) return;
  const previousKey = state.messagesKey;
  state.messagesKey = key;
  const latest = messages[messages.length - 1];
  if (previousKey && latest && latest.author && latest.author !== settings.name) {
    notificationBeep();
  }

  const host = $("messages");
  host.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "message empty";
    empty.textContent = isActive(status) ? "Nothing said yet. Say hi." : "Start a room to chat.";
    host.append(empty);
    return;
  }

  for (const message of messages) {
    const row = document.createElement("div");
    row.className = "message";
    const bubble = document.createElement("div");
    bubble.className = "bubble";

    const meta = document.createElement("div");
    meta.className = "meta";
    const author = document.createElement("b");
    author.textContent = message.author || "system";
    meta.append(author);
    const stamp = document.createElement("span");
    stamp.textContent = message.stamp || "";
    meta.append(stamp);

    const text = document.createElement("div");
    text.textContent = message.text || "";

    bubble.append(meta, text);
    row.append(bubble);
    host.append(row);
  }
  host.scrollTop = host.scrollHeight;
}

function renderStats(status) {
  const bytesIn = Number(status?.bytes_in || 0);
  const bytesOut = Number(status?.bytes_out || 0);
  const now = performance.now();

  if (state.lastBytes && now > state.lastStamp) {
    const seconds = (now - state.lastStamp) / 1000;
    state.rates.down = Math.max(0, (bytesIn - state.lastBytes.in) / seconds / 1024);
    state.rates.up = Math.max(0, (bytesOut - state.lastBytes.out) / seconds / 1024);
  }
  state.lastBytes = { in: bytesIn, out: bytesOut };
  state.lastStamp = now;

  $("downValue").textContent = state.rates.down > 0 ? state.rates.down.toFixed(0) : "0";
  $("upValue").textContent = state.rates.up > 0 ? state.rates.up.toFixed(0) : "0";
  const total = (bytesIn + bytesOut) / (1024 * 1024);
  $("totalValue").textContent = total > 0.01 ? total.toFixed(1) : "0";

  const rtt = Number(status?.rtt_ms || 0);
  if (rtt > 0) {
    state.latency.push(rtt);
    if (state.latency.length > 60) state.latency.shift();
    drawSpark();
  }
}

function drawSpark() {
  const canvas = $("spark");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 280;
  const height = 36;
  if (canvas.width !== Math.round(width * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const values = state.latency.length ? state.latency : [0, 0];
  const max = Math.max(40, ...values);
  const step = width / Math.max(1, values.length - 1);

  context.beginPath();
  values.forEach((value, index) => {
    const x = index * step;
    const y = height - 6 - (value / max) * (height - 14);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  const gradient = context.createLinearGradient(0, 0, width, 0);
  gradient.addColorStop(0, "rgba(94,234,212,0.9)");
  gradient.addColorStop(1, "rgba(139,92,246,0.9)");
  context.strokeStyle = gradient;
  context.lineWidth = 1.8;
  context.stroke();

  context.lineTo(width, height);
  context.lineTo(0, height);
  context.closePath();
  context.fillStyle = "rgba(94,234,212,0.06)";
  context.fill();
}

function renderEmbed() {
  $("placeholder").hidden = state.embedded;
  $("embedChip").hidden = state.embedded;
  $("detachChip").hidden = !state.embedded;
  $("focusBtn").hidden = !state.embedded;
  $("embedBtn").hidden = state.embedded;
  $("detachBtn").hidden = !state.embedded;
  const launchLabel = $("launchLabel");
  if (launchLabel) launchLabel.textContent = state.embedded ? "Focus Blender" : "Launch Blender";
  $("stageMode").textContent = state.embedded ? "Blender docked" : "Blender separate";
}

function applyStatus(status) {
  if (!status || typeof status !== "object") return;
  state.status = status;
  const error = String(status.last_error || "").trim();
  if (error && error !== state.lastError) {
    state.lastError = error;
    const errorCode = String(status.error_code || "").trim();
    log(errorCode ? `addon error [${errorCode}]: ${error}` : `addon error: ${error}`);
    toast(errorCode ? `${errorCode}: ${error}` : error, "error");
  } else if (!error) {
    state.lastError = "";
  }
  const hosting = status.state === "hosting" && Boolean(status.is_host);
  const addonVersion = String(status.addon || "").trim();
  if (addonVersion && addonVersion !== APP_VERSION) {
    if (!state.addonWarned) {
      state.addonWarned = true;
      const message = `Blender is running addon ${addonVersion}, but the app is ${APP_VERSION}. Close Blender completely, then launch it from the app again.`;
      log(`addon version mismatch: ${message}`);
      toast(message, "error");
    }
  } else if (addonVersion) {
    state.addonWarned = false;
  }
  if (hosting) {
    state.offlineStreak = 0;
    if (!state.wasHosting && settings.public) announceServer();
    state.wasHosting = true;
  } else if (state.wasHosting && status.state === "offline") {
    // A single offline read can be a status-file write in flight. Removing the
    // public entry on the first one made servers vanish mid-session.
    state.offlineStreak += 1;
    if (state.offlineStreak >= 3) {
      removeServer();
      state.wasHosting = false;
    }
  } else if (status.state !== "offline") {
    state.offlineStreak = 0;
  }
  renderState(status);
  renderMembers(status);
  renderMessages(status);
  renderStats(status);
}

/* ------------------------------------------------------------------ actions */
async function detectBlender() {
  if (!isTauri) {
    toast("Browser demo mode - desktop features are off", "error");
    return;
  }
  try {
    const path = await invoke("detect_blender");
    log(`detect_blender -> ${path || "null"}`);
    if (path) {
      settings.blender = path;
      store.set("blender", path);
      $("blenderLabel").textContent = "Blender detected";
      $("setBlender").value = path;
      $("stageHint").textContent = "Blender is ready to launch.";
    } else {
      toast("Blender 5.x not found - install Blender 5 or set the path in Settings", "error");
    }
  } catch (error) {
    log(`detect_blender failed: ${error}`);
    toast(`Blender detection failed: ${error}`, "error");
  }
}

async function exportDiagnostics() {
  if (!isTauri) {
    toast("Browser demo mode - desktop features are off", "error");
    return;
  }
  try {
    const path = await invoke("export_diagnostics");
    log(`diagnostics exported -> ${path}`);
    toast("Diagnostics bundle saved", "ok");
  } catch (error) {
    log(`export_diagnostics failed: ${error}`);
    toast(`Diagnostics failed: ${error}`, "error");
  }
}

async function openDashboard() {
  if (isTauri) {
    try {
      await invoke("open_dashboard");
      log("dashboard opened");
    } catch (error) {
      log(`open_dashboard failed: ${error}`);
      toast(`Dashboard failed: ${error}`, "error");
    }
    return;
  }
  const popup = window.open(
    "dashboard.html",
    "bmp-dashboard",
    "width=560,height=760,resizable=yes,scrollbars=yes",
  );
  if (!popup) toast("Popup blocked - allow popups for the dashboard", "error");
}

function syncAccessibilityControls() {
  const a11y = window.BMPA11y;
  if (!a11y) return;
  const contrast = $("setContrast");
  const font = $("setFontScale");
  const motion = $("setMotion");
  const sound = $("setSound");
  if (contrast) contrast.value = a11y.contrast || "normal";
  if (font) font.value = a11y.font || "normal";
  if (motion) motion.checked = a11y.reducedMotion;
  if (sound) sound.checked = a11y.sound;
  if (store.loaded) {
    store.set("contrast", a11y.contrast || "normal");
    store.set("fontScale", a11y.font || "normal");
    store.set("reducedMotion", a11y.reducedMotion ? "1" : "0");
    store.set("sound", a11y.sound ? "1" : "0");
    store.set("dnd", a11y.dnd ? "1" : "0");
  }
}

async function launchBlender() {
  if (!settings.blender) {
    setPanel("settings");
    toast("Set the Blender path first", "error");
    return;
  }
  if (state.blenderBusy) return;
  state.blenderBusy = true;
  try {
    if (state.embedded) {
      log("Blender is already embedded; reusing it");
      await invoke("focus_blender").catch(() => {});
      return;
    }
    // A running Blender is reused even if its window is currently hidden or
    // being recreated. Launching another copy created duplicate players and
    // multiple docked windows when the first window was slow to appear.
    const running = await invoke("blender_running").catch(() => false);
    if (running) {
      log("Blender is already running; reusing it");
      state.dockEnabled = true;
      const found = await invoke("find_blender_window").catch(() => null);
      if (found) await embedNow();
      else toast("Blender is running; waiting for its window", "ok");
      return;
    }
    const existing = await invoke("find_blender_window").catch(() => null);
    if (existing) {
      log("reusing the running Blender window");
      state.dockEnabled = true;
      if (state.embedded) await invoke("focus_blender").catch(() => {});
      else await embedNow();
      return;
    }
    const pid = await invoke("launch_blender", { path: settings.blender, blendFile: null });
    log(`launch_blender ok pid=${pid}`);
    state.dockEnabled = true;
    toast("Blender launching...", "ok");
  } catch (error) {
    log(`launch_blender failed: ${error}`);
    toast(String(error), "error");
  } finally {
    state.blenderBusy = false;
  }
}

async function request(request) {
  try {
    const result = await invoke("write_session", { request });
    log(`write_session ${request.action} ok`);
    return result;
  } catch (error) {
    log(`write_session ${request.action} failed: ${error}`);
    throw error;
  }
}

async function hostRoom() {
  if (!settings.blender) {
    setPanel("settings");
    toast("Set the Blender path first", "error");
    return;
  }
  settings.serverName = $("serverName").value.trim() || `${settings.name}'s server`;
  settings.public = $("serverPublic").checked;
  settings.pin = $("serverPassword").value;
  store.set("serverName", settings.serverName);
  store.set("public", settings.public ? "1" : "0");
  store.set("pin", settings.pin);
  removeServer();
  if (settings.public && !directoryBase()) {
    toast("Set a directory URL in Settings to list this server publicly", "error");
  }
  const workerRelay = settings.public ? hubRelayUrl() : "";
  try {
    await request({
      action: "host",
      name: settings.name,
      port: settings.port,
      pin: settings.pin || null,
      relay: workerRelay ? null : settings.useRelay ? settings.relay : null,
      relay_url: workerRelay || null,
      relay_token: workerRelay ? settings.hostKey : null,
      use_relay: Boolean(workerRelay) || settings.useRelay,
    });
    state.serverId = makeServerId();
    state.joinedServerName = "";
    state.lastAnnounce = 0;
    state.wasHosting = false;
    toast(settings.public ? "Public server starting..." : "Private server starting...", "ok");
    await launchBlender();
    announceServer();
  } catch (error) {
    toast(String(error), "error");
  }
}

async function joinServer(server, pin = "") {
  if (!server || !server.code) return;
  const compat = serverCompatibility(server);
  if (!compat.ok) {
    toast(compat.reason, "error");
    return;
  }
  removeServer();
  // Prefer the direct LAN path for latency, but keep the relay as a fallback:
  // the host's random direct port is often blocked by Windows Firewall.
  const directCodes = (Array.isArray(server.direct_codes) ? server.direct_codes : [])
    .filter((value) => typeof value === "string" && value)
    .slice(0, 4);
  const directCode = String(server.direct_code || directCodes[0] || "").trim();
  if (directCode && !directCodes.includes(directCode)) directCodes.unshift(directCode);
  const relayUrl = server.relay_url || (server.relay ? hubRelayUrl() : "");
  const preferDirect = directCodes.length > 0;
  const code = server.code;
  try {
    await request({
      action: "join",
      code,
      name: settings.name,
      pin: pin || null,
      relay_url: relayUrl || null,
      use_relay: Boolean(relayUrl),
      prefer_direct: preferDirect,
      direct_code: directCode || null,
      direct_codes: directCodes,
    });
    state.joinedServerName = server.name || "server";
    state.serverId = "";
    state.pendingServer = null;
    rememberRoom({
      code: code.replace(/[^0-9A-Za-z]/g, "").toUpperCase(),
      name: server.name || "server",
      host: server.host || settings.name,
      password: Boolean(pin || server.password),
      relay_url: relayUrl || "",
      direct_code: directCode || "",
      direct_codes: directCodes,
      lan: Boolean(server.lan),
    });
    $("serverJoinBox").hidden = true;
    toast(`Joining ${server.name || "server"} - replacing this scene with the host's...`, "ok");
    setPanel("session");
    await launchBlender();
  } catch (error) {
    toast(String(error), "error");
  }
}

async function leaveRoom() {
  try {
    await request({ action: "leave" });
    removeServer();
    toast("Leaving server...");
    state.joinedServerName = "";
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 200));
      const status = await poll();
      if (!status || !isActive(status)) {
        toast("Left the server");
        return;
      }
    }
    toast("Blender did not disconnect. Close Blender, then rejoin.", "error");
  } catch (error) {
    toast(String(error), "error");
  }
}

async function kickPlayer(name) {
  if (!name) return;
  if (!window.confirm(`Kick ${name} from the server?`)) return;
  try {
    await request({ action: "kick", target: name });
    toast(`Kicked ${name}`, "ok");
  } catch (error) {
    toast(String(error), "error");
  }
}

/* ------------------------------------------------------------------ servers */
async function discoverDirectories() {
  if (!isTauri) return;
  try {
    const found = await invoke("discover_directories");
    state.discovered = Array.isArray(found) ? found : [];
  } catch (error) {
    state.discovered = [];
    log(`directory discovery failed: ${error}`);
  }
}

async function refreshServerBrowser() {
  await discoverDirectories();
  await loadServers();
}

async function loadServers() {
  const base = directoryBase();
  const urls = [];
  if (state.localDirectory) urls.push(state.localDirectory);
  if (base) urls.push(base);
  for (const item of state.discovered) {
    if (item && item.url) urls.push(item.url);
  }
  const unique = [...new Set(urls.map((url) => url.replace(/\/+$/, "")).filter(Boolean))];

  const lan = state.lanDirectory || state.localDirectory;
  const note = [];
  if (lan) note.push(`LAN ${lan}`);
  if (base && base !== state.localDirectory) note.push(`extra ${base}`);
  if (state.discovered.length) note.push(`${state.discovered.length} found`);
  $("serverDirectoryNote").textContent = note.join(" - ") || "No directory configured.";

  if (!unique.length) {
    renderServers("No directory is available yet. Start the app-hosted server or set a directory URL in Settings.");
    return;
  }

  const query = encodeURIComponent(state.serverQuery.trim());
  const results = await Promise.allSettled(
    unique.map(async (url) => {
      const response = await fetch(`${url}/servers?q=${query}&limit=100`, { cache: "no-store" });
      if (!response.ok) throw new Error(`directory returned ${response.status}`);
      const body = await response.json();
      return { url, servers: Array.isArray(body.servers) ? body.servers : [] };
    }),
  );

  const merged = new Map();
  let failures = 0;
  for (const result of results) {
    if (result.status !== "fulfilled") {
      failures += 1;
      continue;
    }
    const { url, servers } = result.value;
    const lan =
      url === state.localDirectory ||
      url === state.lanDirectory ||
      state.discovered.some((item) => item && item.url === url);
    for (const server of servers) {
      if (!server || !server.code) continue;
      const entry = { ...server, lan };
      const existing = merged.get(server.code);
      if (!existing || (entry.direct_code && !existing.direct_code)) {
        merged.set(server.code, entry);
      }
    }
  }
  if (failures === unique.length) {
    state.servers = [];
    state.directoryError = "all directories unreachable";
    renderServers(`Could not reach any directory (${unique.join(", ")}).`);
    log("all directory list requests failed");
    return;
  }
  state.servers = [...merged.values()];
  state.directoryError = failures ? `${failures} directory unreachable` : "";
  renderServers("");
}

function renderServers(message) {
  const list = $("serverList");
  list.replaceChildren();
  if (message) {
    const item = document.createElement("p");
    item.className = "empty";
    item.textContent = message;
    list.append(item);
    return;
  }
  const activeCode = isActive(state.status)
    ? String(state.status?.code || "").replace(/[^0-9A-Z]/g, "")
    : "";
  const visible = state.servers.filter((server) => !activeCode || server.code !== activeCode);
  if (!visible.length) {
    const item = document.createElement("p");
    item.className = "empty";
    item.textContent = activeCode
      ? state.status?.is_host
        ? "Your server is listed. Other public servers will appear here."
        : "You are connected to this server. Other public servers will appear here."
      : state.serverQuery.trim()
        ? `No public servers match "${state.serverQuery.trim()}".`
        : "No public servers are listed right now.";
    list.append(item);
    return;
  }
  for (const server of visible) {
    const row = document.createElement("div");
    row.className = "server-row";
    const compat = serverCompatibility(server);
    if (!compat.ok) row.classList.add("incompatible");

    const main = document.createElement("div");
    main.className = "server-main";
    const name = document.createElement("div");
    name.className = "server-name";
    name.textContent = server.name || "Untitled server";
    main.append(name);
    const meta = document.createElement("div");
    meta.className = "server-meta";
    const host = server.host || "unknown host";
    const players = Number(server.players) || 1;
    const maximum = Number(server.max) || 16;
    meta.textContent = `${host} - ${players}/${maximum} players`;
    main.append(meta);
    row.append(main);

    const tags = document.createElement("div");
    tags.className = "server-tags";
    if (server.password) {
      const badge = document.createElement("span");
      badge.className = "badge warn";
      badge.textContent = "password";
      tags.append(badge);
    }
    if (server.relay) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "relay";
      tags.append(badge);
    }
    if (server.lan) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "LAN";
      tags.append(badge);
    }
    if (!compat.ok) {
      const badge = document.createElement("span");
      badge.className = "badge warn";
      badge.textContent = "update";
      badge.title = compat.reason;
      tags.append(badge);
    }
    row.append(tags);

    const join = document.createElement("button");
    join.className = "mini-btn";
    join.type = "button";
    join.textContent = compat.ok ? "Join" : "Update";
    join.disabled = !compat.ok;
    join.title = compat.reason || "";
    join.addEventListener("click", () => {
      if (!compat.ok) {
        toast(compat.reason, "error");
        return;
      }
      openServerJoin(server);
    });
    row.append(join);

    list.append(row);
  }
}

function openServerJoin(server) {
  const compat = serverCompatibility(server);
  if (!compat.ok) {
    toast(compat.reason, "error");
    return;
  }
  state.pendingServer = server;
  $("serverJoinName").textContent = server.name || "server";
  const needsPassword = Boolean(server.password);
  $("serverPasswordRow").hidden = !needsPassword;
  $("serverPasswordJoin").value = "";
  $("serverJoinBox").hidden = false;
  if (needsPassword) setTimeout(() => $("serverPasswordJoin").focus(), 20);
}

async function connectServer() {
  const server = state.pendingServer;
  if (!server) return;
  const pin = server.password ? $("serverPasswordJoin").value : "";
  await joinServer(server, pin);
}

function parseIdentityDescriptor(value) {
  if (!value || typeof value !== "string") return null;
  try {
    const body = JSON.parse(value);
    return body && typeof body === "object" ? body : null;
  } catch {
    return null;
  }
}

function announceServer() {
  if (!isTauri || !settings.public) return;
  const status = state.status;
  if (!status || status.state !== "hosting" || !status.is_host || !status.code) return;
  if (!state.serverId) state.serverId = makeServerId();
  const targets = [...new Set([state.localDirectory, directoryBase()].filter(Boolean).map((url) => url.replace(/\/+$/, "")))];
  if (!targets.length) return;
  state.announceTargets = targets;
  const players = Array.isArray(status.users) ? Math.max(1, status.users.length) : 1;
  const body = {
    id: state.serverId,
    name: serverDisplayName(),
    host: settings.name,
    code: status.code,
    players,
    max: 64,
    password: Boolean(settings.pin),
    relay: Boolean(status.relay),
    relay_url: hubRelayUrl(),
    host_token: hubRelayUrl() ? settings.hostKey : "",
    version: APP_VERSION,
    protocol: PROTOCOL_VERSION,
    caps: CAPABILITIES,
    min_app: APP_VERSION,
    identity_public: status.identity_public || "",
    identity_signature: status.identity_signature || "",
    identity_descriptor: parseIdentityDescriptor(status.identity_descriptor),
    direct_code: status.direct_code || "",
    direct_codes: Array.isArray(status.direct_codes) ? status.direct_codes.slice(0, 4) : [],
  };
  const lanBody = body;
  Promise.allSettled(
    targets.map(async (url) => {
      const response = await fetch(`${url}/announce`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(url === state.localDirectory ? lanBody : body),
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`directory returned ${response.status}`);
      return url;
    }),
  ).then((results) => {
    const succeeded = results.filter((result) => result.status === "fulfilled").length;
    if (succeeded) {
      state.lastAnnounce = Date.now();
      state.directoryError = "";
      return;
    }
    const message = `Directory unreachable: ${targets.join(", ")}`;
    if (state.directoryError !== message) {
      state.directoryError = message;
      toast(message, "error");
    }
    log("all directory announce requests failed");
  });
}

function removeServer() {
  const id = state.serverId;
  state.serverId = "";
  if (!isTauri || !id) return;
  const targets = state.announceTargets.length
    ? state.announceTargets
    : [...new Set([state.localDirectory, directoryBase()].filter(Boolean))];
  state.announceTargets = [];
  for (const url of targets) {
    fetch(`${url.replace(/\/+$/, "")}/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
      cache: "no-store",
    }).catch(() => {});
  }
}

async function embedNow() {
  state.dockEnabled = true;
  try {
    const box = insets();
    if (box.width < 80 || box.height < 60) {
      toast("The viewport is not ready yet", "error");
      return;
    }
    await invoke("embed_blender", {
      xPx: box.x,
      yPx: box.y,
      widthPx: box.width,
      heightPx: box.height,
      viewportW: box.viewportW,
      viewportH: box.viewportH,
    });
    state.embedded = true;
    state.embedError = "";
    state.verifyAt = Date.now() + 4000;
    state.verifyTries = 0;
    state.verifyBlack = 0;
    renderEmbed();
    toast("Blender docked", "ok");
  } catch (error) {
    toast(String(error), "error");
  }
}

async function detachNow() {
  state.dockEnabled = false;
  try {
    await invoke("detach_blender");
  } catch {
    /* ignore */
  }
  state.embedded = false;
  renderEmbed();
}

async function sendChat(event) {
  event.preventDefault();
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  try {
    await request({ action: "chat", text });
  } catch (error) {
    toast(String(error), "error");
  }
}

/* ------------------------------------------------------------------ loops */
async function poll() {
  try {
    const status = await invoke("read_status");
    if (status) applyStatus(status);
    return status;
  } catch {
    /* ignore transient read errors */
    return null;
  }
}

async function verifyDock() {
  const verdict = await invoke("dock_looks_black").catch(() => null);
  state.verifyTries += 1;
  if (verdict === true) {
    state.verifyBlack += 1;
    // Give a slow Blender a second chance to finish painting before falling
    // back; two black reads in a row mean the surface really is not showing.
    if (state.verifyBlack < 2) {
      state.verifyAt = Date.now() + 2000;
      return;
    }
    state.verifyAt = 0;
    log("dock render check: black; switching to a separate window");
    state.dockEnabled = false;
    await invoke("detach_blender").catch(() => {});
    state.embedded = false;
    renderEmbed();
    toast("Blender can't display inside the app on this PC - it is open in its own window now.", "error");
    return;
  }
  if (verdict === false) {
    state.verifyAt = 0;
    state.verifyBlack = 0;
    log("dock render check: ok");
    return;
  }
  // The app is minimized or another window is in front; retry for a few loops.
  if (state.verifyTries >= 8) state.verifyAt = 0;
}

async function embedLoop() {
  if (!isTauri || state.embedBusy) return;
  state.embedBusy = true;
  try {
    // Docking follows Blender's lifetime, not the session state. Session events
    // can arrive late or be rewritten mid-flight; using them here caused the
    // window to detach while the multiplayer session was still healthy.
    const alive = await invoke("blender_running").catch(() => true);
    if (alive === false) {
      if (state.embedded) {
        await invoke("detach_blender").catch(() => {});
        state.embedded = false;
        renderEmbed();
      }
      if (state.wasHosting) {
        removeServer();
        state.wasHosting = false;
      }
      return;
    }
    // Layout edit mode hides the window on purpose; do not mistake that for a
    // closed Blender and tear the dock down.
    if (state.blenderHidden || !state.dockEnabled) return;
    const box = insets();
    if (box.width < 80 || box.height < 60) return;
    if (state.embedded) {
      await updateBounds();
      if (state.verifyAt && Date.now() >= state.verifyAt) await verifyDock();
      return;
    }
    const found = await invoke("find_blender_window").catch(() => null);
    if (!found) return;
    await invoke("embed_blender", {
      xPx: box.x,
      yPx: box.y,
      widthPx: box.width,
      heightPx: box.height,
      viewportW: box.viewportW,
      viewportH: box.viewportH,
    });
    state.embedded = true;
    state.embedError = "";
    state.verifyAt = Date.now() + 4000;
    state.verifyTries = 0;
    state.verifyBlack = 0;
    renderEmbed();
    log("embed ok");
  } catch (error) {
    const message = String(error);
    if (message !== state.embedError) {
      state.embedError = message;
      log(`embed skipped: ${message}`);
    }
  } finally {
    state.embedBusy = false;
  }
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  $("hostBtn").addEventListener("click", hostRoom);
  $("joinBtn").addEventListener("click", () => setPanel("servers"));
  $("joinClose").addEventListener("click", () => setPanel("session"));
  $("serverRefresh").addEventListener("click", refreshServerBrowser);
  $("inviteOpen").addEventListener("click", openInviteLink);
  $("inviteCopy").addEventListener("click", async () => {
    const link = inviteLink(state.status?.code || "");
    if (!link) {
      toast("Start a room first", "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(link);
      toast("Invite link copied", "ok");
    } catch {
      toast(link);
    }
  });
  $("directoryCopy").addEventListener("click", async () => {
    const url = state.lanDirectory || state.localDirectory;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      toast("Directory link copied", "ok");
    } catch {
      toast(url);
    }
  });
  $("serverConnect").addEventListener("click", connectServer);
  $("serverCancel").addEventListener("click", () => {
    state.pendingServer = null;
    $("serverJoinBox").hidden = true;
  });
  $("serverSearch").addEventListener("input", (event) => {
    state.serverQuery = event.target.value;
    clearTimeout(serverSearchTimer);
    serverSearchTimer = setTimeout(() => loadServers(), 250);
  });
  $("serverSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadServers();
  });
  $("serverPasswordJoin").addEventListener("keydown", (event) => {
    if (event.key === "Enter") connectServer();
  });
  $("settingsClose").addEventListener("click", () => setPanel("session"));
  $("leaveBtn").addEventListener("click", leaveRoom);
  $("launchBtn").addEventListener("click", () => {
    if (state.embedded) {
      invoke("focus_blender").catch(() => {});
      return;
    }
    launchBlender();
  });
  $("embedBtn").addEventListener("click", embedNow);
  $("detachBtn").addEventListener("click", detachNow);
  $("embedChip").addEventListener("click", embedNow);
  $("detachChip").addEventListener("click", detachNow);
  $("focusBtn").addEventListener("click", () => invoke("focus_blender").catch(() => {}));
  $("focusView").addEventListener("click", () => window.BMPLayout?.focusView());
  $("chatForm").addEventListener("submit", sendChat);
  $("chatClose").addEventListener("click", () => window.BMPLayout?.setTile("chat", false));
  $("collapseLeft").addEventListener("click", () => window.BMPLayout?.setTile("session", false));
  $("refreshBtn").addEventListener("click", poll);
  $("detectBtn").addEventListener("click", detectBlender);
  $("exportDiagnostics").addEventListener("click", exportDiagnostics);
  document.querySelectorAll('.rail-btn[data-view="dashboard"]').forEach((button) => {
    button.addEventListener("click", openDashboard);
  });
  $("openDashboard").addEventListener("click", openDashboard);
  $("setContrast").addEventListener("change", () => window.BMPA11y?.set("contrast", $("setContrast").value));
  $("setFontScale").addEventListener("change", () => window.BMPA11y?.set("font", $("setFontScale").value));
  $("setMotion").addEventListener("change", () => window.BMPA11y?.set("motion", $("setMotion").checked ? "1" : "0"));
  $("setSound").addEventListener("change", () => window.BMPA11y?.set("sound", $("setSound").checked ? "1" : "0"));
  document.addEventListener("bmp-a11y", syncAccessibilityControls);
  $("saveSettings").addEventListener("click", () => {
    settings.name = $("setName").value.trim() || settings.name;
    settings.port = Math.max(1024, Math.min(65535, Number($("setPort").value) || 51999));
    settings.directory = $("setDirectory").value.trim();
    settings.workerRelay = $("setWorkerRelay").checked;
    settings.relay = $("setRelay").value.trim();
    settings.useRelay = $("setUseRelay").checked;
    settings.blender = $("setBlender").value.trim();
    store.set("name", settings.name);
    store.set("port", settings.port);
    store.set("directory", settings.directory);
    store.set("workerRelay", settings.workerRelay ? "1" : "0");
    store.set("relay", settings.relay);
    store.set("useRelay", settings.useRelay ? "1" : "0");
    store.set("blender", settings.blender);
    $("meAvatar").textContent = initials(settings.name);
    toast("Settings saved", "ok");
    setPanel("session");
  });

  $("setPort").addEventListener("change", () => {
    settings.port = Math.max(1024, Math.min(65535, Number($("setPort").value) || 51999));
  });

  window.addEventListener("resize", scheduleBounds);
}

async function boot() {
  await loadSettings();
  applyStoredSettings();
  window.BMPA11y?.apply();
  wire();
  window.BMPLayout?.init({
    content: {
      session: $("tileSession"),
      viewport: $("tileViewport"),
      chat: $("tileChat"),
    },
    onBounds: scheduleBounds,
    setBlenderVisible: (visible) => {
      state.blenderHidden = !visible;
      invoke("set_blender_visible", { visible }).catch(() => {});
    },
    getPanel: () => state.panel,
    setPanel,
  });
  markBridge();
  setPanel("session");
  try {
    new ResizeObserver(scheduleBounds).observe(document.querySelector(".stage-body"));
  } catch {
    /* ResizeObserver is optional */
  }
  $("meAvatar").textContent = initials(settings.name);
  $("serverName").value = settings.serverName || `${settings.name}'s server`;
  $("serverPublic").checked = settings.public;
  $("serverPassword").value = settings.pin;
  $("setName").value = settings.name;
  $("setPort").value = settings.port;
  $("setDirectory").value = settings.directory;
  $("setWorkerRelay").checked = settings.workerRelay;
  $("setRelay").value = settings.relay;
  $("setUseRelay").checked = settings.useRelay;
  $("setBlender").value = settings.blender;
  syncAccessibilityControls();

  if (state.demo) {
    applyStatus(demoStatus);
    renderEmbed();
    return;
  }

  try {
    const directory = await invoke("local_directory");
    if (directory) {
      state.localDirectory = directory.local || "";
      state.lanDirectory = directory.lan || "";
      log(`app directory: ${state.localDirectory} / ${state.lanDirectory}`);
    }
  } catch (error) {
    log(`local directory unavailable: ${error}`);
  }

  await detectBlender();
  await poll();
  renderRecent();
  setInterval(poll, 750);
  setInterval(pollDeepLink, 1000);
  setInterval(embedLoop, 1200);
  setInterval(announceServer, 15000);
  setInterval(() => {
    if (state.panel === "servers") loadServers();
  }, 8000);

  try {
    const flags = await invoke("startup_flags");
    if (flags && flags.autohost) await hostRoom();
    if (flags && flags.autojoin) {
      await joinServer({ code: flags.autojoin, name: "server", password: false });
    } else if (flags && flags.deep_link) {
      await handleInviteLink(flags.deep_link);
    }
  } catch {
    /* flags are optional */
  }
  await pollDeepLink();
}

boot();
window.addEventListener("pagehide", () => {
  try {
    store.flush();
  } catch {
    /* best effort on shutdown */
  }
});
})();
