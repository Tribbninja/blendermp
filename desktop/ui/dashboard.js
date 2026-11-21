/* Detached Blendermp dashboard. Reads the same status.json through the Tauri
 * command bridge and sends the same control requests as the main window.
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const internals = window.__TAURI_INTERNALS__;
  const isTauri = Boolean(internals && internals.invoke);
  const httpMode = !isTauri && /^https?:$/.test(location.protocol);
  const dashboardToken = new URLSearchParams(location.search).get("token") || "";
  const dashboardMode =
    Boolean(window.__BMP_DASHBOARD__) ||
    location.pathname.endsWith("/dashboard.html") ||
    location.pathname.includes("/dashboard") ||
    new URLSearchParams(location.search).get("dashboard") === "1";
  if (!dashboardMode) return;
  if (!$("dashState")) {
    document.title = "Blendermp dashboard";
    document.body.className = "dashboard-body";
    document.body.innerHTML = `
      <main class="dashboard">
        <header class="dash-head">
          <div>
            <div class="wordmark">Blendermp <span class="tag">dashboard</span></div>
            <div class="statusline">
              <span class="dot" id="dashDot"></span>
              <span id="dashState">Offline</span>
              <span class="sep">/</span>
              <span id="dashRoom">no room</span>
            </div>
          </div>
          <div class="head-actions">
            <button class="mini-btn" id="dashFocus" type="button">Focus Blender</button>
            <button class="mini-btn danger" id="dashLeave" type="button">Leave</button>
          </div>
        </header>
        <section class="dash-stats">
          <div><b id="dashUsers">0</b><small>people</small></div>
          <div><b id="dashLeases">0</b><small>leases</small></div>
          <div><b id="dashQueue">0</b><small>queued</small></div>
          <div><b id="dashPing">-</b><small>ms</small></div>
        </section>
        <section class="dash-card">
          <div class="section-head"><span>People</span><span id="dashRole">editor</span></div>
          <ul class="members" id="dashPeople"><li class="empty">No one here yet.</li></ul>
        </section>
        <section class="dash-card">
          <div class="section-head"><span>Edit leases</span><span id="dashLeaseCount">0</span></div>
          <ul class="dash-list" id="dashLeasesList"><li class="empty">No active leases.</li></ul>
        </section>
        <section class="dash-card">
          <div class="section-head"><span>Sync queue</span><span>per peer</span></div>
          <ul class="dash-list" id="dashSync"><li class="empty">No peer telemetry yet.</li></ul>
        </section>
        <section class="dash-card">
          <div class="section-head"><span>Session tools</span><span id="dashRecording">idle</span></div>
          <ul class="dash-list" id="dashTools"><li class="empty">Start the live view or bridge in Blender.</li></ul>
        </section>
        <section class="dash-card dash-history">
          <div class="section-head"><span>Recent chat</span><span id="dashHistoryCount">0</span></div>
          <div class="messages" id="dashMessages"><div class="message empty">No messages yet.</div></div>
          <form class="composer" id="dashChatForm">
            <input id="dashChatInput" placeholder="Message the room..." maxlength="2000" autocomplete="off" />
            <button class="send" type="submit" title="Send">
              <svg viewBox="0 0 24 24"><path d="M4 12l16-8-6 16-2.5-6.5L4 12z" /></svg>
            </button>
          </form>
        </section>
        <section class="dash-card">
          <div class="section-head"><span>Notifications</span><span id="dashDndLabel">on</span></div>
          <label class="check"><input type="checkbox" id="dashDnd" /><span>Do not disturb</span></label>
          <p class="fine">Chat, joins, leases and errors are mirrored from Blender.</p>
        </section>
        <section class="dash-card">
          <div class="section-head"><span>Accessibility</span><span>shared with main app</span></div>
          <label class="field">
            <span>Contrast</span>
            <select id="dashContrast">
              <option value="normal">Normal</option>
              <option value="high">High contrast</option>
            </select>
          </label>
          <label class="field">
            <span>Text size</span>
            <select id="dashFont">
              <option value="normal">Normal</option>
              <option value="large">Large</option>
            </select>
          </label>
          <label class="check"><input type="checkbox" id="dashMotion" /><span>Reduce motion</span></label>
        </section>
        <div class="dash-toasts" id="dashToasts"></div>
      </main>`;
  }
  const state = {
    status: null,
    name: "",
    lastMessage: "",
    lastState: "",
    lastError: "",
    audio: null,
  };

  async function invoke(command, args = {}) {
    if (isTauri) return internals.invoke(command, args);
    if (!httpMode) throw new Error("dashboard requires the desktop app");
    const suffix = `?token=${encodeURIComponent(dashboardToken)}`;
    if (command === "read_status") {
      const response = await fetch(`/dashboard/status${suffix}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`status ${response.status}`);
      return response.json();
    }
    if (command === "read_settings") {
      const response = await fetch(`/dashboard/settings${suffix}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`settings ${response.status}`);
      return response.json();
    }
    if (command === "read_prefs") {
      const response = await fetch(`/dashboard/prefs${suffix}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`preferences ${response.status}`);
      return response.json();
    }
    if (command === "write_prefs") {
      const response = await fetch(`/dashboard/prefs${suffix}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(args.prefs || {}),
      });
      if (!response.ok) throw new Error(`preferences ${response.status}`);
      return response.json();
    }
    if (command === "write_session") {
      const response = await fetch(`/dashboard/command${suffix}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(args.request || {}),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `command ${response.status}`);
      return body;
    }
    if (command === "log_client") return null;
    if (command === "focus_blender") throw new Error("focus Blender from the main app");
    throw new Error(`unsupported dashboard command: ${command}`);
  }

  function toast(message, kind = "") {
    const host = $("dashToasts");
    const item = document.createElement("div");
    item.className = `toast ${kind}`.trim();
    item.textContent = message;
    host.prepend(item);
    while (host.children.length > 3) host.lastElementChild.remove();
    setTimeout(() => item.remove(), 4200);
  }

  function messageKey(message) {
    return `${message.stamp || ""}|${message.author || ""}|${message.text || ""}`;
  }

  function colorOf(user) {
    const color = Array.isArray(user.color) && user.color.length >= 3 ? user.color : [0.4, 0.8, 1];
    const channel = (value) => Math.max(0, Math.min(255, Math.round(Number(value) * 255) || 0));
    return `rgb(${channel(color[0])}, ${channel(color[1])}, ${channel(color[2])})`;
  }

  function initials(name) {
    return (name || "?").slice(0, 2).toUpperCase();
  }

  function renderPeople(status) {
    const list = $("dashPeople");
    list.replaceChildren();
    const users = Array.isArray(status.users) ? status.users : [];
    $("dashUsers").textContent = String(users.length);
    if (!users.length) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "No one here yet.";
      list.append(empty);
      return;
    }
    for (const user of users) {
      const item = document.createElement("li");
      const avatar = document.createElement("span");
      avatar.className = "mini";
      avatar.style.background = colorOf(user);
      avatar.textContent = initials(user.name);
      item.append(avatar);
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = user.name;
      item.append(who);
      if (user.host) {
        const host = document.createElement("span");
        host.className = "host-badge";
        host.textContent = "host";
        item.append(host);
      }
      if (user.role && status.is_host && user.name !== status.name) {
        const select = document.createElement("select");
        select.className = "role-select";
        for (const role of ["viewer", "commenter", "editor", "admin"]) {
          const option = document.createElement("option");
          option.value = role;
          option.textContent = role;
          if (role === user.role) option.selected = true;
          select.append(option);
        }
        select.addEventListener("change", () => request({ action: "role", target: user.name, role: select.value }));
        item.append(select);
        const kick = document.createElement("button");
        kick.className = "mini-btn";
        kick.type = "button";
        kick.textContent = "Kick";
        kick.addEventListener("click", () => request({ action: "kick", target: user.name }));
        item.append(kick);
      } else {
        const role = document.createElement("span");
        role.className = "ms";
        role.textContent = user.role || "";
        item.append(role);
      }
      const ping = document.createElement("span");
      ping.className = "ms";
      ping.textContent = user.ping_ms ? `${user.ping_ms} ms` : "";
      item.append(ping);
      list.append(item);
    }
  }

  function renderLeases(status) {
    const list = $("dashLeasesList");
    list.replaceChildren();
    const leases = Array.isArray(status.leases) ? status.leases : [];
    $("dashLeases").textContent = String(leases.length);
    $("dashLeaseCount").textContent = String(leases.length);
    if (!leases.length) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "No active leases.";
      list.append(empty);
      return;
    }
    for (const lease of leases) {
      const item = document.createElement("li");
      const name = document.createElement("b");
      name.textContent = lease.object || "object";
      item.append(name);
      const owner = document.createElement("span");
      owner.textContent = lease.owner || "unowned";
      item.append(owner);
      if (lease.requests?.length) {
        const requests = document.createElement("span");
        requests.className = "warn";
        requests.textContent = `${lease.requests.join(", ")} waiting`;
        item.append(requests);
      }
      list.append(item);
    }
  }

  function renderSync(status) {
    const list = $("dashSync");
    list.replaceChildren();
    const peers = Array.isArray(status.sync) ? status.sync : [];
    const total = peers.reduce((sum, peer) => sum + (Number(peer.queue) || 0) + (Number(peer.pending) || 0), 0);
    $("dashQueue").textContent = String(total);
    if (!peers.length) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "No peer telemetry yet.";
      list.append(empty);
      return;
    }
    for (const peer of peers) {
      const item = document.createElement("li");
      const name = document.createElement("b");
      name.textContent = peer.name || "peer";
      item.append(name);
      const detail = document.createElement("span");
      detail.textContent = `queue ${peer.queue || 0} / pending ${peer.pending || 0} / quality ${peer.quality || 0}`;
      item.append(detail);
      list.append(item);
    }
  }

  function renderTools(status) {
    const list = $("dashTools");
    list.replaceChildren();
    $("dashRecording").textContent = status.recording ? "recording" : "idle";
    const rows = [];
    if (status.broadcast_url) {
      const link = document.createElement("a");
      link.href = status.broadcast_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "Open live view";
      rows.push(link);
    }
    if (status.bridge_url) {
      const text = document.createElement("span");
      text.textContent = `Bridge: ${status.bridge_url} (${Number(status.bridge_clients) || 0} client(s))`;
      rows.push(text);
    }
    if (status.recording) {
      const text = document.createElement("span");
      text.textContent = `Recording: ${status.recording}`;
      rows.push(text);
    }
    if (status.farm_url) {
      const text = document.createElement("span");
      text.textContent = `Farm: ${status.farm_url} (${Number(status.farm_workers) || 0} worker(s)) ${status.farm_progress || ""}`;
      rows.push(text);
    }
    if (status.internet_mode) {
      const text = document.createElement("span");
      text.textContent = "Internet mode: reduced presence and interest updates";
      rows.push(text);
    }
    const budgets = status.perf && status.perf.budgets;
    if (budgets && typeof budgets === "object") {
      const parts = Object.entries(budgets)
        .slice(0, 6)
        .map(([name, item]) => `${name} ${Number(item.p95_ms || 0).toFixed(1)} ms`);
      if (parts.length) {
        const text = document.createElement("span");
        text.textContent = `p95: ${parts.join(" · ")}`;
        rows.push(text);
      }
    }
    if (!rows.length) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "Start the live view or bridge in Blender.";
      list.append(empty);
      return;
    }
    for (const row of rows) {
      const item = document.createElement("li");
      item.append(row);
      list.append(item);
    }
  }

  function renderMessages(status) {
    const list = $("dashMessages");
    list.replaceChildren();
    const messages = Array.isArray(status.messages) ? status.messages : [];
    $("dashHistoryCount").textContent = String(messages.length);
    if (!messages.length) {
      const empty = document.createElement("div");
      empty.className = "message empty";
      empty.textContent = "No messages yet.";
      list.append(empty);
      return;
    }
    for (const message of messages) {
      const item = document.createElement("div");
      item.className = "message";
      const meta = document.createElement("div");
      meta.className = "meta";
      const author = document.createElement("b");
      author.textContent = message.author || "?";
      meta.append(author);
      const stamp = document.createElement("span");
      stamp.textContent = message.stamp || "";
      meta.append(stamp);
      item.append(meta);
      const text = document.createElement("div");
      text.textContent = message.text || "";
      item.append(text);
      list.append(item);
    }
    list.scrollTop = list.scrollHeight;
  }

  function applyStatus(status) {
    state.status = status || null;
    const current = status || { state: "offline" };
    $("dashState").textContent = String(current.state || "offline");
    $("dashDot").className = `dot ${current.state === "connected" || current.state === "hosting" ? "on" : current.state === "joining" ? "busy" : ""}`;
    $("dashRoom").textContent = current.code || "no room";
    $("dashRole").textContent = current.role || "editor";
    $("dashPing").textContent = current.rtt_ms ? String(Math.round(current.rtt_ms)) : "-";
    renderPeople(current);
    renderLeases(current);
    renderSync(current);
    renderTools(current);
    renderMessages(current);

    const messages = Array.isArray(current.messages) ? current.messages : [];
    const latest = messages.length ? messageKey(messages[messages.length - 1]) : "";
    if (latest && state.lastMessage && latest !== state.lastMessage) {
      const last = messages[messages.length - 1];
      if (last.author && last.author !== current.name && !window.BMPA11y?.dnd) {
        toast(`${last.author}: ${String(last.text || "").slice(0, 120)}`);
      }
    }
    if (latest) state.lastMessage = latest;
    if (current.state && state.lastState && current.state !== state.lastState && !window.BMPA11y?.dnd) {
      toast(`Session ${current.state}`, current.state === "offline" ? "error" : "ok");
    }
    if (current.state) state.lastState = current.state;
    const error = String(current.last_error || "");
    if (error && error !== state.lastError) {
      toast(error.slice(0, 160), "error");
      state.lastError = error;
    } else if (!error) {
      state.lastError = "";
    }
  }

  async function poll() {
    if (!isTauri && !httpMode) {
      applyStatus({ state: "offline" });
      return;
    }
    if (document.visibilityState === "hidden") return;
    try {
      applyStatus(await invoke("read_status"));
    } catch (error) {
      toast(String(error), "error");
    }
  }

  async function request(body) {
    try {
      await invoke("write_session", { request: body });
    } catch (error) {
      toast(String(error), "error");
    }
  }

  async function loadPrefs() {
    if (!httpMode) return;
    try {
      const prefs = await invoke("read_prefs");
      if (!prefs || typeof prefs !== "object") return;
      if (prefs.contrast) window.BMPA11y?.set("contrast", prefs.contrast);
      if (prefs.fontScale) window.BMPA11y?.set("font", prefs.fontScale);
      if (prefs.reducedMotion) window.BMPA11y?.set("motion", prefs.reducedMotion);
      if (prefs.sound) window.BMPA11y?.set("sound", prefs.sound);
      if (prefs.dnd) window.BMPA11y?.set("dnd", prefs.dnd);
    } catch {
      /* preferences are optional */
    }
  }

  function persistPrefs() {
    if (!httpMode) return;
    invoke("write_prefs", {
      prefs: {
        contrast: window.BMPA11y?.contrast || "normal",
        fontScale: window.BMPA11y?.font || "normal",
        reducedMotion: window.BMPA11y?.reducedMotion ? "1" : "0",
        sound: window.BMPA11y?.sound ? "1" : "0",
        dnd: window.BMPA11y?.dnd ? "1" : "0",
      },
    }).catch(() => {});
  }

  async function loadSettings() {
    try {
      const settings = await invoke("read_settings");
      if (settings && typeof settings.name === "string") state.name = settings.name;
    } catch {
      /* optional */
    }
  }

  function bind() {
    if (!isTauri) $("dashFocus").hidden = true;
    $("dashFocus").addEventListener("click", () => invoke("focus_blender").catch((error) => toast(String(error), "error")));
    $("dashLeave").addEventListener("click", () => request({ action: "leave" }));
    $("dashChatForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = $("dashChatInput").value.trim();
      if (!text) return;
      await request({ action: "chat", text });
      $("dashChatInput").value = "";
    });

    const dnd = $("dashDnd");
    const contrast = $("dashContrast");
    const font = $("dashFont");
    const motion = $("dashMotion");
    const syncA11y = () => {
      dnd.checked = window.BMPA11y?.dnd || false;
      contrast.value = window.BMPA11y?.contrast || "normal";
      font.value = window.BMPA11y?.font || "normal";
      motion.checked = window.BMPA11y?.reducedMotion || false;
      $("dashDndLabel").textContent = dnd.checked ? "off" : "on";
    };
    syncA11y();
    dnd.addEventListener("change", () => {
      window.BMPA11y?.set("dnd", dnd.checked ? "1" : "0");
      persistPrefs();
      syncA11y();
    });
    contrast.addEventListener("change", () => {
      window.BMPA11y?.set("contrast", contrast.value);
      persistPrefs();
    });
    font.addEventListener("change", () => {
      window.BMPA11y?.set("font", font.value);
      persistPrefs();
    });
    motion.addEventListener("change", () => {
      window.BMPA11y?.set("motion", motion.checked ? "1" : "0");
      persistPrefs();
    });
    document.addEventListener("bmp-a11y", syncA11y);
    window.addEventListener("storage", syncA11y);
  }

  async function boot() {
    if (isTauri) {
      invoke("log_client", { message: "dashboard boot" }).catch(() => {});
    }
    await loadPrefs();
    bind();
    await loadSettings();
    await poll();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") poll();
    });
    setInterval(poll, 750);
  }

  boot();
})();
