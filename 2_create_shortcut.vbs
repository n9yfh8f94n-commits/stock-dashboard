' ===========================================================================
'  Script 2 of 3 : create a desktop shortcut with a custom icon
' ---------------------------------------------------------------------------
'  What it does:
'      Creates "<Desktop>\<Daily Review>.lnk" pointing at wscript.exe
'      with run_silent.vbs as the argument, working folder = project folder,
'      icon = app.ico.
'
'  How the Chinese shortcut name works without breaking ASCII:
'      This file must stay pure ASCII (VBScript reads .vbs as ANSI, and a
'      UTF-8 Chinese literal gets garbled under the GBK code page).
'      So the name is built at runtime with ChrW():
'          ChrW(27599) = the 1st char of "daily"
'          ChrW(26085) = the 2nd char
'          ChrW(22797) = the 3rd char
'          ChrW(30424) = the 4th char
'      Together they spell  "Mei Ri Fu Pan"  (daily review).
'      Change those four numbers if you want a different Chinese name.
'
'  Paths are derived from this script's own location, so nothing is hardcoded.
'  Keep this file in the a_share_review folder.
' ===========================================================================

Option Explicit

Dim shell, fso
Dim baseDir, vbsPath, icoPath, desktopPath, lnkPath
Dim shortcutName, wscriptExe
Dim lnk

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' ---- everything is relative to this script's folder ----
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
vbsPath = baseDir & "\run_silent.vbs"
icoPath = baseDir & "\app.ico"

' ---- build the Chinese name at runtime (4 chars: daily review) ----
shortcutName = ChrW(27599) & ChrW(26085) & ChrW(22797) & ChrW(30424)

' ---- locate wscript.exe explicitly instead of relying on PATH ----
wscriptExe = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\wscript.exe"

' ---- sanity checks, fail loudly rather than create a broken link ----
If Not fso.FileExists(vbsPath) Then
    MsgBox "run_silent.vbs not found next to this script:" & vbCrLf & vbCrLf & _
           vbsPath & vbCrLf & vbCrLf & _
           "Keep all three scripts inside the a_share_review folder.", 16, "A-Share Review"
    WScript.Quit 1
End If

If Not fso.FileExists(wscriptExe) Then
    MsgBox "wscript.exe not found at:" & vbCrLf & vbCrLf & wscriptExe, 16, "A-Share Review"
    WScript.Quit 1
End If

' ---- desktop folder (works even if it was redirected to OneDrive) ----
desktopPath = shell.SpecialFolders("Desktop")
If desktopPath = "" Then
    MsgBox "Could not locate the Desktop folder.", 16, "A-Share Review"
    WScript.Quit 1
End If

lnkPath = desktopPath & "\" & shortcutName & ".lnk"

' ---- create / overwrite the shortcut ----
Set lnk = shell.CreateShortcut(lnkPath)
lnk.TargetPath = wscriptExe
lnk.Arguments = """" & vbsPath & """"
lnk.WorkingDirectory = baseDir
lnk.Description = "A-Share daily review - run and publish"
lnk.WindowStyle = 1

If fso.FileExists(icoPath) Then
    ' ",0" = use the first icon inside app.ico
    lnk.IconLocation = icoPath & ",0"
End If

lnk.Save

' ---- verify it really landed on disk ----
If fso.FileExists(lnkPath) Then
    MsgBox "Desktop shortcut created." & vbCrLf & vbCrLf & _
           "Name : " & shortcutName & ".lnk" & vbCrLf & _
           "Where: " & desktopPath & vbCrLf & vbCrLf & _
           "Double-click it to run the daily review silently." & vbCrLf & _
           "The browser opens when it finishes.", 64, "A-Share Review"
    WScript.Quit 0
Else
    MsgBox "Shortcut was not created. Check folder permissions for:" & vbCrLf & _
           desktopPath, 16, "A-Share Review"
    WScript.Quit 1
End If
