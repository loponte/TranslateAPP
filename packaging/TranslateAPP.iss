; Inno Setup: gera dist\TranslateAPP-Setup.exe a partir do onedir do PyInstaller (scripts\build_win.ps1).
; Instala por usuário (sem pedir admin), em %LOCALAPPDATA%\Programs\TranslateAPP.
[Setup]
AppId={{6F1B7C2E-3D54-4A8B-9E21-7C0A5B4D8E13}
AppName=TranslateAPP
AppVersion=0.3.0
AppPublisher=Loponte
DefaultDirName={autopf}\TranslateAPP
DefaultGroupName=TranslateAPP
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=TranslateAPP-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\TranslateAPP.exe
WizardStyle=modern

[Files]
Source: "..\dist\TranslateAPP\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\TranslateAPP"; Filename: "{app}\TranslateAPP.exe"
Name: "{autodesktop}\TranslateAPP"; Filename: "{app}\TranslateAPP.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na area de trabalho"; Flags: unchecked

[Run]
Filename: "{app}\TranslateAPP.exe"; Description: "Abrir o TranslateAPP"; Flags: nowait postinstall skipifsilent
