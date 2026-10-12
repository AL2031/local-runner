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
pi.run(args)
