; ─────────────────────────────────────────────────────────────────
;  DISTRIGEST - Script Inno Setup
;  STiNAUG TECHNOLOGIE - Abidjan, Cote d'Ivoire
; ─────────────────────────────────────────────────────────────────

#define AppName        "DISTRIGEST"
#define AppVersion     "2.0.0"
#define AppPublisher   "STiNAUG TECHNOLOGIE"
#define AppURL         "https://stinaugtech.ci"
#define AppExeName     "DISTRIGEST.exe"
#define SourceDir      "dist\DISTRIGEST"

[Setup]
AppId                    ={{18F64027-EA56-45BC-BB5C-F83C4E3512C0}
AppName                  ={#AppName}
AppVersion               ={#AppVersion}
AppVerName               ={#AppName} v{#AppVersion}
AppPublisher             ={#AppPublisher}
AppPublisherURL          ={#AppURL}
AppSupportURL            ={#AppURL}
AppUpdatesURL            ={#AppURL}
AppCopyright             =Copyright 2025 {#AppPublisher}
DefaultDirName           ={autopf}\{#AppName}
DefaultGroupName         ={#AppName}
AllowNoIcons             =yes
OutputDir                =installer
OutputBaseFilename       =DISTRIGEST_Setup_v{#AppVersion}
SetupIconFile            =static\distrigest.ico
UninstallDisplayIcon     ={app}\static\distrigest.ico
Compression              =lzma2/ultra64
SolidCompression         =yes
WizardStyle              =modern
WizardImageFile          =static\splash.bmp
WizardSmallImageFile     =static\banner.bmp
DisableWelcomePage       =no
PrivilegesRequired       =lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications        =yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon";  Description: "Creer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"; Flags: checkedonce
Name: "startupicon";  Description: "Lancer au demarrage de Windows";   GroupDescription: "Options :";    Flags: unchecked

[Files]
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*";             DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; ── SumatraPDF : moteur d'impression PDF silencieuse ──
; Placez SumatraPDF.exe (version portable, renommee exactement ainsi)
; a la racine du projet, a cote de ce fichier .iss.
; skipifsourcedoesntexist : la compilation n'echoue pas s'il est absent.
Source: "SumatraPDF.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}";               FileName: "{app}\{#AppExeName}"; IconFilename: "{app}\static\distrigest.ico"
Name: "{group}\Desinstaller {#AppName}";  FileName: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";         FileName: "{app}\{#AppExeName}"; IconFilename: "{app}\static\distrigest.ico"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";         FileName: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Lancer {#AppName} maintenant"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM {#AppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

[Registry]
Root: HKCU; Subkey: "Software\STiNAUGTECHNOLOGIE\DISTRIGEST"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\STiNAUGTECHNOLOGIE\DISTRIGEST"; ValueType: string; ValueName: "Version";     ValueData: "{#AppVersion}"; Flags: uninsdeletekey

[Code]
function InitializeUninstall(): Boolean;
begin
  Result := MsgBox(
    'Desinstaller DISTRIGEST ?' +
    ' Vos donnees ne seront PAS supprimees.',
    mbConfirmation, MB_YESNO
  ) = IDYES;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  InfoFile: String;
  Content: String;
begin
  if CurStep = ssPostInstall then
  begin
    InfoFile := ExpandConstant('{app}\LISEZMOI.txt');
    Content := 'DISTRIGEST v2.0.0' + Chr(13) + Chr(10);
    Content := Content + 'STiNAUG TECHNOLOGIE - Abidjan, CI' + Chr(13) + Chr(10);
    Content := Content + '-----------------------------------' + Chr(13) + Chr(10);
    Content := Content + Chr(13) + Chr(10);
    Content := Content + 'DEMARRAGE' + Chr(13) + Chr(10);
    Content := Content + '  Lancez DISTRIGEST.exe' + Chr(13) + Chr(10);
    Content := Content + '  Acces : http://localhost:1439' + Chr(13) + Chr(10);
    Content := Content + Chr(13) + Chr(10);
    Content := Content + 'CONNEXION' + Chr(13) + Chr(10);
    Content := Content + '  Identifiant : admin' + Chr(13) + Chr(10);
    Content := Content + '  Mot de passe : Admin123' + Chr(13) + Chr(10);
    Content := Content + Chr(13) + Chr(10);
    Content := Content + 'SUPPORT' + Chr(13) + Chr(10);
    Content := Content + '  contact@stinaugtech.ci' + Chr(13) + Chr(10);
    Content := Content + '  Tel : +225 07 88 44 92 03' + Chr(13) + Chr(10);
    SaveStringToFile(InfoFile, Content, False);
  end;
end;
