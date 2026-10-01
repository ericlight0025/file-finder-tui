"""啟動器使用隔離探測與固定系統位置，不執行外來模組或測試 EXE。"""

import base64
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import launch


@pytest.mark.skipif(sys.version_info < (3, 12), reason="正式啟動器要求 Python 3.12+")
def test_probe_ignores_cwd_and_pythonpath_modules(tmp_path, monkeypatch):
    (tmp_path / "textual.py").write_text("raise RuntimeError('不應載入工作目錄模組')", encoding="utf-8")
    (tmp_path / "json.py").write_text("raise RuntimeError('不應載入工作目錄模組')", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert launch.find_python(tmp_path) == Path(sys.executable).resolve()


def test_tool_lookup_ignores_cwd_and_path(tmp_path, monkeypatch):
    system = tmp_path / "system"
    appdata = tmp_path / "appdata"
    powershell = system / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    terminal = appdata / "Microsoft" / "WindowsApps" / "wt.exe"
    for file in (powershell, terminal, tmp_path / "powershell.exe", tmp_path / "wt.exe"):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    def system_directory(buffer, _):
        buffer.value = str(system)
        return len(buffer.value)

    def local_appdata(_, folder, __, ___, buffer):
        assert folder == 0x1C
        buffer.value = str(appdata)
        return 0

    monkeypatch.setattr(launch.ctypes, "windll", SimpleNamespace(
        kernel32=SimpleNamespace(GetSystemDirectoryW=system_directory),
        shell32=SimpleNamespace(SHGetFolderPathW=local_appdata),
    ), raising=False)
    assert launch.trusted_windows_tools() == (powershell, terminal)


def test_dry_run_does_not_start_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "find_python", lambda _: tmp_path / "python.exe")
    monkeypatch.setattr(launch, "trusted_windows_tools", lambda: (tmp_path / "powershell.exe", None))
    monkeypatch.setattr(sys, "argv", ["launch.py", "--dry-run"])
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("dry-run 不可開啟視窗"))
    assert launch.main() == 0


def test_launch_cleans_environment_and_disables_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "find_python", lambda _: tmp_path / "python.exe")
    monkeypatch.setattr(launch, "trusted_windows_tools", lambda: (tmp_path / "powershell.exe", tmp_path / "wt.exe"))
    monkeypatch.setattr(sys, "argv", ["launch.py"])
    for key in ("PYTHONPATH", "PYTHONHOME", "NO_COLOR"):
        monkeypatch.setenv(key, "untrusted")
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    assert launch.main() == 0
    args, kwargs = calls[0]
    assert args[0] == str(tmp_path / "wt.exe") and "-NoProfile" in args
    assert not {"PYTHONPATH", "PYTHONHOME", "NO_COLOR"} & kwargs["env"].keys()
    script = base64.b64decode(args[-1]).decode("utf-16-le")
    assert "-X utf8 -E -s" in script


def test_outdated_python_is_not_selected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='{"python":[3,11,0],"textual":"8.2.8"}'))
    with pytest.raises(RuntimeError, match="3.12"):
        launch.find_python(tmp_path)
