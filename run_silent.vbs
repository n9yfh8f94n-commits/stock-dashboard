' ===========================================================================
'  run_silent.vbs ---- silent launcher for daily.bat (no black console window)
' ---------------------------------------------------------------------------
'  Usage:
'      double-click this file         -> review today and push to GitHub
'      run_silent.vbs 20261109        -> review a specific date (backfill)
'
'  Behaviour:
'      - cmd window is fully hidden
'      - shows a 4-second auto-closing toast when starting
'      - exits silently on success (the browser opens the dashboard = done)
'      - shows an error dialog on failure, pointing to logs\review.log
'
'  NOTE 1: this file is intentionally pure ASCII.
'  VBScript reads .vbs as ANSI. A UTF-8 file with Chinese comments gets
'  garbled under the GBK code page and can break parsing.
'  Keep every character below code 128 if you edit it.
'
'  NOTE 2: the project folder is derived from this script's own location,
'  NOT hardcoded. That keeps personal paths out of the published repo and
'  makes the file survive moving/renaming the project.
'  Keep this .vbs inside the project folder. A desktop SHORTCUT is fine;
'  copying the .vbs itself to the desktop is not.
' ===========================================================================

Option Explicit

Dim shell, fso
Dim baseDir, batPath, argStr, cmd
Dim ret

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' ---- project folder = folder containing this script ----
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = baseDir & "\daily.bat"

' ---- guard: does the target exist ----
If Not fso.FileExists(batPath) Then
    MsgBox "daily.bat not found next to this script:" & vbCrLf & vbCrLf & _
           batPath & vbCrLf & vbCrLf & _
           "Keep run_silent.vbs inside the a_share_review folder.", 16, "A-Share Review"
    WScript.Quit 1
End If

' ---- optional date argument (YYYYMMDD); no argument means today ----
argStr = ""
If WScript.Arguments.Count > 0 Then
    argStr = " " & Trim(WScript.Arguments(0))
End If

' ---- working directory must be the project folder ----
shell.CurrentDirectory = baseDir

' ---- start toast: auto-closes after 4 seconds, never blocks ----
shell.Popup "Running daily review, about 1 minute." & vbCrLf & _
            "The browser will open when it finishes." & vbCrLf & _
            "Log: logs\review.log", 4, "A-Share Review", 64

' ---- run: 0 = hidden window, True = wait for exit ----
' Waiting is required to read the exit code. Without it a failure would be
' completely silent (the window is gone and nothing tells you why).
cmd = """" & batPath & """" & argStr
ret = shell.Run(cmd, 0, True)

' ---- result ----
If ret = 0 Then
    ' success: stay silent, the browser already shows the dashboard
    WScript.Quit 0
Else
    MsgBox "Run failed (exit code " & ret & ")" & vbCrLf & vbCrLf & _
           "Common causes:" & vbCrLf & _
           "  1. No network (GitHub push failed)" & vbCrLf & _
           "  2. Missing Python packages (akshare / pyecharts)" & vbCrLf & _
           "  3. Eastmoney API temporarily unavailable" & vbCrLf & vbCrLf & _
           "Local report may still exist. Check the log:" & vbCrLf & _
           baseDir & "\logs\review.log", 16, "A-Share Review"
    WScript.Quit ret
End If
