; Inno Setup script for BrowseTerm's Windows installer.
; Wraps the single PyInstaller-built BrowseTerm.exe (dist/BrowseTerm.exe, built with
; --onefile --windowed) in a real Windows installer: Program Files placement, a Start Menu
; shortcut, and an uninstaller - PyInstaller only produces the raw .exe, not an installer.
; Run from the repo root: ISCC packaging\windows\installer.iss
; (paths below are relative to this .iss file's own location, per Inno Setup's #define SourcePath)

#define MyAppName "BrowseTerm"
#define MyAppVersion GetEnv("BROWSETERM_VERSION")
#if MyAppVersion == ""
  #define MyAppVersion "0.0.0"
#endif
#define MyAppExeName "BrowseTerm.exe"

[Setup]
AppId={{B7B4E3A1-6F1D-4C9E-9A2B-BROWSETERM01}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=BrowseTerm-Setup
SetupIconFile=..\icons\logo.ico
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
