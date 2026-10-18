; 灵语（Lingyu）Windows 安装脚本 —— Inno Setup 6。
;
; 编译（仓库根目录，或由 packaging/build.py 自动调用）：
;
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
;
; 产物：dist\lingyu-setup-x64.exe
;
; 设计取舍：
; * **按用户安装**（PrivilegesRequired=lowest + {localappdata}）：不弹 UAC，
;   且安装目录可写——程序会在 exe 同级写运行日志，装到 Program Files 反而会失败。
; * 安装程序自己写文件，**不会**带上「Internet 区域」标记（该标记只由浏览器
;   下载/资源管理器解压产生），从根上避免 pythonnet 被 .NET 拒绝加载而黑屏。

#ifndef AppVersion
  #define AppVersion "0.8.14"
#endif

#define AppName "灵语"
#define AppExe "lingyu.exe"
#define SourceDir "..\dist\lingyu"

[Setup]
; 应用唯一标识（升级/卸载靠它匹配，不要随意改）。
AppId={{232EF569-4E6C-48FD-9DEE-18826233CED8}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Lingyu
DefaultDirName={localappdata}\Programs\lingyu
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=lingyu-setup-x64
SetupIconFile=lingyu.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 随附的许可证与说明，便于查阅。
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\TECHNICAL.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--gui"; Comment: "启动灵语"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--gui"; Comment: "启动灵语"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--gui"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
