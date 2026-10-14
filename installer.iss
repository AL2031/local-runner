[Setup]
AppName=Local Runner
AppVersion=1.2.0
AppPublisher=Local Runner
VersionInfoVersion=1.2.0.0
VersionInfoCompany=Local Runner
VersionInfoDescription=Local Runner Setup
VersionInfoProductName=Local Runner
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
Name: "{group}\Uninstall Local Runner"; Filename: "{uninstallexe}"

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (TCP)"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (UDP)"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Local Runner sharing (TCP)"" dir=in action=allow protocol=TCP localport=50052 profile=private,domain remoteip=localsubnet"; StatusMsg: "Allowing sharing on your home network..."; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Local Runner sharing (UDP)"" dir=in action=allow protocol=UDP localport=50054 profile=private,domain remoteip=localsubnet"; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{app}\LocalRunner.exe"; Description: "Launch Local Runner"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Close the app and its engines first, otherwise Windows keeps their files locked
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM LocalRunner.exe /IM llama-server.exe /IM ggml-rpc-server.exe"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (TCP)"""; Flags: runhidden; RunOnceId: "FwTcp"; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Local Runner sharing (UDP)"""; Flags: runhidden; RunOnceId: "FwUdp"; Check: IsAdminInstallMode

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := ExpandConstant('{%USERPROFILE}\LocalRunner');
    if DirExists(Dir) and (not UninstallSilent) then
      if MsgBox('Also delete your downloaded models and settings?' + #13#10 + #13#10 + Dir + #13#10 + #13#10 +
                'Choose No to keep them. They will be reused if you install Local Runner again.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Dir, True, True, True);
  end;
end;
