# Windows Defender and SmartScreen

Blendermp is an unsigned hobby build. Windows treats every new unsigned exe as
unknown, so a PC that has never run it can refuse to start it:

- "Windows protected your PC" (SmartScreen)
- "This content is blocked for your protection. Your administrator is not
  allowing you to access content."

Nothing is wrong with the download; the file simply has no reputation yet.
There are three ways through, fastest first.

## 1. Use the Blender-only addon (no exe, nothing to block)

`blender_multiplayer-standalone-<version>.zip` installs inside Blender and
talks to the same relay as the desktop app. It needs no installer, no
administrator rights and no Defender exception. Install it with
**Edit > Preferences > Add-ons > Install from Disk**.

## 2. Unblock the downloaded file

1. Right-click `Blendermp-Updater.exe` (or the Setup exe) and choose
   **Properties**.
2. Tick **Unblock** at the bottom of the General tab, then **OK**.
3. Run it again.

If Windows still refuses, the file may have been quarantined. Open **Windows
Security > Virus & threat protection > Protection history**, find the
Blendermp entry, and choose **Actions > Restore** (or **Allow on device**).

## 3. Add a folder exclusion

Only do this for a folder you control, for example
`%LOCALAPPDATA%\Programs\Blendermp`.

1. Open **Windows Security > Virus & threat protection**.
2. Under **Virus & threat protection settings**, click **Manage settings**.
3. Scroll to **Exclusions**, click **Add or remove exclusions > Add an
   exclusion > Folder**.
4. Pick the Blendermp install folder.

PowerShell (run as administrator) can do the same:

```powershell
Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\Programs\Blendermp"
```

Remove it later with `Remove-MpPreference -ExclusionPath <path>`.

## Report it as a false positive

Microsoft clears these faster when several people report the same file. Upload
the exe at <https://www.microsoft.com/en-us/wdsi/filesubmission> and choose
**I believe this file is safe** (software developer). Include the release URL
and a one-line description: "Open-source Blender multiplayer app; unsigned
Tauri build; source at https://github.com/Tribbninja/blendermp".

The permanent fix is code signing. Once the build has an Authenticode
certificate, SmartScreen warnings stop for new releases after the certificate
builds reputation.
