mod directory;
mod win_embed;

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{Manager, State};

const APP_DIR: &str = "Blendermp";
const MAX_STATUS_BYTES: u64 = 64 * 1024;
const MAX_SETTINGS_BYTES: u64 = 64 * 1024;
const CROCKFORD: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

fn read_text_shared(path: &Path) -> Option<String> {
    #[cfg(windows)]
    {
        use std::io::Read;
        use std::os::windows::fs::OpenOptionsExt;
        // The addon atomically replaces status.json. Allow delete sharing while
        // this read is in flight, otherwise the replace intermittently fails
        // with "Access denied" and the app keeps a stale status forever.
        let mut file = fs::OpenOptions::new()
            .read(true)
            .share_mode(0x7)
            .open(path)
            .ok()?;
        let mut body = String::new();
        file.read_to_string(&mut body).ok()?;
        Some(body)
    }
    #[cfg(not(windows))]
    {
        fs::read_to_string(path).ok()
    }
}

struct AppState {
    blender_pid: Mutex<Option<u32>>,
    embedded: Mutex<Option<win_embed::EmbedInfo>>,
    directory: Option<directory::DirectoryHandle>,
}

#[derive(Deserialize, Serialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
enum Action {
    Host,
    Join,
    Leave,
    Chat,
    Kick,
    Role,
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
    target: Option<String>,
    relay: Option<String>,
    relay_url: Option<String>,
    relay_token: Option<String>,
    use_relay: Option<bool>,
    role: Option<String>,
}

fn data_dir() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("XDG_DATA_HOME").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join(APP_DIR)
}

fn copy_dir_all(source: &Path, target: &Path) -> std::io::Result<()> {
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let path = entry.path();
        let destination = target.join(entry.file_name());
        if path.is_dir() {
            copy_dir_all(&path, &destination)?;
        } else {
            fs::copy(&path, &destination)?;
        }
    }
    Ok(())
}

fn python_vendor_paths() -> Vec<PathBuf> {
    let mut paths = vec![data_dir().join("python")];
    if let Ok(exe) = std::env::current_exe() {
        if let Some(directory) = exe.parent() {
            let bundled = directory.join("python");
            if bundled.is_dir() {
                paths.push(bundled);
            }
        }
    }
    paths
}

fn ensure_python_vendor() {
    let target = data_dir().join("python");
    if target.join("cryptography").is_dir() && target.join("cffi").is_dir() {
        return;
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(directory) = exe.parent() {
            let bundled = directory.join("python");
            if bundled.is_dir() {
                let _ = copy_dir_all(&bundled, &target);
            }
        }
    }
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

    // The addon targets Blender 5.x. Keep unknowns (for example Steam, whose
    // folder has no version) but drop installs that are definitely too old.
    candidates.retain(|path| {
        let version = blender_version(path);
        version.is_empty() || version.first().copied().unwrap_or(0) >= 5
    });
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
    let version = blender_version(&exe);
    if !version.is_empty() && version.first().copied().unwrap_or(0) < 5 {
        return Err("Blender 5 or newer is required".into());
    }

    let root = addon_root().ok_or_else(|| "addon folder not found; set BMP_ADDON_ROOT".to_string())?;
    let root_text = root.to_string_lossy().to_string();
    if root_text.contains('\'') || root_text.contains('\n') || root_text.contains('\r') {
        return Err("addon path contains unsupported characters".into());
    }

    // A previously enabled copy (for example a development checkout) can win
    // over the addon bundled with the app. Disable any loaded copy, drop its
    // modules, then load the bundled addon from the front of sys.path.
    let expression = format!(
        "import sys,bpy; sys.path.insert(0,r'{root_text}'); \
         mod='blender_multiplayer'; \
         (bpy.ops.preferences.addon_disable(module=mod) if mod in bpy.context.preferences.addons else None); \
         _old=sys.modules.get(mod); \
         (_old.unregister() if _old is not None and hasattr(_old, 'unregister') else None); \
         bpy._blendermp_active=False; \
         [sys.modules.pop(name) for name in list(sys.modules) if name == mod or name.startswith(mod + '.')]; \
         bpy.ops.preferences.addon_enable(module=mod); \
         import blender_multiplayer as _bmp; print('BMP_ADDON=' + str(_bmp.__file__), flush=True)"
    );

    let mut command = Command::new(&exe);
    command.arg("--python-expr").arg(expression);
    ensure_python_vendor();
    let mut python_paths = python_vendor_paths();
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        python_paths.extend(std::env::split_paths(&existing));
    }
    if let Ok(value) = std::env::join_paths(python_paths) {
        command.env("PYTHONPATH", value);
    }
    if let Some(file) = blend_file {
        if !file.is_empty() {
            let blend = PathBuf::from(&file).canonicalize().map_err(|_| "blend file does not exist".to_string())?;
            if blend.extension().and_then(|ext| ext.to_str()).map(|ext| ext.eq_ignore_ascii_case("blend")) != Some(true) {
                return Err("only .blend files are accepted".into());
            }
            command.arg(blend);
        }
    }

    // Keep Blender's stdout/stderr in a log instead of a console window. This
    // captures addon tracebacks and startup errors for support.
    if let Ok(log) = fs::File::create(data_dir().join("blender.log")) {
        if let Ok(err) = log.try_clone() {
            command.stdout(Stdio::from(log)).stderr(Stdio::from(err));
        }
    }

    // blender.exe is a console-subsystem binary. Without this flag Windows
    // opens a console window next to Blender (and that console can be picked
    // up as the Blender window to embed).
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
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
fn blender_running(state: State<AppState>) -> Result<bool, String> {
    let pid = *state.blender_pid.lock().map_err(|_| "state lock poisoned".to_string())?;
    let Some(pid) = pid else { return Ok(false) };
    #[cfg(windows)]
    {
        Ok(win_embed::process_alive(pid))
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
        Ok(true)
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
        let x = x_px.max(0.0).round() as i32;
        let y = y_px.max(0.0).round() as i32;
        let width = width_px.max(40.0).round() as i32;
        let height = height_px.max(30.0).round() as i32;
        let mut guard = state.embedded.lock().map_err(|_| "state lock poisoned".to_string())?;
        // Docking is idempotent: re-docking the same window would toggle styles
        // and can stall Blender's surface. Resize instead.
        if let Some(info) = guard.as_ref() {
            if info.hwnd == child {
                return win_embed::resize(info, x, y, width, height, viewport_w, viewport_h);
            }
        }
        let info = win_embed::embed(child, parent, x, y, width, height, viewport_w, viewport_h)?;
        *guard = Some(info);
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
        let x = x_px.max(0.0).round() as i32;
        let y = y_px.max(0.0).round() as i32;
        let width = width_px.max(40.0).round() as i32;
        let height = height_px.max(30.0).round() as i32;
        if let Err(error) = win_embed::resize(info, x, y, width, height, viewport_w, viewport_h) {
            // Blender can replace its main window (for example after loading a
            // file). Re-dock the replacement instead of staying detached.
            if win_embed::window_alive(info.hwnd) {
                return Err(error);
            }
            let parent = info.parent;
            let pid = *state.blender_pid.lock().map_err(|_| "state lock poisoned".to_string())?;
            let Some(pid) = pid else { return Err(error); };
            let child = win_embed::find_window_for_pid(pid).ok_or(error)?;
            drop(guard);
            let mut guard = state.embedded.lock().map_err(|_| "state lock poisoned".to_string())?;
            let new_info = win_embed::embed(child, parent, x, y, width, height, viewport_w, viewport_h)?;
            *guard = Some(new_info);
        }
        Ok(())
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
fn dock_looks_black(state: State<AppState>) -> Result<Option<bool>, String> {
    #[cfg(windows)]
    {
        let guard = state.embedded.lock().map_err(|_| "state lock poisoned".to_string())?;
        Ok(match guard.as_ref() {
            Some(info) => win_embed::looks_black(info),
            None => None,
        })
    }
    #[cfg(not(windows))]
    {
        let _ = state;
        Ok(Some(false))
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
fn read_status(state: State<AppState>) -> Option<serde_json::Value> {
    let path = data_dir().join("status.json");
    let metadata = fs::metadata(&path).ok()?;
    if metadata.len() > MAX_STATUS_BYTES {
        return None;
    }
    let body = read_text_shared(&path)?;
    let value: serde_json::Value = serde_json::from_str(&body).ok()?;

    // Ignore a status left behind by a different Blender process. On the first
    // poll after an app restart, adopt a visible Blender so joining reuses it
    // instead of starting a second copy.
    let status_pid = value.get("pid").and_then(|value| value.as_u64()).map(|value| value as u32);
    let mut tracked = state.blender_pid.lock().ok()?;
    match (*tracked, status_pid) {
        (Some(expected), Some(actual)) => {
            if expected != actual {
                return None;
            }
            #[cfg(windows)]
            {
                if !win_embed::process_alive(expected) {
                    *tracked = None;
                    return Some(serde_json::json!({
                        "state": "offline",
                        "users": [],
                        "messages": [],
                    }));
                }
            }
        }
        (Some(_), None) => return None,
        (None, Some(actual)) => {
            #[cfg(windows)]
            {
                if win_embed::find_window_for_pid(actual).is_none() {
                    return None;
                }
            }
            *tracked = Some(actual);
        }
        (None, None) => {}
    }
    Some(value)
}

#[tauri::command]
fn read_settings() -> Option<serde_json::Value> {
    let path = data_dir().join("settings.json");
    let metadata = fs::metadata(&path).ok()?;
    if metadata.len() > MAX_SETTINGS_BYTES {
        return None;
    }
    let body = read_text_shared(&path)?;
    let value: serde_json::Value = serde_json::from_str(&body).ok()?;
    if !value.is_object() {
        return None;
    }
    Some(value)
}

#[tauri::command]
fn write_settings(settings: serde_json::Value) -> Result<(), String> {
    if !settings.is_object() {
        return Err("settings must be an object".into());
    }
    let body = serde_json::to_string(&settings).map_err(|error| error.to_string())?;
    if body.len() as u64 > MAX_SETTINGS_BYTES {
        return Err("settings are too large".into());
    }
    let directory = data_dir();
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let path = directory.join("settings.json");
    let temp = directory.join("settings.json.tmp");
    {
        use std::io::Write;
        let mut file = fs::File::create(&temp).map_err(|error| error.to_string())?;
        file.write_all(body.as_bytes()).map_err(|error| error.to_string())?;
        file.sync_all().map_err(|error| error.to_string())?;
    }
    if path.exists() {
        fs::remove_file(&path).map_err(|error| error.to_string())?;
    }
    fs::rename(&temp, &path).map_err(|error| error.to_string())?;
    Ok(())
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
            return Err("invalid room password".into());
        }
    }
    if let Some(target) = &request.target {
        if !valid_name(target) {
            return Err("invalid kick target".into());
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
    if let Some(relay_url) = &request.relay_url {
        if relay_url.len() > 256
            || !(relay_url.starts_with("ws://") || relay_url.starts_with("wss://"))
            || relay_url.chars().any(|ch| ch.is_control())
        {
            return Err("invalid relay URL".into());
        }
    }
    if let Some(token) = &request.relay_token {
        if !(32..=128).contains(&token.len())
            || !token
                .chars()
                .all(|ch| ch.is_ascii_alphanumeric() || ch == '-' || ch == '_')
        {
            return Err("invalid relay token".into());
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
        Action::Leave | Action::Chat | Action::Kick | Action::Role => None,
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

    let target = match request.action {
        Action::Kick | Action::Role => Some(
            request
                .target
                .as_deref()
                .ok_or("a player name is required")?
                .to_string(),
        ),
        _ => None,
    };

    let role = match request.action {
        Action::Role => {
            let value = request.role.as_deref().ok_or("a role is required")?;
            if !matches!(value, "viewer" | "commenter" | "editor" | "admin") {
                return Err("invalid role".into());
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
        "target": target,
        "relay": request.relay,
        "relay_url": request.relay_url,
        "relay_token": request.relay_token,
        "use_relay": request.use_relay,
        "role": role,
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

#[tauri::command]
fn local_directory(state: State<AppState>) -> Option<serde_json::Value> {
    let handle = state.directory.as_ref()?;
    Some(serde_json::json!({
        "local": format!("http://127.0.0.1:{}", handle.port),
        "lan": format!("http://{}:{}", directory::lan_ip(), handle.port),
        "port": handle.port,
    }))
}

#[tauri::command]
fn discover_directories() -> Vec<serde_json::Value> {
    directory::discover()
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

    let directory_server = directory::start();
    tauri::Builder::default()
        .manage(AppState {
            blender_pid: Mutex::new(None),
            embedded: Mutex::new(None),
            directory: directory_server,
        })
        .invoke_handler(tauri::generate_handler![
            local_directory,
            discover_directories,
            detect_blender,
            launch_blender,
            find_blender_window,
            blender_running,
            embed_blender,
            set_embed_bounds,
            dock_looks_black,
            detach_blender,
            set_blender_visible,
            focus_blender,
            read_status,
            read_settings,
            write_settings,
            log_client,
            startup_flags,
            write_session
        ])
        .on_window_event(|window, event| {
            // Closing the app also closes the Blender instance it launched.
            // Detach first so Blender's save-confirmation dialog is a normal
            // top-level window, then ask Blender to close. WM_CLOSE lets
            // Blender prompt for unsaved changes instead of losing work.
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                let state = window.app_handle().state::<AppState>();
                #[cfg(windows)]
                {
                    let info = state
                        .embedded
                        .lock()
                        .ok()
                        .and_then(|mut guard| guard.take());
                    if let Some(info) = info {
                        win_embed::detach(&info);
                    }
                }
                let pid = state
                    .blender_pid
                    .lock()
                    .ok()
                    .and_then(|mut guard| guard.take());
                if let Some(pid) = pid {
                    #[cfg(windows)]
                    {
                        // PID reuse must never turn app close into closing an
                        // unrelated program.
                        if win_embed::is_blender_process(pid)
                            && !win_embed::request_close(pid)
                            && win_embed::process_alive(pid)
                        {
                            // No window yet (still starting): do not leave an
                            // invisible Blender process behind.
                            win_embed::terminate_process(pid);
                        }
                    }
                    #[cfg(not(windows))]
                    {
                        let _ = pid;
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Blendermp");
}
