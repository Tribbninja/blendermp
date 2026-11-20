//! Small self-hosted public server directory.
//!
//! The desktop app runs this in-process, so a host does not need a separate
//! Python/VPS service just to make a server visible. It listens on TCP 7101
//! (falling back to an ephemeral port) and answers LAN discovery queries on
//! UDP 5598 with its actual port. The same HTTP API is used by the optional
//! standalone `server/directory.py` for internet-wide lists.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream, UdpSocket};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const HTTP_PORT: u16 = 7101;
const DISCOVERY_PORT: u16 = 5598;
const DISCOVERY_MAGIC: &str = "BMPDIR1";
const TTL: Duration = Duration::from_secs(90);
const MAX_BODY: usize = 8 * 1024;
const MAX_SERVERS: usize = 500;
const CROCKFORD: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

#[derive(Clone)]
struct Entry {
    id: String,
    name: String,
    host: String,
    code: String,
    players: u64,
    max: u64,
    password: bool,
    relay: bool,
    version: String,
    direct_code: String,
    identity_public: String,
    identity_signature: String,
    identity_descriptor: Value,
    seen: Instant,
}

pub struct DirectoryHandle {
    pub port: u16,
}

fn clean_text(value: Option<&Value>, limit: usize, required: bool) -> Result<String, String> {
    let Some(value) = value else {
        return if required { Err("missing text".into()) } else { Ok(String::new()) };
    };
    let Some(text) = value.as_str() else {
        return Err("text must be a string".into());
    };
    let text = text.trim();
    if required && text.is_empty() {
        return Err("text is empty".into());
    }
    if text.len() > limit || text.chars().any(|ch| ch.is_control()) {
        return Err("text is invalid".into());
    }
    Ok(text.to_string())
}

fn clean_code(value: Option<&Value>) -> Result<String, String> {
    let Some(text) = value.and_then(Value::as_str) else {
        return Err("room code is required".into());
    };
    let code: String = text
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .map(|ch| ch.to_ascii_uppercase())
        .collect();
    if code.len() != 15 || !code.chars().all(|ch| CROCKFORD.contains(ch)) {
        return Err("room code is invalid".into());
    }
    Ok(code)
}

fn clean_int(value: Option<&Value>, fallback: u64, low: u64, high: u64) -> Result<u64, String> {
    let Some(value) = value else { return Ok(fallback) };
    let Some(number) = value.as_u64() else {
        return Err("expected an integer".into());
    };
    if !(low..=high).contains(&number) {
        return Err("number out of range".into());
    }
    Ok(number)
}

fn valid_id(value: &str) -> bool {
    (16..=64).contains(&value.len())
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-')
}

fn parse_entry(body: &Value) -> Result<Entry, String> {
    let Some(object) = body.as_object() else {
        return Err("expected a JSON object".into());
    };
    let id = object
        .get("id")
        .and_then(Value::as_str)
        .ok_or("invalid server id")?
        .to_string();
    if !valid_id(&id) {
        return Err("invalid server id".into());
    }
    let name = clean_text(object.get("name"), 48, true)?;
    let host = clean_text(object.get("host"), 48, false)?;
    let code = clean_code(object.get("code"))?;
    let players = clean_int(object.get("players"), 1, 1, 64)?;
    let maximum = clean_int(object.get("max"), 16, 2, 64)?;
    if players > maximum {
        return Err("player count is above the maximum".into());
    }
    let password = object.get("password").and_then(Value::as_bool).unwrap_or(false);
    let relay = object.get("relay").and_then(Value::as_bool).unwrap_or(false);
    let version = clean_text(object.get("version"), 16, false)?;
    let direct_code = match object.get("direct_code").and_then(Value::as_str) {
        Some(value) if !value.trim().is_empty() => clean_code(Some(&Value::String(value.to_string())))?,
        _ => String::new(),
    };
    let identity_public = clean_text(object.get("identity_public"), 128, false)?;
    let identity_signature = clean_text(object.get("identity_signature"), 512, false)?;
    let identity_descriptor = object
        .get("identity_descriptor")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or(Value::Null);
    Ok(Entry {
        id,
        name,
        host,
        code,
        players,
        max: maximum,
        password,
        relay,
        version,
        direct_code,
        identity_public,
        identity_signature,
        identity_descriptor,
        seen: Instant::now(),
    })
}

fn prune(entries: &mut HashMap<String, Entry>) {
    entries.retain(|_, entry| entry.seen.elapsed() <= TTL);
}

fn public_list(
    entries: &Arc<Mutex<HashMap<String, Entry>>>,
    query: &str,
    limit: usize,
) -> Vec<Value> {
    let needle = query.trim().to_lowercase();
    let mut guard = match entries.lock() {
        Ok(guard) => guard,
        Err(_) => return Vec::new(),
    };
    prune(&mut guard);
    let mut values: Vec<&Entry> = guard
        .values()
        .filter(|entry| {
            needle.is_empty()
                || entry.name.to_lowercase().contains(&needle)
                || entry.host.to_lowercase().contains(&needle)
        })
        .collect();
    values.sort_by(|a, b| b.players.cmp(&a.players).then_with(|| a.name.cmp(&b.name)));
    values
        .into_iter()
        .take(limit.clamp(1, 200))
        .map(|entry| {
            json!({
                "name": entry.name,
                "host": entry.host,
                "code": entry.code,
                "players": entry.players,
                "max": entry.max,
                "password": entry.password,
                "relay": entry.relay,
                "version": entry.version,
                "direct_code": entry.direct_code,
                "identity_public": entry.identity_public,
                "identity_signature": entry.identity_signature,
                "identity_descriptor": entry.identity_descriptor,
                "updated": entry.seen.elapsed().as_secs(),
            })
        })
        .collect()
}

fn url_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => out.push(b' '),
            b'%' if index + 2 < bytes.len() => {
                let hex = &value[index + 1..index + 3];
                if let Ok(byte) = u8::from_str_radix(hex, 16) {
                    out.push(byte);
                    index += 2;
                } else {
                    out.push(b'%');
                }
            }
            byte => out.push(byte),
        }
        index += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

fn http_response(status: &str, body: Option<&Value>) -> Vec<u8> {
    let payload = body
        .map(|value| serde_json::to_vec(value).unwrap_or_else(|_| b"{}".to_vec()))
        .unwrap_or_default();
    let mut head = format!("HTTP/1.1 {status}\r\n");
    if body.is_some() {
        head.push_str("Content-Type: application/json\r\n");
        head.push_str(&format!("Content-Length: {}\r\n", payload.len()));
    }
    head.push_str("Access-Control-Allow-Origin: *\r\n");
    head.push_str("Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n");
    head.push_str("Access-Control-Allow-Headers: Content-Type\r\n");
    head.push_str("Cache-Control: no-store\r\nConnection: close\r\n\r\n");
    let mut bytes = head.into_bytes();
    bytes.extend_from_slice(&payload);
    bytes
}

fn route(
    method: &str,
    target: &str,
    body: &[u8],
    entries: &Arc<Mutex<HashMap<String, Entry>>>,
) -> Vec<u8> {
    if method == "OPTIONS" {
        return http_response("204 No Content", None);
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    match (method, path) {
        ("GET", "/health") => {
            let count = entries
                .lock()
                .map(|mut guard| {
                    prune(&mut guard);
                    guard.len()
                })
                .unwrap_or(0);
            http_response("200 OK", Some(&json!({"ok": true, "servers": count})))
        }
        ("GET", "/servers") => {
            let mut q = String::new();
            let mut limit = 100usize;
            for part in query.split('&') {
                let (key, value) = part.split_once('=').unwrap_or((part, ""));
                if key == "q" {
                    q = url_decode(value);
                } else if key == "limit" {
                    limit = value.parse().unwrap_or(100);
                }
            }
            let servers = public_list(entries, &q, limit);
            http_response("200 OK", Some(&json!({"servers": servers})))
        }
        ("POST", "/announce") => {
            let value: Value = match serde_json::from_slice(body) {
                Ok(value) => value,
                Err(_) => return http_response("400 Bad Request", Some(&json!({"error": "invalid JSON"}))),
            };
            let entry = match parse_entry(&value) {
                Ok(entry) => entry,
                Err(error) => return http_response("400 Bad Request", Some(&json!({"error": error}))),
            };
            if let Ok(mut guard) = entries.lock() {
                prune(&mut guard);
                if guard.len() >= MAX_SERVERS && !guard.contains_key(&entry.id) {
                    return http_response("400 Bad Request", Some(&json!({"error": "directory is full"})));
                }
                guard.insert(entry.id.clone(), entry);
            }
            http_response("200 OK", Some(&json!({"ok": true, "ttl": 60})))
        }
        ("POST", "/remove") => {
            let value: Value = match serde_json::from_slice(body) {
                Ok(value) => value,
                Err(_) => return http_response("400 Bad Request", Some(&json!({"error": "invalid JSON"}))),
            };
            let id = value.get("id").and_then(Value::as_str).unwrap_or("");
            if !valid_id(id) {
                return http_response("400 Bad Request", Some(&json!({"error": "invalid server id"})));
            }
            let removed = entries
                .lock()
                .map(|mut guard| guard.remove(id).is_some())
                .unwrap_or(false);
            http_response("200 OK", Some(&json!({"ok": removed})))
        }
        _ => http_response("404 Not Found", Some(&json!({"error": "not found"}))),
    }
}

fn handle_connection(mut stream: TcpStream, entries: Arc<Mutex<HashMap<String, Entry>>>) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let reader_stream = match stream.try_clone() {
        Ok(stream) => stream,
        Err(_) => return,
    };
    let mut reader = BufReader::new(reader_stream);

    let mut request = String::new();
    if reader.read_line(&mut request).is_err() {
        return;
    }
    let mut parts = request.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("/").to_string();

    let mut content_length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).is_err() {
            return;
        }
        if line == "\r\n" || line == "\n" || line.is_empty() {
            break;
        }
        if let Some((key, value)) = line.split_once(':') {
            if key.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    if content_length > MAX_BODY {
        let _ = stream.write_all(&http_response("413 Payload Too Large", Some(&json!({"error": "body too large"}))));
        return;
    }
    let mut body = vec![0u8; content_length];
    if content_length > 0 && reader.read_exact(&mut body).is_err() {
        return;
    }
    let response = route(&method, &target, &body, &entries);
    let _ = stream.write_all(&response);
}

fn subnet_broadcast() -> Option<String> {
    let probe = UdpSocket::bind(("0.0.0.0", 0)).ok()?;
    probe.connect(("8.8.8.8", 80)).ok()?;
    let address = probe.local_addr().ok()?;
    let text = address.ip().to_string();
    let parts: Vec<&str> = text.split('.').collect();
    if parts.len() == 4 {
        Some(format!("{}.{}.{}.255", parts[0], parts[1], parts[2]))
    } else {
        None
    }
}

pub fn lan_ip() -> String {
    let probe = match UdpSocket::bind(("0.0.0.0", 0)) {
        Ok(socket) => socket,
        Err(_) => return "127.0.0.1".into(),
    };
    if probe.connect(("8.8.8.8", 80)).is_err() {
        return "127.0.0.1".into();
    }
    probe
        .local_addr()
        .map(|address| address.ip().to_string())
        .unwrap_or_else(|_| "127.0.0.1".into())
}

pub fn discover() -> Vec<Value> {
    let Ok(socket) = UdpSocket::bind(("0.0.0.0", 0)) else {
        return Vec::new();
    };
    let _ = socket.set_broadcast(true);
    let _ = socket.set_read_timeout(Some(Duration::from_millis(120)));
    let targets = [
        format!("255.255.255.255:{DISCOVERY_PORT}"),
        subnet_broadcast()
            .map(|address| format!("{address}:{DISCOVERY_PORT}"))
            .unwrap_or_default(),
    ];
    for _ in 0..3 {
        for target in &targets {
            if !target.is_empty() {
                let _ = socket.send_to(DISCOVERY_MAGIC.as_bytes(), target.as_str());
            }
        }
        thread::sleep(Duration::from_millis(80));
    }
    let deadline = Instant::now() + Duration::from_millis(700);
    let mut found: HashMap<String, Value> = HashMap::new();
    let mut buffer = [0u8; 512];
    while Instant::now() < deadline {
        let Ok((size, source)) = socket.recv_from(&mut buffer) else {
            continue;
        };
        let Ok(value) = serde_json::from_slice::<Value>(&buffer[..size]) else {
            continue;
        };
        if value.get("magic").and_then(Value::as_str) != Some(DISCOVERY_MAGIC) {
            continue;
        }
        let Some(port) = value.get("port").and_then(Value::as_u64) else {
            continue;
        };
        let url = format!("http://{}:{}", source.ip(), port);
        found.entry(url.clone()).or_insert_with(|| {
            json!({
                "url": url,
                "name": value.get("name").and_then(Value::as_str).unwrap_or("LAN directory"),
            })
        });
    }
    found.into_values().collect()
}

pub fn start() -> Option<DirectoryHandle> {
    let listener = TcpListener::bind(("0.0.0.0", HTTP_PORT))
        .or_else(|_| TcpListener::bind(("0.0.0.0", 0)))
        .ok()?;
    let port = listener.local_addr().ok()?.port();
    let entries: Arc<Mutex<HashMap<String, Entry>>> = Arc::new(Mutex::new(HashMap::new()));

    let http_entries = entries.clone();
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let entries = http_entries.clone();
            thread::spawn(move || handle_connection(stream, entries));
        }
    });

    if let Ok(socket) = UdpSocket::bind(("0.0.0.0", DISCOVERY_PORT)) {
        let _ = socket.set_broadcast(true);
        thread::spawn(move || {
            let mut buffer = [0u8; 128];
            loop {
                let Ok((size, source)) = socket.recv_from(&mut buffer) else {
                    continue;
                };
                if &buffer[..size] != DISCOVERY_MAGIC.as_bytes() {
                    continue;
                }
                let reply = json!({
                    "magic": DISCOVERY_MAGIC,
                    "port": port,
                    "name": "Blendermp",
                });
                if let Ok(payload) = serde_json::to_vec(&reply) {
                    let _ = socket.send_to(&payload, source);
                }
            }
        });
    }

    Some(DirectoryHandle { port })
}
