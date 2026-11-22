# Shared console helpers for the build and test scripts.
# Keeping the window on top makes long builds and test runs easy to watch.

function Set-AlwaysOnTop {
    try {
        if (-not ('Win32.ConsoleTop' -as [type])) {
            Add-Type -Namespace Win32 -Name ConsoleTop -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
'@
        }
        $hwnd = [Win32.ConsoleTop]::GetConsoleWindow()
        if ($hwnd -ne [IntPtr]::Zero) {
            # HWND_TOPMOST (-1), SWP_NOMOVE | SWP_NOSIZE (0x0003)
            [Win32.ConsoleTop]::SetWindowPos($hwnd, [IntPtr](-1), 0, 0, 0, 0, 0x0003) | Out-Null
        }
    } catch {
        # Not running in a console host; ignore.
    }
}

function Set-ConsoleTitle([string]$title) {
    try { $Host.UI.RawUI.WindowTitle = $title } catch { }
}
