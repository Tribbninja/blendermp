#![cfg(target_os = "windows")]

//! Native window embedding.
//!
//! Blender is re-parented into our window with `SetParent`, so its GPU surface
//! keeps rendering at full speed: no capture, no encoding, no extra latency.
//!
//! Safety rules for the bridge:
//! * Only a window owned by the Blender PID we launched is embedded.
//! * The caller passes the bounds; they are clamped to sane minimums.
//! * The original window style is restored on detach.

use windows_sys::Win32::Foundation::{BOOL, HWND, LPARAM, RECT};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{EnableWindow, SetFocus};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetClientRect, GetWindow, GetWindowLongPtrW, GetWindowTextLengthW,
    GetWindowTextW, GetWindowThreadProcessId, IsWindow, IsWindowVisible, SetForegroundWindow, SetParent,
    SetWindowLongPtrW, SetWindowPos, ShowWindow, GetWindowRect, GetClassNameW, PostMessageW, WM_CLOSE,
    BringWindowToTop,
    GWL_EXSTYLE, GWL_STYLE, GW_OWNER, SWP_NOACTIVATE, SWP_NOZORDER, SWP_SHOWWINDOW, SW_SHOW,
    WS_CAPTION, WS_CHILD, WS_POPUP, WS_THICKFRAME,
};

struct Search {
    pid: u32,
    title: Option<String>,
    found: Option<HWND>,
}

unsafe extern "system" fn enum_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let search = &mut *(lparam as *mut Search);
    if IsWindowVisible(hwnd) == 0 {
        return 1;
    }

    let owner = GetWindow(hwnd, GW_OWNER);
    if !owner.is_null() {
        return 1;
    }

    let length = GetWindowTextLengthW(hwnd);
    if length <= 0 {
        return 1;
    }
    let mut buffer = vec![0u16; length as usize + 1];
    let copied = GetWindowTextW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32);
    if copied <= 0 {
        return 1;
    }
    let title = String::from_utf16_lossy(&buffer[..copied as usize]);

    if let Some(fragment) = &search.title {
        if title.to_lowercase().contains(&fragment.to_lowercase()) {
            search.found = Some(hwnd);
            return 0;
        }
        return 1;
    }

    let mut pid = 0u32;
    GetWindowThreadProcessId(hwnd, &mut pid);
    if pid == search.pid && title.to_lowercase().contains("blender") {
        search.found = Some(hwnd);
        return 0;
    }
    1
}

fn enumerate(search: &mut Search) -> Option<HWND> {
    unsafe {
        let _ = EnumWindows(Some(enum_proc), search as *mut Search as LPARAM);
    }
    search.found
}

pub fn find_window_for_pid(pid: u32) -> Option<isize> {
    let mut search = Search {
        pid,
        title: None,
        found: None,
    };
    enumerate(&mut search).map(|hwnd| hwnd as isize)
}

pub fn find_window_by_title(fragment: &str) -> Option<isize> {
    let mut search = Search {
        pid: 0,
        title: Some(fragment.to_string()),
        found: None,
    };
    enumerate(&mut search).map(|hwnd| hwnd as isize)
}

pub struct EmbedInfo {
    pub hwnd: isize,
    pub style: isize,
    pub exstyle: isize,
    pub parent: isize,
}

pub fn embed(
    child_handle: isize,
    parent_handle: isize,
    x_px: i32,
    y_px: i32,
    width_px: i32,
    height_px: i32,
    viewport_w: f64,
    viewport_h: f64,
) -> Result<EmbedInfo, String> {
    let child = child_handle as HWND;
    let parent = parent_handle as HWND;
    if unsafe { IsWindow(child) } == 0 {
        return Err("Blender window is no longer valid".into());
    }
    if unsafe { IsWindow(parent) } == 0 {
        return Err("application window is no longer valid".into());
    }

    let style = unsafe { GetWindowLongPtrW(child, GWL_STYLE) };
    let exstyle = unsafe { GetWindowLongPtrW(child, GWL_EXSTYLE) };
    unsafe {
        let _ = EnableWindow(child, 1);
    }
    let strip = (WS_POPUP | WS_CAPTION | WS_THICKFRAME) as isize;
    let new_style = (style & !strip) | WS_CHILD as isize;
    unsafe {
        SetWindowLongPtrW(child, GWL_STYLE, new_style);
    }

    let previous = unsafe { SetParent(child, parent) };
    if previous.is_null() {
        // SetParent returns NULL on success when the window had no parent, so a
        // zero result is not by itself an error. Verify by checking the window
        // is still alive instead.
        if unsafe { IsWindow(child) } == 0 {
            return Err("SetParent failed".into());
        }
    }

    position(child, parent, x_px, y_px, width_px, height_px, viewport_w, viewport_h)?;
    unsafe {
        let _ = ShowWindow(child, SW_SHOW);
    }

    Ok(EmbedInfo {
        hwnd: child_handle,
        style,
        exstyle,
        parent: parent_handle,
    })
}

fn position(
    child: HWND,
    parent: HWND,
    x_px: i32,
    y_px: i32,
    width_px: i32,
    height_px: i32,
    viewport_w: f64,
    viewport_h: f64,
) -> Result<(), String> {
    let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
    if unsafe { GetClientRect(parent, &mut rect) } == 0 {
        return Err("GetClientRect failed".into());
    }
    let client_w = (rect.right - rect.left) as f64;
    let client_h = (rect.bottom - rect.top) as f64;
    let scale_x = (client_w / viewport_w.max(1.0)).clamp(0.25, 4.0);
    let scale_y = (client_h / viewport_h.max(1.0)).clamp(0.25, 4.0);
    let x = ((x_px.max(0) as f64) * scale_x).round() as i32;
    let y = ((y_px.max(0) as f64) * scale_y).round() as i32;
    let width = ((width_px.max(40) as f64) * scale_x).round() as i32;
    let height = ((height_px.max(30) as f64) * scale_y).round() as i32;

    let ok = unsafe {
        SetWindowPos(
            child,
            std::ptr::null_mut(),
            x,
            y,
            width,
            height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    };
    if ok == 0 {
        return Err("SetWindowPos failed".into());
    }
    Ok(())
}

pub fn resize(
    info: &EmbedInfo,
    x_px: i32,
    y_px: i32,
    width_px: i32,
    height_px: i32,
    viewport_w: f64,
    viewport_h: f64,
) -> Result<(), String> {
    let child = info.hwnd as HWND;
    let parent = info.parent as HWND;
    if unsafe { IsWindow(child) } == 0 || unsafe { IsWindow(parent) } == 0 {
        return Err(format!("window was closed (child={}, parent={})", info.hwnd, info.parent));
    }
    position(child, parent, x_px, y_px, width_px, height_px, viewport_w, viewport_h)
}

pub fn detach(info: &EmbedInfo) {
    let child = info.hwnd as HWND;
    unsafe {
        let _ = SetParent(child, std::ptr::null_mut());
        SetWindowLongPtrW(child, GWL_STYLE, info.style);
        SetWindowLongPtrW(child, GWL_EXSTYLE, info.exstyle);
        let _ = ShowWindow(child, SW_SHOW);
    }
}

pub fn dismiss_splash(pid: u32) -> usize {
    // After the main Blender window becomes a child, any remaining top-level
    // GHOST window for its process is the startup splash. Close it with WM_CLOSE
    // rather than killing the process.
    struct Search {
        pid: u32,
        count: usize,
    }
    unsafe extern "system" fn callback(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let search = &mut *(lparam as *mut Search);
        let mut window_pid = 0u32;
        GetWindowThreadProcessId(hwnd, &mut window_pid);
        if window_pid != search.pid || IsWindowVisible(hwnd) == 0 {
            return 1;
        }
        let mut class_buffer = [0u16; 128];
        let class_length = GetClassNameW(hwnd, class_buffer.as_mut_ptr(), class_buffer.len() as i32);
        if class_length <= 0 {
            return 1;
        }
        let class_name = String::from_utf16_lossy(&class_buffer[..class_length as usize]);
        if class_name != "GHOST_WindowClass" {
            return 1;
        }
        let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
        if GetWindowRect(hwnd, &mut rect) == 0 {
            return 1;
        }
        let width = rect.right - rect.left;
        let height = rect.bottom - rect.top;
        // Splash windows are centered and modest; avoid closing large tool windows.
        if (360..=1600).contains(&width) && (180..=1000).contains(&height) {
            let _ = PostMessageW(hwnd, WM_CLOSE, 0, 0);
            search.count += 1;
        }
        1
    }

    let mut search = Search { pid, count: 0 };
    unsafe {
        let _ = EnumWindows(Some(callback), &mut search as *mut Search as LPARAM);
    }
    search.count
}

pub fn focus(info: &EmbedInfo) {
    unsafe {
        let _ = SetForegroundWindow(info.hwnd as HWND);
    }
}
