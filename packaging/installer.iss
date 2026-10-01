; Munyun Lab Windows installer (ADR-76). Built by packaging/build_installer.py (Inno Setup 6, ISCC):
;   ISCC /DAppVersion=0.2.0 /DBuildNumber=11 /DFileVersion=0.2.0.11 /DSourceDir=dist\EdgeLab
;        /DOutputDir=dist\release /DOutputBase=MunyunLab-Setup-0.2.0-b11 /DIconFile=packaging\munyun.ico installer.iss
;
; Per user, no administrator rights. The program goes to %LOCALAPPDATA%\Programs\EdgeLab: exactly the folder the
; built-in updater replaces, so "Restart and update" keeps working after an installer install. The uninstaller lives
; NEXT to it (EdgeLab-Uninstall) so an auto-update (which swaps the whole program folder) never removes it. Research
; workspaces, settings and logs live elsewhere and are never touched by install, update or uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BuildNumber
  #define BuildNumber "0"
#endif
#ifndef FileVersion
  #define FileVersion AppVersion + ".0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\EdgeLab"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\release"
#endif
#ifndef OutputBase
  #define OutputBase "MunyunLab-Setup"
#endif
#ifndef IconFile
  #define IconFile "munyun.ico"
#endif

[Setup]
AppId={{8C3E2F6B-5A1D-4E7B-9C2A-7F4D1B6E3A90}
AppName=Munyun Lab
AppVersion={#AppVersion} (build {#BuildNumber})
AppVerName=Munyun Lab {#AppVersion} build {#BuildNumber}
AppPublisher=Munyun Lab
VersionInfoVersion={#FileVersion}
VersionInfoProductName=Munyun Lab
VersionInfoDescription=Munyun Lab installer
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\EdgeLab
DisableDirPage=yes
DisableProgramGroupPage=yes
UninstallFilesDir={localappdata}\Programs\EdgeLab-Uninstall
UninstallDisplayName=Munyun Lab
UninstallDisplayIcon={app}\EdgeLab.exe
SetupIconFile={#IconFile}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBase}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "startmenuicon"; Description: "Add Munyun Lab to the Start menu (searchable; right-click it there to pin it to the taskbar)"; GroupDescription: "Shortcuts:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; a fresh copy of the program every time (an older build's files never linger next to the new ones)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Munyun Lab"; Filename: "{app}\EdgeLab.exe"; WorkingDir: "{app}"; Comment: "Munyun Lab research app"; Tasks: startmenuicon
Name: "{autodesktop}\Munyun Lab"; Filename: "{app}\EdgeLab.exe"; WorkingDir: "{app}"; Comment: "Munyun Lab research app"; Tasks: desktopicon

[Run]
Filename: "{app}\EdgeLab.exe"; Description: "Start Munyun Lab now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; whatever build is installed now (auto-updates replace the folder), plus leftovers of interrupted updates
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{app}.old-*"
Type: filesandordirs; Name: "{app}.new-*"
Type: filesandordirs; Name: "{app}.failed-*"
