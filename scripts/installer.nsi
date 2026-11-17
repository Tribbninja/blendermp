; Blendermp Windows installer.
; Built from the repository root with: makensis scripts\installer.nsi
Unicode true

!include "MUI2.nsh"

!define APPNAME "Blendermp"
!define VERSION "0.1.0"
!define PUBLISHER "Blendermp"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Blendermp"

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
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Function .onInit
    ; WebView2 ships with Windows 11, but warn on older machines.
    ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
    StrCmp $0 "" 0 webview_ok
    ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
    StrCmp $0 "" 0 webview_ok
    MessageBox MB_ICONEXCLAMATION|MB_OK "Microsoft WebView2 is required. Install it from https://developer.microsoft.com/microsoft-edge/webview2/ and run this setup again."
    webview_ok:
FunctionEnd

Section "Blendermp" SecMain
    SectionIn RO
    SetOutPath "$INSTDIR"
    File "..\dist\blendermp\blendermp.exe"
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
    Delete "$INSTDIR\README.md"
    Delete "$INSTDIR\SECURITY.md"
    Delete "$INSTDIR\LICENSE"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
SectionEnd
