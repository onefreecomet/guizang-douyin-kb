; 归藏 · 抖音收藏知识库 — Inno Setup 安装脚本
; 由 tools\make_installer.ps1 调用（它负责先拼好 build\payload）
; 直接编译：ISCC.exe tools\installer.iss

#define AppName "归藏 · 抖音收藏知识库"
#define AppShortName "归藏"
#define AppVersion "1.0.0"
#define AppPublisher "冰临城下"
#define AppUrl "https://github.com/"
#define SourceRoot "..\build\payload"

[Setup]
AppId={{8C3F1A64-2B7D-4E19-9A55-6D2E7C4B18A1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} 安装程序
VersionInfoCompany={#AppPublisher}
DefaultDirName={localappdata}\Programs\Guizang
DefaultGroupName={#AppShortName}
DisableProgramGroupPage=yes
UsePreviousAppDir=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=归藏Setup-{#AppVersion}
SetupIconFile=..\app\icon.ico
UninstallDisplayIcon={app}\app\icon.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0
; 用户数据（work 目录、下载目录、Obsidian 库）一律不动

[Languages]
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce
Name: "startup"; Description: "开机自动在后台守护（推荐）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppShortName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\icon.ico"; Comment: "归藏 · 抖音收藏知识库 — by 冰临城下"
Name: "{autodesktop}\{#AppShortName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\icon.ico"; Comment: "归藏 · 抖音收藏知识库 — by 冰临城下"; Tasks: desktopicon
Name: "{userstartup}\归藏守护"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\app\watchdog.py"" --interval 30"; WorkingDir: "{app}"; IconFilename: "{app}\app\icon.ico"; Tasks: startup

[Run]
Filename: "{app}\app\start.bat"; Description: "现在启动 {#AppShortName}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallRun]
Filename: "{app}\runtime\python\python.exe"; Parameters: """{app}\app\uninstall_cleanup.py"""; Flags: runhidden; RunOnceId: "GuizangCleanup"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\references"
Type: filesandordirs; Name: "{app}\scripts"
Type: files; Name: "{app}\INSTALL.md"
Type: dirifempty; Name: "{app}"
