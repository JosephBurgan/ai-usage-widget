Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "pythonw.exe """ & scriptDir & "\claude_widget.py"""
For Each arg In WScript.Arguments
  cmd = cmd & " " & arg
Next
sh.Run cmd, 0, False
