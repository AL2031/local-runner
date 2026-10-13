[Setup]
AppName=Local Runner
AppVersion=1.2.0
DefaultDirName={autopf}\Local Runner
DefaultGroupName=Local Runner
; Per-user install (no admin password). The user can still choose 'all users' in the dialog if they are an admin.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\LocalRunner.exe
OutputDir=installer
OutputBaseFilename=LocalRunner-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\LocalRunner\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{group}\Local Runner"; Filename: "{app}\LocalRunner.exe"
Name: "{autodesktop}\Local Runner"; Filename: "{app}\LocalRunner.exe"

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (TCP)"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (UDP)"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Local Runner sharing (TCP)"" dir=in action=allow protocol=TCP localport=50052 profile=private,domain remoteip=localsubnet"; StatusMsg: "Allowing sharing on your home network..."; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Local Runner sharing (UDP)"" dir=in action=allow protocol=UDP localport=50054 profile=private,domain remoteip=localsubnet"; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{app}\LocalRunner.exe"; Description: "Launch Local Runner"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (TCP)"""; Flags: runhidden; RunOnceId: "FwTcp"; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (UDP)"""; Flags: runhidden; RunOnceId: "FwUdp"; Check: IsAdminInstallMode
