Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appPath = fso.BuildPath(scriptDir, "app.py")

shell.CurrentDirectory = scriptDir
shell.Run "pyw """ & appPath & """", 0, False
