; FinResearch 安装包脚本（Inno Setup 6）
; 由 build_installer.bat 调用 ISCC 编译 → packaging/Output/FinResearch-Setup.exe
; 打包 PyInstaller onedir 产物（仅运行时，无源码/无 node_modules）。

#define MyAppName "景渊金融研究助手"
#define MyAppNameEn "FinResearch"
#define MyAppVersion "2.0.6"
#define MyAppPublisher "FinResearch"

[Setup]
AppId={{8F2C7A1E-3B5D-4A6E-9F01-FINRESEARCH001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=FinResearch-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\FinResearch.exe

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller onedir 产物整体打包（仅运行所需）
Source: "..\dist\FinResearch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\FinResearch.exe"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\FinResearch.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FinResearch.exe"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只删程序目录；用户数据（SQLite）在 %APPDATA%\FinResearch，卸载不删，留给用户决定
Type: filesandordirs; Name: "{app}"
