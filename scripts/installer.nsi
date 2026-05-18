; Tune Server — NSIS Installer
; Built by CI, version injected via /DVERSION=x.y.z

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"

; ---------------------------------------------------------------------------
; Version metadata — embedded in the .exe PE headers.
; Windows SmartScreen uses FileDescription, CompanyName, and ProductName to
; build publisher reputation over time. Without these fields the .exe is
; treated as "unknown publisher" forever and every download triggers a
; warning dialog.
;
; VIProductVersion requires exactly 4 dot-separated numbers (major.minor.patch.build).
; Our version is 3-part (e.g. 0.7.108), so we append ".0".
; ---------------------------------------------------------------------------
VIProductVersion "${VERSION}.0"
VIFileVersion "${VERSION}.0"

VIAddVersionKey /LANG=0 "ProductName" "Tune Server"
VIAddVersionKey /LANG=0 "CompanyName" "MozAIk Labs"
VIAddVersionKey /LANG=0 "LegalCopyright" "© 2024-2026 MozAIk Labs (mozaiklabs.fr)"
VIAddVersionKey /LANG=0 "FileDescription" "Tune Server — Serveur audio Hi-Res multi-room"
VIAddVersionKey /LANG=0 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=0 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=0 "OriginalFilename" "tune-server-${VERSION}-windows-setup.exe"
VIAddVersionKey /LANG=0 "InternalName" "tune-server-setup"

Name "Tune Server ${VERSION}"
OutFile "tune-server-${VERSION}-windows-setup.exe"
InstallDir "$LOCALAPPDATA\Tune Server"
InstallDirRegKey HKCU "Software\MozAIkLabs\Tune Server" "InstallDir"

; Per-user install (no UAC prompt). Combined with HKCU registry and
; $LOCALAPPDATA install dir, the installer never triggers the UAC
; elevation dialog — reducing friction on machines where the user
; does not have admin rights.
RequestExecutionLevel user

; --- UI ---
!define MUI_ICON "AppIcon.ico"
!define MUI_UNICON "AppIcon.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Installation de Tune Server ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "Tune Server est un lecteur audio Hi-Res multi-room.$\r$\n$\r$\nL'installation copie les fichiers et crée des raccourcis.$\r$\nAucun redémarrage n'est nécessaire."
!define MUI_FINISHPAGE_RUN "$INSTDIR\start-tune-server.bat"
!define MUI_FINISHPAGE_RUN_TEXT "Démarrer Tune Server"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "French"

; --- Install ---
Section "Install"
    ; Kill running instance before overwriting files
    ; 1. Kill the batch launcher window (cmd.exe running start-tune-server.bat)
    nsExec::ExecToLog 'cmd /c taskkill /F /FI "WINDOWTITLE eq Tune Server"'
    ; 2. Kill the server binary
    nsExec::ExecToLog 'taskkill /F /IM tune-server.exe'
    nsExec::ExecToLog 'taskkill /F /IM "Tune Server.exe"'
    nsExec::ExecToLog 'taskkill /F /IM librespot.exe'
    ; 3. Fallback: kill anything on port 8888
    nsExec::ExecToLog 'cmd /c for /f "tokens=5" %p in (''netstat -ano ^| findstr :8888 ^| findstr LISTENING'') do taskkill /F /PID %p'
    Sleep 3000

    SetOutPath "$INSTDIR"

    ; Core files
    File /r "dist\tune-server\*.*"

    ; Store install dir
    WriteRegStr HKCU "Software\MozAIkLabs\Tune Server" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "Software\MozAIkLabs\Tune Server" "Version" "${VERSION}"

    ; Uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Add/Remove Programs entry
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "DisplayName" "Tune Server ${VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "DisplayIcon" "$INSTDIR\AppIcon.ico"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "Publisher" "MozAIk Labs"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "DisplayVersion" "${VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "URLInfoAbout" "https://mozaiklabs.fr"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "URLUpdateInfo" "https://github.com/renesenses/tune-server-linux/releases"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "NoRepair" 1
    ; Estimated size in KB (helps Add/Remove Programs show disk usage)
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "EstimatedSize" $0

    ; Start Menu
    CreateDirectory "$SMPROGRAMS\Tune Server"
    CreateShortCut "$SMPROGRAMS\Tune Server\Tune Server.lnk" "$INSTDIR\start-tune-server.bat" \
        "" "$INSTDIR\AppIcon.ico"
    CreateShortCut "$SMPROGRAMS\Tune Server\Désinstaller.lnk" "$INSTDIR\uninstall.exe"

    ; Desktop shortcut
    CreateShortCut "$DESKTOP\Tune Server.lnk" "$INSTDIR\start-tune-server.bat" \
        "" "$INSTDIR\AppIcon.ico"
SectionEnd

; --- Uninstall ---
Section "Uninstall"
    ; Kill running instance
    nsExec::ExecToLog 'taskkill /F /IM tune-server.exe'

    ; Remove files
    RMDir /r "$INSTDIR"

    ; Shortcuts
    Delete "$SMPROGRAMS\Tune Server\Tune Server.lnk"
    Delete "$SMPROGRAMS\Tune Server\Désinstaller.lnk"
    RMDir "$SMPROGRAMS\Tune Server"
    Delete "$DESKTOP\Tune Server.lnk"

    ; Registry
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer"
    DeleteRegKey HKCU "Software\MozAIkLabs\Tune Server"
SectionEnd
