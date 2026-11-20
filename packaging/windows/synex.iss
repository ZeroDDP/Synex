; Установщик Synex для Windows (Inno Setup 6). Его вызывает build.ps1 после build.py:
;   iscc /DAppVersion=0.1.0 packaging\windows\synex.iss
; Ставит программу для текущего пользователя (без прав администратора) в
; %LOCALAPPDATA%\Programs\Synex, ярлык в меню Пуск. Данные пользователя (ключи,
; подключения, synex.env) лежат в %LOCALAPPDATA%\Synex и при удалении остаются.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0B7C5A-3F1D-4F7B-9C7E-5A1D2B9F0E11}
AppName=Synex
AppVersion={#AppVersion}
AppVerName=Synex {#AppVersion}
AppPublisher=ZeroDDP
AppPublisherURL=https://github.com/ZeroDDP/Synex
DefaultDirName={localappdata}\Programs\Synex
DefaultGroupName=Synex
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Synex-{#AppVersion}-windows-x64-setup
SetupIconFile=..\assets\synex.ico
UninstallDisplayIcon={app}\Synex.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Synex\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Synex"; Filename: "{app}\Synex.exe"
Name: "{autodesktop}\Synex"; Filename: "{app}\Synex.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Synex.exe"; Description: "{cm:LaunchProgram,Synex}"; Flags: nowait postinstall skipifsilent
