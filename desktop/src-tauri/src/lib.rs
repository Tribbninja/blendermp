mod win_embed;

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::State;

const APP_DIR: &str = "Blendermp";
const MAX_STATUS_BYTES: u64 = 64 * 1024;
const CROCKFORD: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

#[derive(Default)]
struct AppState {
    blender_pid: Mutex<Option<u32>>,
    embedded: Mutex<Option<win_embed::EmbedInfo>>,
}

#[derive(Deserialize, Serialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
enum Action {
    Host,
    Join,
    Leave,
    Chat,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SessionRequest {
    action: Action,
    code: Option<String>,
    name: Option<String>,
    port: Option<u16>,
    pin: Option<String>,
    fingerprint: Option<String>,
    text: Option<String>,
    relay: Option<String>,
    use_relay: Option<bool>,
}

fn data_dir() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("XDG_DATA_HOME").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join(APP_DIR)
}

fn normalize_code(value: &str) -> Result<String, String> {
    let cleaned: String = value
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .map(|ch| ch.to_ascii_uppercase())
        .collect();
    if cleaned.len() != 15 {
        return Err("room code must be 15 characters".into());
    }
    if !cleaned.chars().all(|ch| CROCKFORD.contains(ch)) {
        return Err("room code contains an invalid character".into());
    }
    Ok(cleaned)
}

fn valid_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 48
        && !value.chars().any(|ch| ch.is_control())
}

fn valid_pin(value: &str) -> bool {
    value.len() <= 64 && !value.chars().any(|ch| ch.is_control())
}

fn valid_fingerprint(value: &str) -> bool {
    value.len() == 64 && value.chars().all(|ch| ch.is_ascii_hexdigit())
}

fn addon_root() -> Option<PathBuf> {
    if let Some(value) = std::env::var_os("BMP_ADDON_ROOT") {
        let path = PathBuf::from(value);
        if path.join("blender_multiplayer").is_dir() {
            return Some(path);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors().take(7) {
            let candidate = ancestor.join("addon");
            if candidate.join("blender_multiplayer").is_dir() {
                return Some(candidate);
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        let candidate = cwd.join("addon");
        if candidate.join("blender_multiplayer").is_dir() {
            return Some(candidate);
        }
    }
    let installed = data_dir().join("addon");
    if installed.join("blender_multiplayer").is_dir() {
        return Some(installed);
    }
    None
}

fn blender_version(path: &Path) -> Vec<u32> {
    // Parent folders look like "Blender 5.3" or "blender-5.3.0-windows-x64".
    let Some(name) = path
        .parent()
        .and_then(|parent| parent.file_name())
        .and_then(|name| name.to_str())
    else {
        return Vec::new();
    };
    let mut best: Vec<u32> = Vec::new();
    for token in name.split(|ch: char| !(ch.is_ascii_digit() || ch == '.')) {
        if !token.contains('.') {
            continue;
        }
        let parts: Vec<u32> = token.split('.').filter_map(|part| part.parse().ok()).collect();
        if parts.len() >= 2 && parts.len() > best.len() {
            best = parts;
        }
    }
    best
}

fn newest_blender() -> Option<PathBuf> {
    if let Some(value) = std::env::var_os("BMP_BLENDER") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Some(path);
        }
    }

    let mut roots = vec![
        PathBuf::from(r"C:\Program Files\Blender Foundation"),
        PathBuf::from(r"C:\Program Files (x86)\Blender Foundation"),
    ];
    if let Some(program_files) = std::env::var_os("ProgramFiles") {
        roots.push(PathBuf::from(program_files).join("Blender Foundation"));
    }
    if let Some(program_files_x86) = std::env::var_os("ProgramFiles(x86)") {
        roots.push(PathBuf::from(program_files_x86).join("Blender Foundation"));
    }
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        roots.push(PathBuf::from(local).join("Programs").join("Blender Foundation"));
    }

    let mut candidates: Vec<PathBuf> = Vec::new();
    for root in roots {
        let Ok(entries) = fs::read_dir(root) else {
            continue;
        };
        for entry in entries.flatten() {
            let exe = entry.path().join("blender.exe");
            if exe.is_file() {
                candidates.push(exe);
            }
        }
    }

    // Steam installs Blender as ...\steamapps\common\Blender\blender.exe.
    for key in ["ProgramFiles(x86)", "ProgramFiles"] {
        let Some(base) = std::env::var_os(key) else {
            continue;
        };
        let exe = PathBuf::from(base)
            .join("Steam")
            .join("steamapps")
            .join("common")
            .join("Blender")
            .join("blender.exe");
        if exe.is_file() {
            candidates.push(exe);
        }
    }

    candidates.sort_by(|a, b| {
        blender_version(a)
            .cmp(&blender_version(b))
            .then_with(|| a.cmp(b))
    });
    candidates.pop()
}

#[tauri::command]
fn detect_blender() -> Option<String> {
    newest_blender().map(|path| path.to_string_lossy().to_string())
}

#[tauri::command]
fn launch_blender(state: State<AppState>, path: String, blend_file: Option<String>) -> Result<u32, String> {
    let exe = PathBuf::from(&path);
    let exe = exe.canonicalize().map_err(|_| "Blender path does not exist".to_string())?;
    if exe.file_name().and_then(|name| name.to_str()).map(|name| name.eq_ignore_ascii_case("blender.exe")) != Some(true) {
        return Err("selected file is not blender.exe".into());
    }
    if !exe.is_file() {
        return Err("Blender path is not a file".into());
    }

    let root = addon_root().ok_or_else(|| "addon folder not found; set BMP_ADDON_ROOT".to_string())?;
    let root_text = root.to_string_lossy().to_string();
    if root_text.contains('\'') || root_text.contains('\n') || root_text.contains('\r') {
        return Err("addon path contains unsupported characters".into());
    }

    let expression = format!(
        "import sys,bpy; sys.path.insert(0,r'{root_text}'); bpy.ops.preferences.addon_enable(module='blender_multiplayer')"
    );

    let mut command = Command::new(&exe);
    command.arg("--python-expr").arg(expression);
    if let Some(file) = blend_file {
        if !file.is_empty() {
            let blend = PathBuf::from(&file).canonicalize().map_err(|_| "blend file does not exist".to_string())?;
            if blend.extension().and_then(|ext| ext.to_str()).map(|ext| ext.eq_ignore_ascii_case("blend")) != Some(true) {
                return Err("only .blend files are accepted".into());
            }
            command.arg(blend);
        }
    }

    let child = command.spawn().map_err(|error| format!("could not start Blender: {error}"))?;
    let pid = child.id();
    *state.blender_pid.lock().map_err(|_| "state lock poisoned".to_string())? = Some(pid);
    Ok(pid)
}

#[tauri::command]
fn find_blender_window(state: State<AppState>) -> Result<Option<i64>, String> {
    let pid = *state.blender_pid.lock().map_err(|_| "state lock poisoned".to_string())?;
    let Some(pid) = pid else { return Ok(None) };
    #[cfg(windows)]
    {
        Ok(win_embed::find_window_for_pid(pid).map(|value| value as i64))
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
        Ok(None)
    }
}

#[tauri::command]
fn embed_blender(
    state: State<AppState>,
    x_px: f64,
    y_px: f64,
    width_px: f64,
    height_px: f64,
    viewport_w: f64,
    viewport_h: f64,
) -> Result<(), String> {
    #[cfg(windows)]
    {
        let pid = *state.blender_pid.lock().map_err(|_| "state lock poisoned".to_string())?;
        let pid = pid.ok_or_else(|| "Blender is not running".to_string())?;
        let child = win_embed::find_window_for_pid(pid).ok_or_else(|| "Blender window not found".to_string())?;
        let parent = win_embed::find_window_by_title("Blendermp").ok_or_else(|| "app window not found".to_string())?;
        let info = win_embed::embed(
            child,
            parent,
            x_px.max(0.0).round() as i32,
            y_px.max(0.0).round() as i32,
            width_px.max(40.0).round() as i32,
            height_px.max(30.0).round() as i32,
            viewport_w,
            viewport_h,
        )?;
        *state.embedded.lock().map_err(|_| "state lock poisoned".to_string())? = Some(info);
        let _ = win_embed::dismiss_splash(pid);
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = (state, x_px, y_px, width_px, height_px, viewport_w, viewport_h);
        Err("native embedding is Windows-only; Blender runs in its own window".into())
    }
}

#[tauri::command]
fn set_embed_bounds(
    state: State<AppState>,
    x_px: f64,
    y_px: f64,
    width_px: f64,
    height_px: f64,
    viewport_w: f64,
    viewport_h: f64,
) -> Result<(), String> {
    #[cfg(windows)]
    {
        let guard = state.embedded.lock().map_err(|_| "state lock poisoned".to_string())?;
        let info = guard.as_ref().ok_or_else(|| "Blender is not embedded".to_string())?;
        win_embed::resize(
            info,
            x_px.max(0.0).round() as i32,
            y_px.max(0.0).round() as i32,
            width_px.max(40.0).round() as i32,
            height_px.max(30.0).round() as i32,
            viewport_w,
            viewport_h,
        )
    }
    #[cfg(not(windows))]
    {
        let _ = (state, x_px, y_px, width_px, height_px, viewport_w, viewport_h);
        Ok(())
    }
}

#[tauri::command]
fn detach_blender(state: State<AppState>) -> Result<(), String> {
    #[cfg(windows)]
    {
        let mut guard = state.embedded.lock().map_err(|_| "state lock poisoned".to_string())?;
        if let Some(info) = guard.take() {
            win_embed::detach(&info);
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = state;
        Ok(())
    }
}

#[tauri::command]
fn set_blender_visible(state: State<AppState>, visible: bool) -> Result<(), String> {
    #[cfg(windows)]
    {
        let guard = state.embedded.lock().map_err(|_| "state lock poisoned".to_string())?;
        if let Some(info) = guard.as_ref() {
            win_embed::set_visible(info, visible);
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = (state, visible);
        Ok(())
    }
}

#[tauri::command]
fn focus_blender(state: State<AppState>) -> Result<(), String> {
    #[cfg(windows)]
    {
        let guard = state.embedded.lock().map_err(|_| "state lock poisoned".to_string())?;
        if let Some(info) = guard.as_ref() {
            win_embed::focus(info);
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = state;
        Ok(())
    }
}

#[tauri::command]
fn read_status() -> Option<serde_json::Value> {
    let path = data_dir().join("status.json");
    let metadata = fs::metadata(&path).ok()?;
    if metadata.len() > MAX_STATUS_BYTES {
        return None;
    }
    let body = fs::read_to_string(path).ok()?;
    serde_json::from_str(&body).ok()
}

#[tauri::command]
fn log_client(message: String) {
    let dir = data_dir();
    if fs::create_dir_all(&dir).is_err() {
        return;
    }
    let path = dir.join("ui.log");
    let line = format!("{} {}\n", chrono_stamp(), message.chars().take(2000).collect::<String>());
    let existing = fs::metadata(&path).map(|meta| meta.len()).unwrap_or(0);
    if existing > 256 * 1024 {
        let _ = fs::write(&path, b"");
    }
    use std::io::Write;
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = file.write_all(line.as_bytes());
    }
}

fn chrono_stamp() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or(0);
    format!("{seconds}")
}

#[tauri::command]
fn startup_flags() -> serde_json::Value {
    let autohost = std::env::args().any(|arg| arg == "--autohost");
    let autojoin = std::env::args().find_map(|arg| arg.strip_prefix("--autojoin=").map(str::to_string));
    serde_json::json!({ "autohost": autohost, "autojoin": autojoin })
}

#[tauri::command]
fn write_session(request: SessionRequest) -> Result<(), String> {
    if let Some(name) = &request.name {
        if !valid_name(name) {
            return Err("invalid display name".into());
        }
    }
    if let Some(pin) = &request.pin {
        if !valid_pin(pin) {
            return Err("invalid room PIN".into());
        }
    }
    if let Some(fingerprint) = &request.fingerprint {
        if !valid_fingerprint(fingerprint) {
            return Err("invalid certificate fingerprint".into());
        }
    }
    if let Some(port) = request.port {
        if port < 1024 {
            return Err("port must be 1024 or higher".into());
        }
    }
    if let Some(relay) = &request.relay {
        let (host, port) = relay
            .rsplit_once(':')
            .ok_or("relay must be IP:PORT")?;
        host.parse::<std::net::Ipv4Addr>().map_err(|_| "relay host must be IPv4")?;
        let port: u16 = port.parse().map_err(|_| "relay port is invalid")?;
        if port < 1024 {
            return Err("relay port must be 1024 or higher".into());
        }
    }

    let code = match request.action {
        Action::Leave | Action::Chat => None,
        Action::Join => Some(normalize_code(request.code.as_deref().ok_or("a room code is required to join")?)?),
        Action::Host => match request.code.as_deref() {
            Some(value) => Some(normalize_code(value)?),
            None => None,
        },
    };

    let text = match request.action {
        Action::Chat => {
            let value = request.text.as_deref().ok_or("chat text is required")?;
            if value.is_empty() || value.len() > 2000 || value.chars().any(|ch| ch.is_control()) {
                return Err("invalid chat text".into());
            }
            Some(value.to_string())
        }
        _ => None,
    };

    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    let body = serde_json::json!({
        "action": request.action,
        "code": code,
        "name": request.name,
        "port": request.port,
        "pin": request.pin,
        "fingerprint": request.fingerprint,
        "text": text,
        "relay": request.relay,
        "use_relay": request.use_relay,
        "nonce": format!("{:x}-{:x}", std::process::id(), stamp),
    });

    let directory = data_dir();
    fs::create_dir_all(&directory).map_err(|error| format!("cannot create app data directory: {error}"))?;
    let path = directory.join("session.json");
    let temp = directory.join(".session.tmp");
    fs::write(&temp, serde_json::to_vec(&body).map_err(|error| error.to_string())?)
        .map_err(|error| format!("cannot write control file: {error}"))?;
    fs::rename(&temp, &path).map_err(|error| format!("cannot replace control file: {error}"))?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // MinGW's default manifest can collide with Tauri's when linking, leaving
    // the process DPI-unaware. Set awareness explicitly so the embedded window
    // is laid out in real pixels on high-DPI displays.
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::UI::HiDpi::{
            SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        };
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }

    let directory = data_dir();
    if fs::create_dir_all(&directory).is_ok() {
        use std::io::Write;
        if let Ok(mut file) = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(directory.join("boot.log"))
        {
            let stamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|value| value.as_secs())
                .unwrap_or(0);
            let _ = writeln!(file, "{stamp} boot");
        }
    }

    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            detect_blender,
            launch_blender,
            find_blender_window,
            embed_blender,
            set_embed_bounds,
            detach_blender,
            set_blender_visible,
            focus_blender,
            read_status,
            log_client,
            startup_flags,
            write_session
        ])
        .run(tauri::generate_context!())
        .expect("error while running Blendermp");
}
