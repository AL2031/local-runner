[Setup]
AppName=Local Runner
AppVersion=1.1.0
DefaultDirName={autopf}\Local Runner
DefaultGroupName=Local Runner
OutputDir=installer
OutputBaseFilename=LocalRunner-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\LocalRunner\*"; DestDir: "{app}"; Flags: recursesubdirs
Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\Local Runner"; Filename: "{app}\LocalRunner.exe"
Name: "{autodesktop}\Local Runner"; Filename: "{app}\LocalRunner.exe"

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Microsoft runtime..."; Flags: waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (TCP)"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (UDP)"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Local Runner sharing (TCP)"" dir=in action=allow protocol=TCP localport=50052 profile=private,domain remoteip=localsubnet"; StatusMsg: "Allowing sharing on your home network..."; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Local Runner sharing (UDP)"" dir=in action=allow protocol=UDP localport=50054 profile=private,domain remoteip=localsubnet"; Flags: runhidden waituntilterminated
Filename: "{app}\LocalRunner.exe"; Description: "Launch Local Runner"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (TCP)"""; Flags: runhidden; RunOnceId: "FwTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (UDP)"""; Flags: runhidden; RunOnceId: "FwUdp"
