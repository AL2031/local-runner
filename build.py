"""Build a desktop app for the OS you run this on (output: dist/LocalRunner).
Put llama.cpp's llama-server (and its DLLs) in an 'engine' folder first.
Run: pip install pyinstaller && python build.py
Set DEBUG_CONSOLE=1 to keep a console window that shows errors."""
import os
import sys
import PyInstaller.__main__ as pi

sep = ";" if sys.platform == "win32" else ":"
args = ["app.py", "--name", "LocalRunner", "--noconfirm", "--add-data", f"index.html{sep}.",
        "--collect-all", "webview", "--collect-submodules", "uvicorn"]
if os.path.isdir("engine"):
    args += ["--add-data", f"engine{sep}engine"]
if not os.environ.get("DEBUG_CONSOLE"):
    args.append("--windowed")
if sys.platform == "win32":
    # Windows version details (publisher, description, version). Programs without them look anonymous to antivirus tools.
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    v = (1, 2, 0, 0)
    text = [("CompanyName", "Local Runner"), ("FileDescription", "Local Runner - run AI models on your own computer"),
            ("FileVersion", "1.2.0.0"), ("InternalName", "LocalRunner"), ("OriginalFilename", "LocalRunner.exe"),
            ("ProductName", "Local Runner"), ("ProductVersion", "1.2.0.0")]
    info = VSVersionInfo(ffi=FixedFileInfo(filevers=v, prodvers=v, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
                         kids=[StringFileInfo([StringTable("040904B0", [StringStruct(k, x) for k, x in text])]),
                               VarFileInfo([VarStruct("Translation", [1033, 1200])])])
    open("version_info.txt", "w", encoding="utf-8").write(str(info))
    args += ["--version-file", "version_info.txt"]
args.append("--noupx")  # compressed executables are a common trigger for antivirus heuristics
pi.run(args)
