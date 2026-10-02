"""雙擊啟動入口：選擇可用的 Python，開啟較窄的彩色終端視窗。"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys


def find_python(project: Path) -> Path:
    """只使用已安裝 Textual 的環境，不自動安裝或修改套件。"""
    candidates = [project / ".venv" / "Scripts" / "python.exe", Path(sys.executable)]
    candidates.extend(Path(f"C:/Python/Python{version}/python.exe") for version in ("314", "313", "312"))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "-I", "-c",
                 "import sys,json,importlib.metadata; print(json.dumps({'python':list(sys.version_info[:3]),'textual':importlib.metadata.version('textual')}))"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            continue
        if result.returncode == 0:
            try:
                info = json.loads(result.stdout)
                if tuple(info["python"]) >= (3, 12) and info["textual"] == "8.2.8":
                    return candidate.resolve()
            except (ValueError, KeyError, TypeError):
                continue
    raise RuntimeError("找不到 Python 3.12+ 與 Textual 8.2.8；請用已修補的 Python 建立 .venv 並安裝 requirements.txt。")


def trusted_windows_tools() -> tuple[Path, Path | None]:
    """以 Windows API 取得受信任的位置，不搜尋工作目錄或 PATH。"""
    if not hasattr(ctypes, "windll"):
        raise RuntimeError("視窗啟動器僅支援 Windows。")
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
        raise OSError("無法取得 Windows 系統目錄。")
    powershell = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise RuntimeError("找不到系統 Windows PowerShell。")
    # CSIDL_LOCAL_APPDATA 由系統取得，避免同名環境變數被覆寫。
    terminal = None
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x1C, None, 0, buffer) == 0:
        expected = Path(buffer.value) / "Microsoft" / "WindowsApps" / "wt.exe"
        if expected.is_file():
            terminal = expected
    return powershell, terminal


def main() -> int:
    parser = argparse.ArgumentParser(description="開啟 File Finder 彩色終端視窗。")
    parser.add_argument("--dry-run", action="store_true", help="只檢查啟動環境，不開啟視窗")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    try:
        python = find_python(project)
        powershell, terminal = trusted_windows_tools()
        # 編碼傳遞整段指令，避免終端將分號拆成多個分頁；路徑可含空白與中文。
        def quote(value: Path) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        script = (
            "$env:NO_COLOR = $null; $env:TERM = 'xterm-256color'; "
            "$env:COLORTERM = 'truecolor'; "
            f"Set-Location -LiteralPath {quote(project)}; "
            f"& {quote(python)} -X utf8 -E -s {quote(project / 'main.py')}"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command = [str(powershell), "-NoProfile", "-NoExit", "-EncodedCommand", encoded]
        if terminal:
            command = [str(terminal), "-w", "new", "--size", "80,30", "new-tab", "--title", "FileFinder", "-d", str(project), *command]
        if args.dry_run:
            print(f"Python：{python}\n程式：{project / 'main.py'}")
            print("視窗：80 欄 × 30 列" if terminal else "視窗：PowerShell（未找到 Windows Terminal）")
            print("彩色主題已啟用；Ctrl+Q 後保留專案命令列。")
            return 0
        environment = dict(os.environ)
        for key in ("NO_COLOR", "PYTHONPATH", "PYTHONHOME"):
            environment.pop(key, None)
        subprocess.Popen(command, cwd=project, env=environment, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return 0
    except (OSError, RuntimeError) as error:
        print(f"無法啟動：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
