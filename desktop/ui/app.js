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

const store = {
  get(key, fallback) {
    try {
      const value = localStorage.getItem(`bmp.${key}`);
      return value === null ? fallback : value;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`bmp.${key}`, String(value));
    } catch {
      /* ignore */
    }
  },
};

const settings = {
  name: store.get("name", `user-${Math.random().toString(36).slice(2, 6)}`),
  port: Number(store.get("port", 51999)),
  pin: store.get("pin", ""),
  relay: store.get("relay", ""),
  useRelay: store.get("useRelay", "0") === "1",
  blender: store.get("blender", ""),
};

const state = {
  status: null,
  embedded: false,
  panel: "session",
  latency: [],
  lastBytes: null,
  lastStamp: 0,
  rates: { down: 0, up: 0 },
  messagesKey: "",
  blenderBusy: false,
  lastError: "",
  demo: demoMode,
};

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
  addon: "0.1.0",
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

function normalizeCode(value) {
  return value
    .toUpperCase()
    .replace(/[^0-9A-Z]/g, "")
    .replace(/O/g, "0")
    .replace(/[IL]/g, "1")
    .slice(0, 15);
}

function groupCode(value) {
  const text = normalizeCode(value);
  const parts = [text.slice(0, 5), text.slice(5, 10), text.slice(10, 15)].filter(Boolean);
  return parts.join("-");
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

function setPanel(name) {
  state.panel = name;
  for (const key of ["session", "join", "settings"]) {
    $(`panel-${key}`).hidden = key !== name;
  }
  window.BMPLayout?.setTile("session", true);
  window.BMPLayout?.refreshRail();
  if (name === "join") {
    $("joinCode").value = "";
    $("joinName").value = settings.name;
    $("joinPin").value = "";
    setTimeout(() => $("joinCode").focus(), 30);
  }
  if (name === "settings") {
    $("setName").value = settings.name;
    $("setPort").value = settings.port;
    $("setPin").value = settings.pin;
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
  boundsBusy = true;
  try {
    const box = insets();
    await invoke("set_embed_bounds", {
      xPx: box.x,
      yPx: box.y,
      widthPx: box.width,
      heightPx: box.height,
      viewportW: box.viewportW,
      viewportH: box.viewportH,
    });
  } catch (error) {
    log(`bounds failed: ${error}`);
    state.embedded = false;
    renderEmbed();
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
  $("hostActions").hidden = active;
  $("activeActions").hidden = !active;
  $("embedActions").hidden = !active;
  $("embedChip").hidden = !active || state.embedded;
  $("detachChip").hidden = !active || !state.embedded;
  $("focusBtn").hidden = !state.embedded;

  $("heroEyebrow").textContent = active
    ? value === "joining"
      ? "Connecting"
      : "Session live"
    : "Get started";
  $("heroTitle").textContent = active
    ? value === "connected"
      ? "You're in the room"
      : value === "hosting"
        ? "Your room is live"
        : "Connecting to the host"
    : "Start a room";
  $("heroSub").textContent = active
    ? value === "hosting"
      ? "Share the code. Blender is docked here; the host validates every edit."
      : "Share the code. Everyone connects over TLS 1.3 and the host validates every edit."
    : "One click launches Blender, starts the session and embeds it right here.";

  const code = status?.code || "";
  $("codeBox").hidden = !code;
  if (code) $("roomCode").textContent = code;
  const verify = status?.verify_code || "";
  $("verifyRow").hidden = !verify;
  if (verify) $("verifyCode").textContent = verify;

  const name = status?.name || settings.name;
  $("meAvatar").textContent = initials(name);
  $("meAvatar").title = name;

  const rtt = Number(status?.rtt_ms || 0);
  $("pingValue").textContent = rtt > 0 ? String(Math.round(rtt)) : "-";
  $("latency").textContent = rtt > 0 ? `${Math.round(rtt)} ms` : active ? "measuring" : "no session";

  const blender = status?.blender;
  $("blenderLabel").textContent = blender ? `Blender ${blender}` : settings.blender ? "Blender ready" : "Blender 5.x not detected";
  $("statusRight").textContent = blender ? `Blender ${blender}` : "v0.1.0";
  $("stageMode").textContent = state.embedded ? "embedded - native" : active ? "Blender separate" : "no Blender window";
  $("statusLeft").textContent = active ? `${labels[value]} - ${code || "no code"}` : "Ready";
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

    if (user.host) {
      const badge = document.createElement("span");
      badge.className = "host-badge";
      badge.textContent = "host";
      item.append(badge);
    }

    const ms = document.createElement("span");
    ms.className = "ms";
    ms.textContent = user.ping_ms ? `${Math.round(user.ping_ms)} ms` : "";
    item.append(ms);

    list.append(item);
  }
}

function renderMessages(status) {
  const messages = Array.isArray(status?.messages) ? status.messages.slice(-60) : [];
  const key = `${messages.length}:${messages[messages.length - 1]?.text || ""}:${messages[messages.length - 1]?.stamp || ""}`;
  if (key === state.messagesKey) return;
  state.messagesKey = key;

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
  $("embedChip").hidden = !isActive(state.status) || state.embedded;
  $("detachChip").hidden = !state.embedded;
  $("focusBtn").hidden = !state.embedded;
  $("embedBtn").hidden = state.embedded;
  $("detachBtn").hidden = !state.embedded;
  const launchLabel = $("launchLabel");
  if (launchLabel) launchLabel.textContent = state.embedded ? "Focus Blender" : "Launch Blender";
  $("stageMode").textContent = state.embedded ? "embedded - native" : isActive(state.status) ? "Blender separate" : "no Blender window";
}

function applyStatus(status) {
  if (!status || typeof status !== "object") return;
  state.status = status;
  const error = String(status.last_error || "").trim();
  if (error && error !== state.lastError) {
    state.lastError = error;
    log(`addon error: ${error}`);
    toast(error, "error");
  } else if (!error) {
    state.lastError = "";
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
    const existing = await invoke("find_blender_window").catch(() => null);
    if (existing) {
      log("reusing the running Blender window");
      if (state.embedded) await invoke("focus_blender").catch(() => {});
      else await embedNow();
      return;
    }
    const pid = await invoke("launch_blender", { path: settings.blender, blendFile: null });
    log(`launch_blender ok pid=${pid}`);
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
  try {
    await request({
      action: "host",
      name: settings.name,
      port: settings.port,
      pin: settings.pin || null,
      relay: settings.useRelay ? settings.relay : null,
      use_relay: settings.useRelay,
    });
    toast("Room starting...", "ok");
    await launchBlender();
  } catch (error) {
    toast(String(error), "error");
  }
}

async function joinRoom() {
  const code = groupCode($("joinCode").value);
  const name = $("joinName").value.trim() || settings.name;
  const pin = $("joinPin").value;
  if (normalizeCode(code).length !== 15) {
    toast("That room code is incomplete", "error");
    return;
  }
  settings.name = name;
  store.set("name", name);
  try {
    await request({ action: "join", code, name, pin: pin || null });
    toast("Joining room...", "ok");
    setPanel("session");
    await launchBlender();
  } catch (error) {
    toast(String(error), "error");
  }
}

async function leaveRoom() {
  try {
    await request({ action: "leave" });
    toast("Leaving room...");
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 200));
      const status = await poll();
      if (!status || !isActive(status)) {
        toast("Left the room");
        return;
      }
    }
    toast("Blender did not disconnect. Close Blender, then rejoin.", "error");
  } catch (error) {
    toast(String(error), "error");
  }
}

async function embedNow() {
  try {
    const box = insets();
    await invoke("embed_blender", {
      xPx: box.x,
      yPx: box.y,
      widthPx: box.width,
      heightPx: box.height,
      viewportW: box.viewportW,
      viewportH: box.viewportH,
    });
    state.embedded = true;
    renderEmbed();
    toast("Blender docked", "ok");
  } catch (error) {
    toast(String(error), "error");
  }
}

async function detachNow() {
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

async function copyCode() {
  const code = $("roomCode").textContent;
  if (!code || code === "-") return;
  try {
    await navigator.clipboard.writeText(code);
    toast("Room code copied", "ok");
  } catch {
    const range = document.createRange();
    range.selectNodeContents($("roomCode"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast("Select and copy the code");
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

async function embedLoop() {
  if (!isTauri || state.embedBusy) return;
  state.embedBusy = true;
  try {
    if (!isActive(state.status)) {
      if (state.embedded) {
        await invoke("detach_blender").catch(() => {});
        state.embedded = false;
        renderEmbed();
      }
      return;
    }
    if (state.embedded) {
      await updateBounds();
      return;
    }
    const found = await invoke("find_blender_window").catch(() => null);
    if (!found) return;
    const box = insets();
    await invoke("embed_blender", {
      xPx: box.x,
      yPx: box.y,
      widthPx: box.width,
      heightPx: box.height,
      viewportW: box.viewportW,
      viewportH: box.viewportH,
    });
    state.embedded = true;
    renderEmbed();
    log("embed ok");
  } catch (error) {
    log(`embed skipped: ${error}`);
  } finally {
    state.embedBusy = false;
  }
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  $("hostBtn").addEventListener("click", hostRoom);
  $("joinBtn").addEventListener("click", () => setPanel("join"));
  $("joinClose").addEventListener("click", () => setPanel("session"));
  $("joinGo").addEventListener("click", joinRoom);
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
  $("copyBtn").addEventListener("click", copyCode);
  $("chatForm").addEventListener("submit", sendChat);
  $("chatClose").addEventListener("click", () => window.BMPLayout?.setTile("chat", false));
  $("collapseLeft").addEventListener("click", () => window.BMPLayout?.setTile("session", false));
  $("refreshBtn").addEventListener("click", poll);
  $("detectBtn").addEventListener("click", detectBlender);
  $("saveSettings").addEventListener("click", () => {
    settings.name = $("setName").value.trim() || settings.name;
    settings.port = Math.max(1024, Math.min(65535, Number($("setPort").value) || 51999));
    settings.pin = $("setPin").value;
    settings.relay = $("setRelay").value.trim();
    settings.useRelay = $("setUseRelay").checked;
    settings.blender = $("setBlender").value.trim();
    store.set("name", settings.name);
    store.set("port", settings.port);
    store.set("pin", settings.pin);
    store.set("relay", settings.relay);
    store.set("useRelay", settings.useRelay ? "1" : "0");
    store.set("blender", settings.blender);
    $("meAvatar").textContent = initials(settings.name);
    toast("Settings saved", "ok");
    setPanel("session");
  });

  $("joinCode").addEventListener("input", (event) => {
    const caret = event.target.selectionStart;
    event.target.value = groupCode(event.target.value);
    if (caret !== null) event.target.setSelectionRange(event.target.value.length, event.target.value.length);
  });

  $("joinCode").addEventListener("keydown", (event) => {
    if (event.key === "Enter") joinRoom();
  });
  $("joinPin").addEventListener("keydown", (event) => {
    if (event.key === "Enter") joinRoom();
  });
  $("setPort").addEventListener("change", () => {
    settings.port = Math.max(1024, Math.min(65535, Number($("setPort").value) || 51999));
  });

  window.addEventListener("resize", scheduleBounds);
}

async function boot() {
  wire();
  window.BMPLayout?.init({
    content: {
      session: $("tileSession"),
      viewport: $("tileViewport"),
      chat: $("tileChat"),
    },
    onBounds: scheduleBounds,
    setBlenderVisible: (visible) => invoke("set_blender_visible", { visible }).catch(() => {}),
    getPanel: () => state.panel,
    setPanel,
  });
  markBridge();
  setPanel("session");
  $("meAvatar").textContent = initials(settings.name);
  $("joinName").value = settings.name;
  $("setName").value = settings.name;
  $("setPort").value = settings.port;
  $("setPin").value = settings.pin;
  $("setRelay").value = settings.relay;
  $("setUseRelay").checked = settings.useRelay;
  $("setBlender").value = settings.blender;

  if (state.demo) {
    applyStatus(demoStatus);
    renderEmbed();
    return;
  }

  await detectBlender();
  await poll();
  setInterval(poll, 750);
  setInterval(embedLoop, 1200);

  try {
    const flags = await invoke("startup_flags");
    if (flags && flags.autohost) await hostRoom();
    if (flags && flags.autojoin) {
      $("joinCode").value = groupCode(flags.autojoin);
      await joinRoom();
    }
  } catch {
    /* flags are optional */
  }
}

boot();
})();
