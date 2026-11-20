#![cfg(target_os = "windows")]

//! Native window docking.
//!
//! Blender is kept as a real top-level GPU window and **owned** by the app, then
//! positioned exactly over the stage area with its frame removed. This looks
//! identical to embedding but avoids `SetParent` across processes, which is the
//! classic cause of a black OpenGL surface on some GPUs, DPI settings and
//! WebView2 builds.
//!
//! Safety rules for the bridge:
//! * Only a window owned by the Blender PID we launched is docked.
//! * The caller passes the bounds; they are clamped to sane minimums.
//! * The original window style and owner are restored on detach.
//! * Closing the app asks Blender to close normally, so unsaved work can still
//!   be saved.

use windows_sys::Win32::Foundation::{BOOL, CloseHandle, HWND, LPARAM, POINT, RECT};
use windows_sys::Win32::Graphics::Gdi::{
    BitBlt, ClientToScreen, CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GetDC,
    ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, DIB_RGB_COLORS, HGDIOBJ, RGBQUAD, SRCCOPY,
};
use windows_sys::Win32::System::Threading::{
    AttachThreadInput, OpenProcess, TerminateProcess, PROCESS_TERMINATE,
};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{EnableWindow, SetFocus};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, EnumWindows, GetClassNameW, GetClientRect, GetForegroundWindow, GetParent,
    GetSystemMetrics, GetWindow, GetWindowLongPtrW, GetWindowRect, GetWindowTextLengthW,
    GetWindowTextW, GetWindowThreadProcessId, IsIconic, IsWindow, IsWindowVisible,
    PostMessageW, SetForegroundWindow, SetParent, SetWindowLongPtrW, SetWindowPos, ShowWindow,
    GWL_EXSTYLE, GWL_HWNDPARENT, GWL_STYLE, GW_OWNER, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN,
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SWP_ASYNCWINDOWPOS, SWP_FRAMECHANGED, SWP_NOACTIVATE,
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_SHOWWINDOW, SW_HIDE, SW_SHOW, WM_CLOSE, WS_CAPTION,
    WS_CHILD, WS_CLIPCHILDREN, WS_CLIPSIBLINGS, WS_MAXIMIZEBOX, WS_MINIMIZEBOX, WS_POPUP,
    WS_SYSMENU, WS_THICKFRAME, WS_VISIBLE,
};

const MIN_DOCK_WIDTH: f64 = 80.0;
const MIN_DOCK_HEIGHT: f64 = 60.0;
const WS_EX_APPWINDOW: isize = 0x0004_0000;
const WS_EX_TOOLWINDOW: isize = 0x0000_0080;

struct Search {
    pid: u32,
    title: Option<String>,
    found: Option<HWND>,
    best_area: i64,
    include_hidden: bool,
}

unsafe extern "system" fn enum_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let search = &mut *(lparam as *mut Search);
    if !search.include_hidden && IsWindowVisible(hwnd) == 0 {
        return 1;
    }

    let owner = GetWindow(hwnd, GW_OWNER);
    if search.title.is_some() {
        if !owner.is_null() {
            return 1;
        }
    } else if !owner.is_null() {
        // Skip Blender's own splash/tool windows, which are owned by its main
        // window. A docked main window is owned by the app (a different PID),
        // so it must still be found.
        let mut owner_pid = 0u32;
        GetWindowThreadProcessId(owner, &mut owner_pid);
        if owner_pid == search.pid {
            return 1;
        }
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

    // Blender's main window uses GHOST_WindowClass. Its console window (from
    // launching blender.exe directly) also has a title containing "blender",
    // so filter by class to avoid docking the console.
    let mut class_buffer = [0u16; 128];
    let class_length = GetClassNameW(hwnd, class_buffer.as_mut_ptr(), class_buffer.len() as i32);
    if class_length <= 0 {
        return 1;
    }
    let class_name = String::from_utf16_lossy(&class_buffer[..class_length as usize]);
    if class_name != "GHOST_WindowClass" {
        return 1;
    }

    let mut pid = 0u32;
    GetWindowThreadProcessId(hwnd, &mut pid);
    if pid != search.pid || !title.to_lowercase().contains("blender") {
        return 1;
    }

    // Prefer the largest window. Startup can briefly expose a small splash or
    // a tool window; docking that produces a tiny or black viewport.
    let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
    if GetWindowRect(hwnd, &mut rect) == 0 {
        return 1;
    }
    let width = (rect.right - rect.left).max(0) as i64;
    let height = (rect.bottom - rect.top).max(0) as i64;
    if width < 320 || height < 200 {
        return 1;
    }
    let area = width * height;
    if area > search.best_area {
        search.best_area = area;
        search.found = Some(hwnd);
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
        best_area: 0,
        include_hidden: false,
    };
    enumerate(&mut search).map(|hwnd| hwnd as isize)
}

fn find_window_for_pid_including_hidden(pid: u32) -> Option<HWND> {
    let mut search = Search {
        pid,
        title: None,
        found: None,
        best_area: 0,
        include_hidden: true,
    };
    enumerate(&mut search)
}

pub fn find_window_by_title(fragment: &str) -> Option<isize> {
    let mut search = Search {
        pid: 0,
        title: Some(fragment.to_string()),
        found: None,
        best_area: 0,
        include_hidden: false,
    };
    enumerate(&mut search).map(|hwnd| hwnd as isize)
}

pub struct EmbedInfo {
    pub hwnd: isize,
    pub style: isize,
    pub exstyle: isize,
    pub parent: isize,
    pub overlay: bool,
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
    if viewport_w < MIN_DOCK_WIDTH || viewport_h < MIN_DOCK_HEIGHT {
        return Err("the stage is not laid out yet".into());
    }

    let style = unsafe { GetWindowLongPtrW(child, GWL_STYLE) };
    let exstyle = unsafe { GetWindowLongPtrW(child, GWL_EXSTYLE) };
    unsafe {
        let _ = EnableWindow(child, 1);
    }

    // An older run (or a previous build) may have left the window parented as a
    // true child. Move it back to the desktop first so the owner/z-order logic
    // below works on a normal top-level window.
    if unsafe { GetParent(child) } == parent {
        unsafe {
            let _ = SetParent(child, std::ptr::null_mut());
        }
    }

    // Owned top-level window: it follows the app's z-order, minimizes with it,
    // but keeps its own native GPU surface. Do not use SetParent here.
    let dock_style = (style
        & !((WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_CHILD)
            as isize))
        | (WS_POPUP | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN) as isize;
    let dock_exstyle = (exstyle & !WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW;

    unsafe {
        SetWindowLongPtrW(child, GWL_HWNDPARENT, parent as isize);
        SetWindowLongPtrW(child, GWL_STYLE, dock_style);
        SetWindowLongPtrW(child, GWL_EXSTYLE, dock_exstyle);
        // Apply the style change without moving the window yet.
        let _ = SetWindowPos(
            child,
            std::ptr::null_mut(),
            0,
            0,
            0,
            0,
            SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_NOACTIVATE
                | SWP_FRAMECHANGED
                | SWP_ASYNCWINDOWPOS,
        );
    }

    position_overlay(
        child,
        parent,
        x_px,
        y_px,
        width_px,
        height_px,
        viewport_w,
        viewport_h,
        true,
    )?;

    unsafe {
        let _ = ShowWindow(child, SW_SHOW);
    }

    Ok(EmbedInfo {
        hwnd: child_handle,
        style,
        exstyle,
        parent: parent_handle,
        overlay: true,
    })
}

fn position_overlay(
    child: HWND,
    parent: HWND,
    x_px: i32,
    y_px: i32,
    width_px: i32,
    height_px: i32,
    viewport_w: f64,
    viewport_h: f64,
    raise: bool,
) -> Result<(), String> {
    if viewport_w < MIN_DOCK_WIDTH || viewport_h < MIN_DOCK_HEIGHT {
        return Err("the stage is not laid out yet".into());
    }
    // Minimized/hidden app windows have no usable client rect. Leave the docked
    // window alone; it follows the owner's minimize state automatically.
    if unsafe { IsIconic(parent) } != 0 || unsafe { IsWindowVisible(parent) } == 0 {
        return Ok(());
    }
    let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
    if unsafe { GetClientRect(parent, &mut rect) } == 0 {
        return Err("GetClientRect failed".into());
    }
    let client_w = (rect.right - rect.left) as f64;
    let client_h = (rect.bottom - rect.top) as f64;
    if client_w < MIN_DOCK_WIDTH || client_h < MIN_DOCK_HEIGHT {
        return Err("the app window is minimized".into());
    }
    let scale_x = (client_w / viewport_w.max(1.0)).clamp(0.25, 4.0);
    let scale_y = (client_h / viewport_h.max(1.0)).clamp(0.25, 4.0);

    let mut origin = POINT { x: 0, y: 0 };
    if unsafe { ClientToScreen(parent, &mut origin) } == 0 {
        return Err("ClientToScreen failed".into());
    }

    let x = origin.x + ((x_px.max(0) as f64) * scale_x).round() as i32;
    let y = origin.y + ((y_px.max(0) as f64) * scale_y).round() as i32;
    let width = ((width_px.max(MIN_DOCK_WIDTH as i32) as f64) * scale_x).round() as i32;
    let height = ((height_px.max(MIN_DOCK_HEIGHT as i32) as f64) * scale_y).round() as i32;

    let mut flags = SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS | SWP_FRAMECHANGED;
    if !raise {
        flags |= SWP_NOZORDER;
    }
    let ok = unsafe {
        SetWindowPos(
            child,
            std::ptr::null_mut(),
            x,
            y,
            width,
            height,
            flags,
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
    if info.overlay {
        // The window may have been moved behind another window; keep it owned
        // by the app so it stays above the app and follows its minimize state.
        unsafe {
            if GetWindowLongPtrW(child, GWL_HWNDPARENT) != parent as isize {
                SetWindowLongPtrW(child, GWL_HWNDPARENT, parent as isize);
            }
        }
        position_overlay(
            child,
            parent,
            x_px,
            y_px,
            width_px,
            height_px,
            viewport_w,
            viewport_h,
            false,
        )
    } else {
        let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
        if unsafe { GetClientRect(parent, &mut rect) } == 0 {
            return Err("GetClientRect failed".into());
        }
        let scale_x = ((rect.right - rect.left) as f64 / viewport_w.max(1.0)).clamp(0.25, 4.0);
        let scale_y = ((rect.bottom - rect.top) as f64 / viewport_h.max(1.0)).clamp(0.25, 4.0);
        let ok = unsafe {
            SetWindowPos(
                child,
                std::ptr::null_mut(),
                (x_px.max(0) as f64 * scale_x).round() as i32,
                (y_px.max(0) as f64 * scale_y).round() as i32,
                (width_px.max(MIN_DOCK_WIDTH as i32) as f64 * scale_x).round() as i32,
                (height_px.max(MIN_DOCK_HEIGHT as i32) as f64 * scale_y).round() as i32,
                SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS | SWP_NOZORDER,
            )
        };
        if ok == 0 {
            return Err("SetWindowPos failed".into());
        }
        Ok(())
    }
}

pub fn detach(info: &EmbedInfo) {
    let child = info.hwnd as HWND;
    if unsafe { IsWindow(child) } == 0 {
        return;
    }
    unsafe {
        if info.overlay {
            SetWindowLongPtrW(child, GWL_HWNDPARENT, 0);
        } else {
            let _ = SetParent(child, std::ptr::null_mut());
        }
        SetWindowLongPtrW(child, GWL_STYLE, info.style);
        SetWindowLongPtrW(child, GWL_EXSTYLE, info.exstyle);
        let _ = SetWindowPos(
            child,
            std::ptr::null_mut(),
            0,
            0,
            0,
            0,
            SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_NOZORDER
                | SWP_NOACTIVATE
                | SWP_FRAMECHANGED
                | SWP_SHOWWINDOW,
        );
        let _ = ShowWindow(child, SW_SHOW);
    }
}

pub fn window_alive(handle: isize) -> bool {
    unsafe { IsWindow(handle as HWND) != 0 }
}

pub fn process_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        let _ = CloseHandle(handle);
        ok != 0 && code == STILL_ACTIVE as u32
    }
}

/// True only when the tracked PID still points at blender.exe. Guards against
/// PID reuse after Blender has already exited.
pub fn is_blender_process(pid: u32) -> bool {
    use windows_sys::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut buffer = [0u16; 1024];
        let mut length = buffer.len() as u32;
        let ok = QueryFullProcessImageNameW(
            handle,
            PROCESS_NAME_WIN32,
            buffer.as_mut_ptr(),
            &mut length,
        );
        let _ = CloseHandle(handle);
        if ok == 0 || length == 0 {
            return false;
        }
        let path = String::from_utf16_lossy(&buffer[..length as usize]);
        path.rsplit(|character| character == '\\' || character == '/')
            .next()
            .map(|name| name.eq_ignore_ascii_case("blender.exe"))
            .unwrap_or(false)
    }
}

/// Ask Blender to close normally (so it can prompt to save unsaved work).
/// Returns false when no Blender window could be found.
pub fn request_close(pid: u32) -> bool {
    let Some(hwnd) = find_window_for_pid_including_hidden(pid) else {
        return false;
    };
    if unsafe { IsWindow(hwnd) } == 0 {
        return false;
    }
    unsafe { PostMessageW(hwnd, WM_CLOSE, 0, 0) != 0 }
}

/// Last-resort cleanup for a Blender process that has no window (for example
/// one still starting up when the app is closed).
pub fn terminate_process(pid: u32) {
    unsafe {
        let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if handle.is_null() {
            return;
        }
        let _ = TerminateProcess(handle, 0);
        let _ = CloseHandle(handle);
    }
}

pub fn set_visible(info: &EmbedInfo, visible: bool) {
    let child = info.hwnd as HWND;
    unsafe {
        let _ = ShowWindow(child, if visible { SW_SHOW } else { SW_HIDE });
    }
}

pub fn focus(info: &EmbedInfo) {
    let child = info.hwnd as HWND;
    if unsafe { IsWindow(child) } == 0 {
        return;
    }
    unsafe {
        // Cross-process focus requires sharing the input state with the window's
        // thread; SetFocus alone silently fails for another process.
        let foreground = GetForegroundWindow();
        let foreground_thread = if foreground.is_null() {
            0
        } else {
            GetWindowThreadProcessId(foreground, std::ptr::null_mut())
        };
        let mut pid = 0u32;
        let child_thread = GetWindowThreadProcessId(child, &mut pid);
        let attached = foreground_thread != 0
            && child_thread != 0
            && foreground_thread != child_thread
            && AttachThreadInput(foreground_thread, child_thread, 1) != 0;
        let _ = SetForegroundWindow(child);
        let _ = BringWindowToTop(child);
        let _ = SetFocus(child);
        if attached {
            let _ = AttachThreadInput(foreground_thread, child_thread, 0);
        }
    }
}

/// Returns `Some(true)` when the docked region is effectively a black/empty
/// rectangle, `Some(false)` when real content is visible, and `None` when the
/// check cannot be trusted (another app is in front, the window is hidden, or
/// the screen capture failed).
///
/// This is a safety net for GPUs/drivers where an owned Blender window reports
/// a valid size but never composites. The app then falls back to a separate
/// Blender window instead of leaving a black viewport.
pub fn looks_black(info: &EmbedInfo) -> Option<bool> {
    let child = info.hwnd as HWND;
    let parent = info.parent as HWND;
    unsafe {
        if IsWindow(child) == 0 || IsWindow(parent) == 0 {
            return Some(true);
        }
        if IsIconic(parent) != 0 || IsWindowVisible(parent) == 0 {
            return None;
        }
        let foreground = GetForegroundWindow();
        if foreground != parent && foreground != child {
            return None;
        }

        let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
        if GetWindowRect(child, &mut rect) == 0 {
            return None;
        }
        let vx = GetSystemMetrics(SM_XVIRTUALSCREEN);
        let vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
        let vw = GetSystemMetrics(SM_CXVIRTUALSCREEN);
        let vh = GetSystemMetrics(SM_CYVIRTUALSCREEN);
        let left = rect.left.max(vx);
        let top = rect.top.max(vy);
        let right = rect.right.min(vx + vw);
        let bottom = rect.bottom.min(vy + vh);
        let width = right - left;
        let height = bottom - top;
        if width < 80 || height < 60 {
            return Some(true);
        }

        let screen = GetDC(std::ptr::null_mut());
        if screen.is_null() {
            return None;
        }
        let memory = CreateCompatibleDC(screen);
        if memory.is_null() {
            let _ = ReleaseDC(std::ptr::null_mut(), screen);
            return None;
        }
        let bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: width,
                biHeight: -height,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: 0,
                biSizeImage: 0,
                biXPelsPerMeter: 0,
                biYPelsPerMeter: 0,
                biClrUsed: 0,
                biClrImportant: 0,
            },
            bmiColors: [RGBQUAD {
                rgbBlue: 0,
                rgbGreen: 0,
                rgbRed: 0,
                rgbReserved: 0,
            }],
        };
        let mut bits: *mut std::ffi::c_void = std::ptr::null_mut();
        let dib = CreateDIBSection(
            memory,
            &bmi,
            DIB_RGB_COLORS,
            &mut bits,
            std::ptr::null_mut(),
            0,
        );
        if dib.is_null() || bits.is_null() {
            let _ = DeleteDC(memory);
            let _ = ReleaseDC(std::ptr::null_mut(), screen);
            return None;
        }
        let old = SelectObject(memory, dib as HGDIOBJ);
        let copied = BitBlt(
            memory,
            0,
            0,
            width,
            height,
            screen,
            left,
            top,
            SRCCOPY,
        );
        let mut lit = 0usize;
        let mut sampled = 0usize;
        if copied != 0 {
            let data = std::slice::from_raw_parts(
                bits as *const u8,
                (width as usize) * (height as usize) * 4,
            );
            let step = 2usize;
            let mut y = 0usize;
            while y < height as usize {
                let mut x = 0usize;
                while x < width as usize {
                    let index = (y * width as usize + x) * 4;
                    let blue = data[index];
                    let green = data[index + 1];
                    let red = data[index + 2];
                    if red.max(green).max(blue) > 24 {
                        lit += 1;
                    }
                    sampled += 1;
                    x += step;
                }
                y += step;
            }
        }
        let _ = SelectObject(memory, old);
        let _ = DeleteObject(dib as HGDIOBJ);
        let _ = DeleteDC(memory);
        let _ = ReleaseDC(std::ptr::null_mut(), screen);

        if copied == 0 || sampled == 0 {
            return None;
        }
        // Blender's chrome (top bar, headers, icons) always produces far more
        // than 0.2% lit pixels; the empty app stage (#0b0b0d) produces none.
        Some(lit * 500 < sampled)
    }
}
