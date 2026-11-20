// Blendermp hub: public directory + WebSocket relay on Cloudflare's free tier.
// Deploy with: npx wrangler deploy
// Then put https://<worker>.workers.dev in the app's Hub URL field on every PC.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "no-store",
};

const TTL = 60;
const MAX_SERVERS = 500;
const MAX_BODY = 8 * 1024;
const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const CODE_LEN = 15;

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

function empty(status = 204) {
  return new Response(null, { status, headers: CORS });
}

function now() {
  return Math.floor(Date.now() / 1000);
}

function cleanText(value, limit, required) {
  if (value === undefined || value === null) {
    if (required) throw new Error("missing text");
    return "";
  }
  if (typeof value !== "string") throw new Error("text must be a string");
  const text = value.trim();
  if (required && !text) throw new Error("text is empty");
  if (text.length > limit || /[\x00-\x1f\x7f]/.test(text)) throw new Error("text is invalid");
  return text;
}

function cleanCode(value) {
  if (typeof value !== "string") throw new Error("room code is required");
  const code = value
    .toUpperCase()
    .split("")
    .filter((ch) => /[0-9A-Z]/.test(ch))
    .join("");
  if (code.length !== CODE_LEN || [...code].some((ch) => !CROCKFORD.includes(ch))) {
    throw new Error("room code is invalid");
  }
  return code;
}

function cleanInt(value, fallback, low, high) {
  if (value === undefined || value === null) return fallback;
  if (!Number.isInteger(value)) throw new Error("expected an integer");
  if (value < low || value > high) throw new Error("number out of range");
  return value;
}

function cleanToken(value) {
  if (typeof value !== "string") return "";
  const token = value.trim();
  if (token.length < 32 || token.length > 128 || !/^[A-Za-z0-9_-]+$/.test(token)) return "";
  return token;
}

async function hashToken(token) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function roomIdFromCode(code) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(code));
  const hex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return hex.slice(0, 16);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let index = 0; index < a.length; index += 1) {
    diff |= a.charCodeAt(index) ^ b.charCodeAt(index);
  }
  return diff === 0;
}

async function binaryData(data) {
  if (data instanceof ArrayBuffer) return data;
  if (ArrayBuffer.isView(data)) {
    return data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
  }
  if (data && typeof data.arrayBuffer === "function") {
    try { return await data.arrayBuffer(); } catch { return null; }
  }
  return null;
}

function validId(value) {
  return typeof value === "string" && value.length >= 16 && value.length <= 64 && /^[A-Za-z0-9_-]+$/.test(value);
}

async function readJson(request) {
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_BODY) throw new Error("request body is too large");
  try {
    return await request.json();
  } catch {
    throw new Error("invalid JSON");
  }
}

export default {
  async fetch(request, env) {
    const id = env.HUB.idFromName("hub");
    return env.HUB.get(id).fetch(request);
  },
};

export class Hub {
  constructor(state) {
    this.state = state;
    this.rooms = new Map();
    this.tokens = new Map();
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return empty();
    if (url.pathname.startsWith("/relay/")) return this.relay(request, url);
    try {
      if (url.pathname === "/health" && request.method === "GET") {
        const servers = await this.loadServers();
        return json({ ok: true, servers: Object.keys(servers).length });
      }
      if (url.pathname === "/servers" && request.method === "GET") {
        const servers = await this.loadServers();
        const query = (url.searchParams.get("q") || "").trim().toLowerCase();
        const limit = Math.min(200, Math.max(1, Number(url.searchParams.get("limit")) || 100));
        const unique = new Map();
        for (const entry of Object.values(servers)) {
          if (!entry || !entry.code) continue;
          const existing = unique.get(entry.code);
          if (!existing || entry.seen > existing.seen) unique.set(entry.code, entry);
        }
        const list = [...unique.values()]
          .filter((entry) => !query || entry.name.toLowerCase().includes(query) || entry.host.toLowerCase().includes(query))
          .sort((a, b) => b.players - a.players || a.name.localeCompare(b.name))
          .slice(0, limit)
          .map(({ id, seen, ...entry }) => entry);
        return json({ servers: list });
      }
      if (url.pathname === "/announce" && request.method === "POST") {
        const body = await readJson(request);
        if (!body || typeof body !== "object") return json({ error: "expected an object" }, 400);
        if (!validId(body.id)) return json({ error: "invalid server id" }, 400);
        const entry = {
          id: body.id,
          name: cleanText(body.name, 48, true),
          host: cleanText(body.host, 48, false),
          code: cleanCode(body.code),
          players: cleanInt(body.players, 1, 1, 64),
          max: cleanInt(body.max, 16, 2, 64),
          password: Boolean(body.password),
          relay: Boolean(body.relay),
          version: cleanText(body.version, 16, false),
          protocol: cleanInt(body.protocol, 0, 0, 999),
          caps: cleanInt(body.caps, 0, 0, 2147483647),
          min_app: cleanText(body.min_app, 16, false),
          relay_url: cleanText(body.relay_url, 256, false),
          seen: now(),
        };
        if (entry.players > entry.max) return json({ error: "player count is above the maximum" }, 400);
        const hostToken = cleanToken(body.host_token);
        if (entry.relay && !hostToken) {
          return json({ error: "relay hosts must announce a host token" }, 400);
        }
        const servers = await this.loadServers();
        if (!servers[entry.id] && Object.keys(servers).length >= MAX_SERVERS) {
          return json({ error: "directory is full" }, 400);
        }
        if (entry.relay) {
          const room = await roomIdFromCode(entry.code);
          const hash = await hashToken(hostToken);
          const existing = this.tokens.get(room);
          if (existing && !timingSafeEqual(existing.hash, hash)) {
            return json({ error: "this room is already owned by another host" }, 409);
          }
          this.tokens.set(room, { hash, id: entry.id, seen: now() });
        }
        servers[entry.id] = entry;
        await this.state.storage.put("servers", servers);
        return json({ ok: true, ttl: TTL });
      }
      if (url.pathname === "/remove" && request.method === "POST") {
        const body = await readJson(request);
        if (!validId(body && body.id)) return json({ error: "invalid server id" }, 400);
        const servers = await this.loadServers();
        const removed = Boolean(servers[body.id]);
        const entry = servers[body.id];
        if (entry && entry.code) {
          try {
            this.tokens.delete(await roomIdFromCode(entry.code));
          } catch {
            /* ignore a malformed stored entry */
          }
        }
        delete servers[body.id];
        await this.state.storage.put("servers", servers);
        return json({ ok: removed });
      }
    } catch (error) {
      return json({ error: String(error.message || error) }, 400);
    }
    return json({ error: "not found" }, 404);
  }

  async loadServers() {
    const stored = (await this.state.storage.get("servers")) || {};
    const cutoff = now() - TTL;
    let changed = false;
    for (const [id, entry] of Object.entries(stored)) {
      if (!entry || entry.seen < cutoff) {
        if (entry && entry.code) {
          try {
            this.tokens.delete(await roomIdFromCode(entry.code));
          } catch {
            /* ignore a malformed stored entry */
          }
        }
        delete stored[id];
        changed = true;
      }
    }
    if (changed) await this.state.storage.put("servers", stored);
    return stored;
  }

  // ------------------------------------------------------------------ relay
  async relay(request, url) {
    const roomId = url.pathname.slice("/relay/".length).toLowerCase();
    if (!/^[0-9a-f]{16}$/.test(roomId)) return new Response("bad room", { status: 400, headers: CORS });
    const role = url.searchParams.get("role");
    if (role !== "host" && role !== "join") return new Response("bad role", { status: 400, headers: CORS });
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("websocket required", { status: 426, headers: CORS });
    }
    if (role === "host") {
      const record = this.tokens.get(roomId);
      if (!record) return new Response("host token required", { status: 403, headers: CORS });
      const supplied = request.headers.get("x-bmp-host-token") || "";
      if (!timingSafeEqual(await hashToken(supplied), record.hash)) {
        return new Response("invalid host token", { status: 403, headers: CORS });
      }
    }

    let room = this.rooms.get(roomId);
    if (!room) {
      room = { host: null, clients: new Map(), next: 1 };
      this.rooms.set(roomId, room);
    }

    if (role === "join" && !room.host) {
      return new Response("no host", { status: 503, headers: CORS });
    }

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.accept();

    if (role === "host") {
      if (room.host) {
        try { room.host.close(1000, "replaced"); } catch { /* ignore */ }
        for (const socket of room.clients.values()) {
          try { socket.close(1000, "host replaced"); } catch { /* ignore */ }
        }
        room.clients.clear();
      }
      room.host = server;
      server.addEventListener("message", (event) => {
        void this.hostMessage(room, event.data);
      });
      server.addEventListener("close", () => this.hostClosed(room, server));
      server.addEventListener("error", () => this.hostClosed(room, server));
    } else {
      const stream = room.next++;
      room.clients.set(stream, server);
      this.sendFrame(room.host, 1, stream, new ArrayBuffer(0));
      server.addEventListener("message", (event) => {
        void (async () => {
          const buffer = await binaryData(event.data);
          if (buffer) this.sendFrame(room.host, 2, stream, buffer);
        })();
      });
      const close = () => {
        room.clients.delete(stream);
        this.sendFrame(room.host, 3, stream, new ArrayBuffer(0));
      };
      server.addEventListener("close", close);
      server.addEventListener("error", close);
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  async hostMessage(room, data) {
    const buffer = await binaryData(data);
    if (!buffer || buffer.byteLength < 9) return;
    const view = new DataView(buffer);
    const type = view.getUint8(0);
    const stream = view.getUint32(1, true);
    const length = view.getUint32(5, true);
    if (buffer.byteLength < 9 + length) return;
    const payload = buffer.slice(9, 9 + length);
    const client = room.clients.get(stream);
    if (!client) return;
    if (type === 2) {
      try { client.send(payload); } catch { /* ignore */ }
    } else if (type === 3) {
      try { client.close(1000, "closed"); } catch { /* ignore */ }
      room.clients.delete(stream);
    }
  }

  hostClosed(room, socket) {
    if (room.host !== socket) return;
    room.host = null;
    for (const client of room.clients.values()) {
      try { client.close(1000, "host left"); } catch { /* ignore */ }
    }
    room.clients.clear();
    this.rooms.delete([...this.rooms.entries()].find(([, value]) => value === room)?.[0]);
  }

  sendFrame(socket, type, stream, payload) {
    if (!socket) return;
    const length = payload.byteLength || 0;
    const frame = new ArrayBuffer(9 + length);
    const view = new DataView(frame);
    view.setUint8(0, type);
    view.setUint32(1, stream, true);
    view.setUint32(5, length, true);
    if (length) new Uint8Array(frame, 9).set(new Uint8Array(payload));
    try { socket.send(frame); } catch { /* ignore */ }
  }
}
