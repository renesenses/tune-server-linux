; Tune Server — NSIS Installer
; Built by CI, version injected via /DVERSION=x.y.z

Unicode true
!include "MUI2.nsh"

Name "Tune Server ${VERSION}"
OutFile "tune-server-${VERSION}-windows-setup.exe"
InstallDir "$LOCALAPPDATA\Tune Server"
InstallDirRegKey HKCU "Software\MozAIkLabs\Tune Server" "InstallDir"
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
    nsExec::ExecToLog 'taskkill /F /IM tune-server.exe'
    nsExec::ExecToLog 'taskkill /F /IM librespot.exe'
    Sleep 2000

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
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TuneServer" \
        "NoRepair" 1

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
