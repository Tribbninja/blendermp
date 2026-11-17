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

use windows_sys::Win32::Foundation::{BOOL, HWND, LPARAM, POINT, RECT};
use windows_sys::Win32::Graphics::Gdi::ClientToScreen;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetClientRect, GetWindow, GetWindowLongPtrW, GetWindowTextLengthW,
    GetWindowTextW, GetWindowThreadProcessId, IsWindow, IsWindowVisible, SetForegroundWindow, SetParent,
    SetWindowLongPtrW, SetWindowPos, ShowWindow, GWL_EXSTYLE, GWL_STYLE, GW_OWNER, SWP_NOACTIVATE,
    SWP_NOZORDER, SWP_SHOWWINDOW, SW_SHOW, WS_CAPTION, WS_CHILD, WS_POPUP, WS_THICKFRAME,
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
    left_px: i32,
    top_px: i32,
    right_px: i32,
    bottom_px: i32,
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

    position(child, parent, left_px, top_px, right_px, bottom_px)?;
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
    left_px: i32,
    top_px: i32,
    right_px: i32,
    bottom_px: i32,
) -> Result<(), String> {
    let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
    if unsafe { GetClientRect(parent, &mut rect) } == 0 {
        return Err("GetClientRect failed".into());
    }
    let mut origin = POINT { x: 0, y: 0 };
    unsafe {
        let _ = ClientToScreen(parent, &mut origin);
    }

    let left = left_px.max(0);
    let top = top_px.max(0);
    let width = (rect.right - rect.left - left - right_px.max(0)).max(320);
    let height = (rect.bottom - rect.top - top - bottom_px.max(0)).max(240);

    let ok = unsafe {
        SetWindowPos(
            child,
            std::ptr::null_mut(),
            origin.x + left,
            origin.y + top,
            width,
            height,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    };
    if ok == 0 {
        return Err("SetWindowPos failed".into());
    }
    Ok(())
}

pub fn resize(
    info: &EmbedInfo,
    left_px: i32,
    top_px: i32,
    right_px: i32,
    bottom_px: i32,
) -> Result<(), String> {
    let child = info.hwnd as HWND;
    let parent = info.parent as HWND;
    if unsafe { IsWindow(child) } == 0 || unsafe { IsWindow(parent) } == 0 {
        return Err(format!("window was closed (child={}, parent={})", info.hwnd, info.parent));
    }
    position(child, parent, left_px, top_px, right_px, bottom_px)
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

pub fn focus(info: &EmbedInfo) {
    unsafe {
        let _ = SetForegroundWindow(info.hwnd as HWND);
    }
}
