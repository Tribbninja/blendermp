; Shared Blender detection for the installer (and the detection self-test).
!ifndef BLENDER_DETECT_NSH
!define BLENDER_DETECT_NSH

!include "FileFunc.nsh"

Var BlenderFound

Function CheckBlenderRoot
    IfFileExists "$R9\blender.exe" 0 cbr_scan
        ${GetFileVersion} "$R9\blender.exe" $6
        StrCpy $7 $6 1
        StrCmp $7 "5" 0 cbr_scan
        StrCpy $BlenderFound "1"
        Return
    cbr_scan:
    FindFirst $0 $1 "$R9\*"
    cbr_loop:
        StrCmp $1 "" cbr_done
        IfFileExists "$R9\$1\blender.exe" 0 cbr_next
            ${GetFileVersion} "$R9\$1\blender.exe" $6
            StrCpy $7 $6 1
            StrCmp $7 "5" cbr_accept
            ; Some portable builds omit version resources; fall back to the
            ; folder name ("Blender 5.3" or "blender-5.3.0-windows-x64").
            StrCmp $6 "" 0 cbr_next
            StrCpy $7 $1 9
            StrCmp $7 "Blender 5" cbr_accept
            StrCmp $7 "blender-5" cbr_accept
            Goto cbr_next
        cbr_accept:
            StrCpy $BlenderFound "1"
            Goto cbr_done
        cbr_next:
        FindNext $0 $1
        Goto cbr_loop
    cbr_done:
    FindClose $0
FunctionEnd

Function DetectBlender
    StrCpy $BlenderFound "0"

    StrCpy $R9 "$PROGRAMFILES64\Blender Foundation"
    Call CheckBlenderRoot
    StrCmp $BlenderFound "1" detect_done

    StrCpy $R9 "$PROGRAMFILES\Blender Foundation"
    Call CheckBlenderRoot
    StrCmp $BlenderFound "1" detect_done

    StrCpy $R9 "$LOCALAPPDATA\Programs\Blender Foundation"
    Call CheckBlenderRoot
    StrCmp $BlenderFound "1" detect_done

    StrCpy $R9 "$PROGRAMFILES\Steam\steamapps\common\Blender"
    Call CheckBlenderRoot
    StrCmp $BlenderFound "1" detect_done
    StrCpy $R9 "$PROGRAMFILES64\Steam\steamapps\common\Blender"
    Call CheckBlenderRoot
    StrCmp $BlenderFound "1" detect_done

    ; MSI installs can use a custom INSTALL_ROOT; read the uninstall entry.
    SetRegView 64
    StrCpy $2 0
    detect_reg_loop:
        EnumRegKey $3 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall" $2
        StrCmp $3 "" detect_reg_done
        ReadRegStr $4 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\$3" "DisplayName"
        StrCpy $5 $4 7
        StrCmp $5 "Blender" 0 detect_reg_next
        ReadRegStr $6 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\$3" "DisplayVersion"
        StrCpy $7 $6 1
        StrCmp $7 "5" 0 detect_reg_next
        ReadRegStr $4 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\$3" "InstallLocation"
        StrCmp $4 "" detect_reg_next
        IfFileExists "$4\blender.exe" detect_found
        IfFileExists "$4blender.exe" detect_found
    detect_reg_next:
        IntOp $2 $2 + 1
        Goto detect_reg_loop
    detect_reg_done:
    SetRegView lastused

    Goto detect_done
    detect_found:
        StrCpy $BlenderFound "1"
    detect_done:
FunctionEnd

!endif
