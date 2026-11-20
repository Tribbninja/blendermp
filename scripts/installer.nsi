; Blendermp Windows installer.
; Built from the repository root with: makensis scripts\installer.nsi
Unicode true

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "blender-detect.nsh"

!define APPNAME "Blendermp"
!define VERSION "0.1.4"
!define PUBLISHER "Blendermp"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Blendermp"

; Official Blender build. Pinned version and checksum so setup never runs an
; unverified binary. Update all three values together.
!define BLENDER_VERSION "5.3.0"
!define BLENDER_URL "https://download.blender.org/release/Blender5.3/blender-5.3.0-windows-x64.msi"
!define BLENDER_SHA256 "3ff4ec8a488be1148aa2bdb2cb229be21b8050e75931da617934cac254dd726c"

Name "${APPNAME} ${VERSION}"
OutFile "..\dist\Blendermp-${VERSION}-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Blendermp"
InstallDirRegKey HKCU "Software\Blendermp" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

!define MUI_ABORTWARNING
!define MUI_ICON "..\desktop\src-tauri\icons\icon.ico"
!define MUI_UNICON "..\desktop\src-tauri\icons\icon.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\blendermp.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start Blendermp"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page custom BlenderPageShow BlenderPageLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Var NeedBlender
Var BlenderFailed
Var BlenderExitCode
Var BlenderDialog
Var BlenderBar
Var BlenderStatus
Var BlenderProgressFile
Var BlenderStatusFile
Var BlenderExitFile

; --------------------------------------------------------------------------
; Setup
; --------------------------------------------------------------------------
Function .onInit
    ; WebView2 ships with Windows 11, but warn on older machines.
    ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
    StrCmp $0 "" 0 webview_ok
    ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
    StrCmp $0 "" 0 webview_ok
    MessageBox MB_ICONEXCLAMATION|MB_OK "Microsoft WebView2 is required. Install it from https://developer.microsoft.com/microsoft-edge/webview2/ and run this setup again."
    webview_ok:

    Call DetectBlender
    StrCmp $BlenderFound "1" blender_ready
    IfSilent blender_ready
    MessageBox MB_YESNO|MB_ICONQUESTION "Blender 5.x was not found on this PC.$\r$\n$\r$\nBlendermp needs Blender 5 or newer to open and sync scenes. Download and install Blender ${BLENDER_VERSION} from blender.org now?$\r$\n$\r$\nThis downloads about 366 MB. Windows will ask for administrator permission. Choose No to install Blendermp only." IDNO blender_ready
    StrCpy $NeedBlender "1"
    blender_ready:
FunctionEnd

; --------------------------------------------------------------------------
; Optional Blender bootstrap page
; --------------------------------------------------------------------------
Function BlenderPageShow
    ${If} $NeedBlender != "1"
        Abort
    ${EndIf}

    !insertmacro MUI_HEADER_TEXT "Installing Blender" "Setup is downloading the official Blender build and installing it silently."

    nsDialogs::Create 1018
    Pop $BlenderDialog
    ${If} $BlenderDialog == error
        Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 28u "Blendermp needs Blender. Setup is downloading Blender ${BLENDER_VERSION} from blender.org and installing it in the background. This can take a few minutes."
    Pop $0

    ${NSD_CreateProgressBar} 0 36u 100% 12u ""
    Pop $BlenderBar
    ${NSD_ProgressBar_SetRange32} $BlenderBar 0 100
    ${NSD_ProgressBar_SetPos} $BlenderBar 0

    ${NSD_CreateLabel} 0 58u 100% 48u "Starting download..."
    Pop $BlenderStatus

    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File "blender-bootstrap.ps1"

    StrCpy $BlenderProgressFile "$PLUGINSDIR\blender-progress.txt"
    StrCpy $BlenderStatusFile "$PLUGINSDIR\blender-status.txt"
    StrCpy $BlenderExitFile "$PLUGINSDIR\blender-exit.txt"
    Delete "$BlenderProgressFile"
    Delete "$BlenderStatusFile"
    Delete "$BlenderExitFile"
    StrCpy $BlenderExitCode ""

    GetDlgItem $0 $HWNDPARENT 1
    EnableWindow $0 0
    GetDlgItem $0 $HWNDPARENT 2
    EnableWindow $0 0
    GetDlgItem $0 $HWNDPARENT 3
    EnableWindow $0 0

    ; Sysnative gives the 64-bit PowerShell from this 32-bit installer, so the
    ; Program Files lookup and msiexec architecture match the x64 Blender MSI.
    Exec '"$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$PLUGINSDIR\blender-bootstrap.ps1" -Url "${BLENDER_URL}" -Sha256 "${BLENDER_SHA256}" -MsiPath "$PLUGINSDIR\blender-setup.msi" -ProgressFile "$BlenderProgressFile" -StatusFile "$BlenderStatusFile" -ExitFile "$BlenderExitFile" -LogFile "$PLUGINSDIR\blender.log"'
    IfErrors 0 blender_started
        FileOpen $0 "$BlenderExitFile" w
        FileWrite $0 "20"
        FileClose $0
    blender_started:

    ${NSD_CreateTimer} BlenderTimer 250
    nsDialogs::Show
FunctionEnd

Function BlenderTimer
    StrCmp $BlenderExitCode "" blender_timer_read
        ${NSD_KillTimer} BlenderTimer
        Return
    blender_timer_read:

    IfFileExists "$BlenderProgressFile" 0 blender_timer_status
        FileOpen $0 "$BlenderProgressFile" r
        FileRead $0 $1
        FileClose $0
        StrCmp $1 "" blender_timer_status
        ${NSD_ProgressBar_SetPos} $BlenderBar $1
    blender_timer_status:

    IfFileExists "$BlenderStatusFile" 0 blender_timer_exit
        FileOpen $0 "$BlenderStatusFile" r
        FileRead $0 $1
        FileClose $0
        StrCmp $1 "" blender_timer_exit
        ${NSD_SetText} $BlenderStatus $1
    blender_timer_exit:

    IfFileExists "$BlenderExitFile" 0 blender_timer_done
        FileOpen $0 "$BlenderExitFile" r
        FileRead $0 $1
        FileClose $0
        StrCpy $BlenderExitCode $1
        ${NSD_KillTimer} BlenderTimer
        ${If} $BlenderExitCode == "0"
            ${NSD_ProgressBar_SetPos} $BlenderBar 100
            ${NSD_SetText} $BlenderStatus "Blender ${BLENDER_VERSION} is installed. Click Next to install Blendermp."
        ${Else}
            ${NSD_SetText} $BlenderStatus "Blender could not be installed automatically. Click Next to install Blendermp, then install Blender from blender.org and set its path in Settings."
            StrCpy $BlenderFailed "1"
        ${EndIf}
        GetDlgItem $0 $HWNDPARENT 1
        EnableWindow $0 1
        GetDlgItem $0 $HWNDPARENT 2
        EnableWindow $0 1
    blender_timer_done:
FunctionEnd

Function BlenderPageLeave
    ${If} $BlenderExitCode == ""
        MessageBox MB_OK|MB_ICONEXCLAMATION "Blender setup is still running. Please wait for it to finish."
        Abort
    ${EndIf}
    ${NSD_KillTimer} BlenderTimer
FunctionEnd

Function .onInstSuccess
    ${If} $BlenderFailed == "1"
        MessageBox MB_OK|MB_ICONEXCLAMATION "Blendermp is installed, but Blender could not be installed automatically.$\r$\n$\r$\nInstall Blender from https://www.blender.org/download/ and choose its blender.exe in Blendermp Settings."
    ${EndIf}
FunctionEnd

; --------------------------------------------------------------------------
; Payload
; --------------------------------------------------------------------------
Section "Blendermp" SecMain
    SectionIn RO
    SetOutPath "$INSTDIR"
    File "..\dist\blendermp\blendermp.exe"
    File "..\dist\blendermp\WebView2Loader.dll"
    File "..\dist\blendermp\README.md"
    File "..\dist\blendermp\SECURITY.md"
    File "..\dist\blendermp\LICENSE"

    SetOutPath "$INSTDIR\addon"
    File /r "..\dist\blendermp\addon\*.*"

    WriteUninstaller "$INSTDIR\Uninstall.exe"
    WriteRegStr HKCU "Software\Blendermp" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
    WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${PUBLISHER}"
    WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\blendermp.exe"
    WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1

    CreateDirectory "$SMPROGRAMS\Blendermp"
    CreateShortcut "$SMPROGRAMS\Blendermp\Blendermp.lnk" "$INSTDIR\blendermp.exe"
    CreateShortcut "$SMPROGRAMS\Blendermp\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
    CreateShortcut "$DESKTOP\Blendermp.lnk" "$INSTDIR\blendermp.exe"
SectionEnd

Section "Blender extension (optional)" SecExtension
    SetOutPath "$INSTDIR\extension"
    File "..\dist\blender_multiplayer-${VERSION}.zip"
SectionEnd

Section "Uninstall"
    Delete "$DESKTOP\Blendermp.lnk"
    Delete "$SMPROGRAMS\Blendermp\Blendermp.lnk"
    Delete "$SMPROGRAMS\Blendermp\Uninstall.lnk"
    RMDir "$SMPROGRAMS\Blendermp"
    DeleteRegKey HKCU "${UNINSTKEY}"
    DeleteRegKey HKCU "Software\Blendermp"
    RMDir /r "$INSTDIR\addon"
    RMDir /r "$INSTDIR\extension"
    Delete "$INSTDIR\blendermp.exe"
    Delete "$INSTDIR\WebView2Loader.dll"
    Delete "$INSTDIR\README.md"
    Delete "$INSTDIR\SECURITY.md"
    Delete "$INSTDIR\LICENSE"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
SectionEnd
