import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request


try:
    import winreg
except ImportError:
    winreg = None


app = Flask(__name__)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "pymanager.config.json"
APP_VENV_DIR = APP_DIR / ".venv"
DEFAULT_APP_PYTHON = os.environ.get("PYMANAGER_APP_PYTHON", "3.14.6")
IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if IS_WINDOWS else 0

_jobs = {}
_jobs_lock = threading.Lock()


# Platform/process helpers ----------------------------------------------------

def _popen_kwargs(extra=None):
    kwargs = dict(extra or {})
    if IS_WINDOWS and NO_WINDOW:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | NO_WINDOW
    return kwargs


def _expand(path):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def _run(exe, args, timeout=8, cwd=None):
    try:
        result = subprocess.run(
            [exe, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            **_popen_kwargs(),
        )
        output = (result.stdout or "").strip() or (result.stderr or "").strip()
        return output, result.returncode
    except FileNotFoundError:
        return None, 127
    except subprocess.TimeoutExpired:
        return None, 124
    except Exception:
        return None, -1


def _spawn(args, cwd=None):
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **_popen_kwargs(),
    )


def _norm(path):
    return os.path.normcase(_expand(path))


def _posixish(path):
    return _norm(path).replace("\\", "/").lower()


def _is_relative_to(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_id(exe):
    return hashlib.sha1(_norm(exe).encode("utf-8", "ignore")).hexdigest()[:16]


def _platform_label():
    if IS_WINDOWS:
        return "windows"
    if IS_LINUX:
        return "linux"
    return platform.system().lower() or "unknown"


def _python_exe_name():
    return "python.exe" if IS_WINDOWS else "bin/python"


def _app_venv_python():
    if IS_WINDOWS:
        return APP_VENV_DIR / "Scripts" / "python.exe"
    return APP_VENV_DIR / "bin" / "python"


def _read_pyvenv_cfg():
    cfg = APP_VENV_DIR / "pyvenv.cfg"
    values = {}
    if not cfg.is_file():
        return values

    try:
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    except OSError:
        return {}
    return values


def _is_python_executable_name(name):
    low = name.lower()
    if IS_WINDOWS:
        return low in {"python.exe", "python3.exe"}
    return re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", low) is not None


def _run_json_python(exe):
    code = (
        "import json, platform, struct, sys;"
        "print(json.dumps({"
        "'version': platform.python_version(),"
        "'implementation': platform.python_implementation(),"
        "'arch': struct.calcsize('P') * 8,"
        "'prefix': sys.prefix,"
        "'base_prefix': getattr(sys, 'base_prefix', sys.prefix),"
        "'executable': sys.executable"
        "}))"
    )
    out, rc = _run(exe, ["-c", code], timeout=8)
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None


def _pip(exe):
    out, rc = _run(exe, ["-m", "pip", "--version"], timeout=8)
    if rc != 0 or not out:
        return None
    match = re.search(r"pip\s+(\S+)", out)
    return match.group(1) if match else None


def _venv_root(exe, info=None):
    if info and info.get("prefix") and info.get("base_prefix"):
        if _norm(info["prefix"]) != _norm(info["base_prefix"]):
            return info["prefix"]

    exe_path = Path(exe)
    candidates = [exe_path.parent, exe_path.parent.parent, exe_path.parent.parent.parent]
    for candidate in candidates:
        if candidate and (candidate / "pyvenv.cfg").is_file():
            return str(candidate)
    return None


def _manager_root_from_exe(exe, path, kind):
    install_path = _expand(path or os.path.dirname(exe))
    base = os.path.basename(install_path).lower()
    if not IS_WINDOWS and base == "bin" and kind in {"uv", "pyenv", "asdf", "portable", "custom"}:
        return str(Path(install_path).parent)
    if IS_WINDOWS and base == "scripts" and kind in {"portable", "custom"}:
        return str(Path(install_path).parent)
    return install_path


def _path_source_kind(path, source, kind):
    low = _posixish(path)
    source_low = source.lower()

    if "/uv/python/" in low or low.endswith("/uv/python") or source_low.startswith("uv"):
        return "uv", "uv"
    if "/appdata/local/python/pythoncore-" in low or source_low.startswith("python install manager"):
        return "Python Install Manager", "pymanager"
    if "/appdata/local/programs/python/python" in low or "python.org" in source_low:
        return "Python.org", "standard"
    if "/.pyenv/versions/" in low or "/pyenv-win/versions/" in low or "pyenv" in source_low:
        return "pyenv", "pyenv"
    if "/.asdf/installs/python/" in low or "asdf" in source_low:
        return "asdf", "asdf"
    if "/scoop/apps/" in low or "scoop" in source_low:
        return "Scoop", "scoop"
    if "conda" in source_low or "anaconda" in source_low or "miniforge" in source_low:
        return source, kind
    if not IS_WINDOWS and (low.startswith("/usr/") or low.startswith("/bin/")):
        return "System", "system"
    return source, kind


def _remove_mode(kind):
    modes = {
        "standard": "uninstall",
        "pymanager": "pymanager uninstall",
        "system": "system package manager",
        "pyenv": "pyenv uninstall",
        "uv": "uv python uninstall",
        "asdf": "asdf uninstall",
        "conda": "not managed",
        "conda-env": "conda env remove",
        "scoop": "scoop uninstall",
        "portable": "delete folder",
        "custom": "delete folder",
        "virtual-env": "not managed",
        "path": "not managed",
        "store": "Windows Settings",
    }
    return modes.get(kind, "not managed")


def _can_remove(kind, is_current):
    if is_current:
        return False
    if kind == "standard":
        return IS_WINDOWS
    return kind in {"pymanager", "pyenv", "uv", "asdf", "conda-env", "scoop", "portable", "custom"}


def _build(exe, path=None, source="Unknown", kind="standard", seen=None):
    exe = _expand(exe)
    if not os.path.isfile(exe):
        return None

    info = _run_json_python(exe)
    if not info or not info.get("version"):
        return None

    reported_exe = _expand(info.get("executable") or exe)
    if os.path.isfile(reported_exe):
        exe = reported_exe

    if seen is not None:
        key = _norm(os.path.realpath(exe))
        if key in seen:
            return None
        seen.add(key)

    install_path = _manager_root_from_exe(exe, path, kind)
    if kind == "path" and not _is_relative_to(exe, install_path):
        install_path = os.path.dirname(exe)
    venv = _venv_root(exe, info)
    if venv and kind in {"path", "custom"}:
        install_path = _expand(venv)
        source = "Virtual environment"
        kind = "virtual-env"

    source, kind = _path_source_kind(install_path, source, kind)
    is_current = _norm(exe) == _norm(sys.executable)

    return {
        "id": _safe_id(exe),
        "version": info["version"],
        "implementation": info.get("implementation", "Python"),
        "path": install_path,
        "executable": exe,
        "source": source,
        "architecture": f"{info.get('arch', 'unknown')}-bit",
        "pip_version": _pip(exe),
        "type": kind,
        "is_current": is_current,
        "is_virtual_env": kind == "virtual-env",
        "can_remove": _can_remove(kind, is_current),
        "remove_mode": _remove_mode(kind),
    }


def _version_tuple(version):
    parts = []
    for item in str(version).split("."):
        match = re.match(r"\d+", item)
        parts.append(int(match.group(0)) if match else 0)
    return tuple(parts + [0] * (4 - len(parts)))


def _load_config():
    if not CONFIG_PATH.is_file():
        return {"custom_paths": []}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"custom_paths": []}
    paths = data.get("custom_paths", [])
    if not isinstance(paths, list):
        paths = []
    return {"custom_paths": [str(p) for p in paths if str(p).strip()]}


def _save_config(config):
    clean = []
    seen = set()
    for raw in config.get("custom_paths", []):
        path = _expand(raw)
        key = _norm(path)
        if key not in seen:
            seen.add(key)
            clean.append(path)
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump({"custom_paths": clean}, handle, indent=2)


# Detectors -------------------------------------------------------------------

def _registry(seen):
    if not IS_WINDOWS or winreg is None:
        return []

    results = []
    locations = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Python\PythonCore", "Python.org (system)"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Python\PythonCore", "Python.org (32-bit)"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Python\PythonCore", "Python.org (user)"),
    ]
    for hive, key_path, label in locations:
        try:
            with winreg.OpenKey(hive, key_path) as root:
                index = 0
                while True:
                    try:
                        version_key = winreg.EnumKey(root, index)
                        index += 1
                    except OSError:
                        break

                    try:
                        with winreg.OpenKey(root, rf"{version_key}\InstallPath") as install_key:
                            raw, _ = winreg.QueryValueEx(install_key, "")
                    except OSError:
                        continue

                    install_path = str(raw).rstrip("\\/")
                    inst = _build(os.path.join(install_path, "python.exe"), install_path, label, "standard", seen)
                    if inst:
                        results.append(inst)
        except (OSError, PermissionError):
            continue
    return results


def _python_launcher(seen):
    if not IS_WINDOWS:
        return []

    results = []
    out, rc = _run("py", ["-0p"], timeout=8)
    if rc != 0 or not out:
        return results

    pattern = re.compile(r"^\s*-V:(?P<tag>\S+)(?:\s+\*)?\s+(?P<exe>[A-Za-z]:\\.*?\.exe)\s*$")
    for line in out.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        tag = match.group("tag")
        exe = match.group("exe").strip()
        low = _posixish(exe)
        if "/uv/python/" in low or tag.lower().startswith("astral/"):
            source, kind = "uv", "uv"
        elif "/windowsapps/" in low:
            source, kind = "Microsoft Store", "store"
        else:
            source, kind = "Python Launcher", "standard"
        inst = _build(exe, os.path.dirname(exe), source, kind, seen)
        if inst:
            results.append(inst)
    return results


def _uv(seen):
    results = []
    roots = []
    if IS_WINDOWS:
        roots.extend([
            os.path.expandvars(r"%APPDATA%\uv\python"),
            os.path.expandvars(r"%LOCALAPPDATA%\uv\python"),
        ])
    else:
        data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        roots.append(os.path.join(data_home, "uv", "python"))

    patterns = ["*", "*/*"]
    for root in roots:
        for pattern in patterns:
            for folder in glob.glob(os.path.join(root, pattern)):
                folder = os.path.realpath(folder)
                exe_candidates = [
                    os.path.join(folder, "python.exe"),
                    os.path.join(folder, "bin", "python"),
                    os.path.join(folder, "bin", "python3"),
                ]
                for exe in exe_candidates:
                    inst = _build(exe, folder, "uv", "uv", seen)
                    if inst:
                        results.append(inst)
    return results


def _pyenv(seen):
    results = []
    root = os.environ.get("PYENV_ROOT")
    if not root:
        root = os.path.expandvars(r"%USERPROFILE%\.pyenv\pyenv-win") if IS_WINDOWS else os.path.expanduser("~/.pyenv")

    versions_dir = os.path.join(root, "versions")
    if not os.path.isdir(versions_dir):
        return results

    for entry in os.scandir(versions_dir):
        if not entry.is_dir():
            continue
        exe = os.path.join(entry.path, "python.exe" if IS_WINDOWS else "bin/python")
        inst = _build(exe, entry.path, "pyenv", "pyenv", seen)
        if inst:
            results.append(inst)
    return results


def _asdf(seen):
    if IS_WINDOWS:
        return []

    results = []
    root = os.environ.get("ASDF_DATA_DIR") or os.path.expanduser("~/.asdf")
    versions_dir = os.path.join(root, "installs", "python")
    if not os.path.isdir(versions_dir):
        return results

    for entry in os.scandir(versions_dir):
        if not entry.is_dir():
            continue
        for exe_name in ("python", "python3"):
            inst = _build(os.path.join(entry.path, "bin", exe_name), entry.path, "asdf", "asdf", seen)
            if inst:
                results.append(inst)
                break
    return results


def _conda_roots():
    if IS_WINDOWS:
        return [
            os.path.expandvars(r"%USERPROFILE%\anaconda3"),
            os.path.expandvars(r"%USERPROFILE%\miniconda3"),
            os.path.expandvars(r"%USERPROFILE%\miniforge3"),
            os.path.expandvars(r"%LOCALAPPDATA%\anaconda3"),
            os.path.expandvars(r"%LOCALAPPDATA%\miniconda3"),
            os.path.expandvars(r"%LOCALAPPDATA%\miniforge3"),
            r"C:\ProgramData\Anaconda3",
            r"C:\ProgramData\Miniconda3",
            r"C:\ProgramData\Miniforge3",
            r"C:\Anaconda3",
            r"C:\Miniconda3",
            r"C:\Miniforge3",
        ]
    return [
        os.path.expanduser("~/anaconda3"),
        os.path.expanduser("~/miniconda3"),
        os.path.expanduser("~/miniforge3"),
        "/opt/conda",
        "/opt/anaconda3",
        "/opt/miniconda3",
        "/opt/miniforge3",
        "/usr/local/anaconda3",
        "/usr/local/miniconda3",
        "/usr/local/miniforge3",
    ]


def _conda_python(root):
    return os.path.join(root, "python.exe" if IS_WINDOWS else "bin/python")


def _conda_known_roots(seen):
    results = []
    for root in _conda_roots():
        root = _expand(root)
        if not os.path.isdir(root):
            continue
        name = os.path.basename(root).lower()
        label = "Anaconda" if "anaconda" in name else "Miniforge" if "miniforge" in name else "Miniconda"
        base = _build(_conda_python(root), root, f"{label} (base)", "conda", seen)
        if base:
            results.append(base)

        envs_dir = os.path.join(root, "envs")
        if not os.path.isdir(envs_dir):
            continue
        for entry in os.scandir(envs_dir):
            if not entry.is_dir():
                continue
            inst = _build(_conda_python(entry.path), entry.path, f"Conda env ({entry.name})", "conda-env", seen)
            if inst:
                results.append(inst)
    return results


def _conda_command(seen):
    results = []
    out, rc = _run("conda", ["info", "--json"], timeout=12)
    if rc != 0 or not out:
        return results

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return results

    root_prefix = data.get("root_prefix") or data.get("base_prefix")
    for env_path in data.get("envs", []):
        if not env_path:
            continue
        kind = "conda" if root_prefix and _norm(env_path) == _norm(root_prefix) else "conda-env"
        label = "Conda (base)" if kind == "conda" else f"Conda env ({os.path.basename(env_path)})"
        inst = _build(_conda_python(env_path), env_path, label, kind, seen)
        if inst:
            results.append(inst)
    return results


def _scoop(seen):
    if not IS_WINDOWS:
        return []

    results = []
    root = os.path.expandvars(r"%USERPROFILE%\scoop\apps")
    for exe in glob.glob(os.path.join(root, "python*", "current", "python.exe")):
        app_name = Path(exe).parents[1].name
        inst = _build(exe, os.path.dirname(exe), f"Scoop ({app_name})", "scoop", seen)
        if inst:
            results.append(inst)
    return results


def _pymanager(seen):
    if not IS_WINDOWS:
        return []

    results = []
    root = os.path.expandvars(r"%LOCALAPPDATA%\Python")
    for folder in glob.glob(os.path.join(root, "pythoncore-*")):
        exe = os.path.join(folder, "python.exe")
        inst = _build(exe, folder, "Python Install Manager", "pymanager", seen)
        if inst:
            results.append(inst)
    return results


def _python_exes_in_dir(directory):
    if not os.path.isdir(directory):
        return []
    if IS_WINDOWS:
        return [os.path.join(directory, name) for name in ("python.exe", "python3.exe")]
    try:
        return [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if _is_python_executable_name(name) and os.access(os.path.join(directory, name), os.X_OK)
        ]
    except OSError:
        return []


def _in_path(seen):
    results = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        if IS_WINDOWS:
            path_dir = _posixish(directory).rstrip("/")
            if "/appdata/local/python/bin" in path_dir:
                continue
            if "/windowsapps" in path_dir:
                continue
            if path_dir.endswith("/.local/bin"):
                continue
        for exe in _python_exes_in_dir(directory):
            try:
                size = os.path.getsize(exe)
            except OSError:
                continue
            if IS_WINDOWS and "WindowsApps" in directory and size < 512:
                continue

            source = "PATH"
            kind = "path"
            low = _posixish(exe)
            if not IS_WINDOWS and (low.startswith("/usr/") or low.startswith("/bin/")):
                source, kind = "System", "system"

            inst = _build(exe, directory, source, kind, seen)
            if inst:
                results.append(inst)
    return results


def _common_paths(seen):
    results = []
    if IS_WINDOWS:
        direct_patterns = [
            r"C:\Python*",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python*"),
            os.path.expandvars(r"%PROGRAMFILES%\Python*"),
            os.path.expandvars(r"%PROGRAMFILES(x86)%\Python*"),
            os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Python\Python*"),
            os.path.expandvars(r"%PROGRAMFILES%\WinPython*"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\WinPython*"),
            r"C:\Tools\Python*",
            r"C:\Tools\WinPython*",
        ]
        for pattern in direct_patterns:
            for folder in glob.glob(pattern):
                inst = _build(os.path.join(folder, "python.exe"), folder, "Common folder", "standard", seen)
                if inst:
                    results.append(inst)

        for pattern in [
            os.path.expandvars(r"%PROGRAMFILES%\WinPython*\python*\python.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\WinPython*\python*\python.exe"),
            r"C:\Tools\WinPython*\python*\python.exe",
        ]:
            for exe in glob.glob(pattern):
                inst = _build(exe, os.path.dirname(exe), "WinPython", "portable", seen)
                if inst:
                    results.append(inst)
        return results

    system_exes = []
    for directory in ("/usr/bin", "/usr/local/bin", "/bin"):
        system_exes.extend(_python_exes_in_dir(directory))
    for exe in system_exes:
        inst = _build(exe, os.path.dirname(exe), "System", "system", seen)
        if inst:
            results.append(inst)

    for pattern in [
        "/opt/python*/bin/python*",
        "/opt/Python*/bin/python*",
        "/usr/local/python*/bin/python*",
        os.path.expanduser("~/.local/bin/python*"),
        os.path.expanduser("~/.local/share/pipx/venvs/*/bin/python*"),
    ]:
        for exe in glob.glob(pattern):
            if _is_python_executable_name(os.path.basename(exe)):
                source = "System" if _posixish(exe).startswith("/usr/local/") else "Common folder"
                kind = "system" if source == "System" else "portable"
                root = str(Path(exe).parent.parent) if Path(exe).parent.name == "bin" else os.path.dirname(exe)
                inst = _build(exe, root, source, kind, seen)
                if inst:
                    results.append(inst)
    return results


def _find_python_exes(root, max_depth=3):
    root_path = Path(os.path.expandvars(str(root))).expanduser()
    if root_path.is_file():
        if _is_python_executable_name(root_path.name):
            return [str(root_path)]
        return []
    if not root_path.is_dir():
        return []

    skip_dirs = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        "site-packages",
        "dist-info",
        "build",
        "dist",
    }
    found = []
    for current, dirs, files in os.walk(root_path):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root_path).parts)
        except ValueError:
            depth = 0
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]

        for file_name in files:
            full = current_path / file_name
            if _is_python_executable_name(file_name) and (IS_WINDOWS or os.access(full, os.X_OK)):
                found.append(str(full))
    return found


def _custom_paths(seen):
    results = []
    for custom_path in _load_config()["custom_paths"]:
        for exe in _find_python_exes(custom_path, max_depth=4):
            root = str(Path(exe).parent.parent) if not IS_WINDOWS and Path(exe).parent.name == "bin" else os.path.dirname(exe)
            inst = _build(exe, root, "Custom path", "custom", seen)
            if inst:
                results.append(inst)
    return results


def _app_virtual_env(seen):
    exe = _app_venv_python()
    if not exe.is_file():
        return []

    inst = _build(str(exe), str(APP_VENV_DIR), "PyManager app venv", "virtual-env", seen)
    return [inst] if inst else []


def _scan():
    seen = set()
    results = []
    detectors = [
        _app_virtual_env,
        _registry,
        # Do not call "py -0p" during passive scans. On systems with Python
        # Install Manager, the launcher can auto-provision a runtime just from
        # being queried, which means scanning changes the user's machine.
        _uv,
        _pyenv,
        _asdf,
        _conda_known_roots,
        _conda_command,
        _scoop,
        _pymanager,
        _in_path,
        _common_paths,
        _custom_paths,
    ]
    for detector in detectors:
        try:
            results.extend(detector(seen))
        except Exception as exc:
            print(f"[scan] {detector.__name__}: {exc}", file=sys.stderr)

    results.sort(key=lambda item: (_version_tuple(item["version"]), item["source"], item["path"]), reverse=True)
    return results


# Job helpers -----------------------------------------------------------------

def _new_job(tag):
    jid = f"{tag}_{int(time.time() * 1000)}"
    with _jobs_lock:
        _jobs[jid] = {"status": "running", "lines": [], "success": None, "error": None}
    return jid


def _line(jid, text):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]["lines"].append(str(text))


def _done(jid, ok, err=None):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(status="done", success=bool(ok), error=err)


def _stream_proc(proc, jid):
    if proc.stdout:
        for line in proc.stdout:
            _line(jid, line.rstrip())
    proc.wait()
    return proc.returncode


# Bootstrap helpers -----------------------------------------------------------

def _unique_paths(paths):
    seen = set()
    for path in paths:
        if not path:
            continue
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        yield path


def _uv_path_candidates():
    names = ["uv.exe"] if IS_WINDOWS else ["uv"]
    paths = []

    found = shutil.which("uv")
    if found:
        paths.append(found)

    home = Path.home()
    base_dirs = [home / ".local" / "bin", home / ".cargo" / "bin"]
    if IS_WINDOWS:
        base_dirs.extend([
            Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "Programs" / "uv",
            Path(os.path.expandvars(r"%PROGRAMFILES%")) / "uv",
        ])

    for directory in base_dirs:
        for name in names:
            paths.append(str(directory / name))

    return list(_unique_paths(paths))


def _find_uv_executable():
    for candidate in _uv_path_candidates():
        path = Path(candidate)
        if not path.is_file():
            continue
        if IS_WINDOWS or os.access(path, os.X_OK):
            return str(path)
    return None


def _uv_version(uv_exe=None):
    uv_exe = uv_exe or _find_uv_executable()
    if not uv_exe:
        return None
    out, rc = _run(uv_exe, ["--version"], timeout=8)
    if rc != 0 or not out:
        return None
    return out.splitlines()[0].strip()


def _prepend_to_path(directory):
    directory = str(Path(directory).resolve())
    existing = os.environ.get("PATH", "").split(os.pathsep)
    if not any(_norm(item) == _norm(directory) for item in existing if item):
        os.environ["PATH"] = os.pathsep.join([directory, *existing])


def _refresh_uv_path(uv_exe=None):
    uv_exe = uv_exe or _find_uv_executable()
    if uv_exe:
        _prepend_to_path(Path(uv_exe).parent)
    return uv_exe


def _app_venv_status():
    venv_python = _app_venv_python()
    cfg = _read_pyvenv_cfg()
    info = _run_json_python(str(venv_python)) if venv_python.is_file() else None
    home = cfg.get("home", "")
    uv_backed = "uv" in cfg or "/uv/python/" in _posixish(home) or "\\uv\\python\\" in home.lower()

    return {
        "directory": str(APP_VENV_DIR),
        "directory_exists": APP_VENV_DIR.is_dir(),
        "exists": venv_python.is_file(),
        "python": str(venv_python),
        "version": (info or {}).get("version") or cfg.get("version_info"),
        "running_from_venv": _is_relative_to(sys.executable, APP_VENV_DIR) if APP_VENV_DIR.exists() else False,
        "uv_backed": uv_backed,
    }


def _bootstrap_status():
    uv_exe = _find_uv_executable()
    return {
        "platform": _platform_label(),
        "uv": {
            "installed": bool(uv_exe),
            "path": uv_exe,
            "version": _uv_version(uv_exe) if uv_exe else None,
        },
        "app_venv": _app_venv_status(),
    }


def _install_uv_if_missing(jid):
    uv_exe = _find_uv_executable()
    if uv_exe:
        _refresh_uv_path(uv_exe)
        version = _uv_version(uv_exe) or "uv"
        _line(jid, f"[ok] {version} already installed.")
        _line(jid, f"  Path: {uv_exe}")
        return uv_exe

    if IS_WINDOWS:
        _line(jid, "> Installing uv with the Windows standalone installer.")
        proc = _spawn([
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "ByPass",
            "-Command",
            "irm https://astral.sh/uv/install.ps1 | iex",
        ], cwd=str(APP_DIR))
    elif IS_LINUX:
        _line(jid, "> Installing uv with the Linux standalone installer.")
        if shutil.which("curl"):
            script = "curl -LsSf https://astral.sh/uv/install.sh | sh"
        elif shutil.which("wget"):
            script = "wget -qO- https://astral.sh/uv/install.sh | sh"
        else:
            raise RuntimeError("curl or wget is required to install uv on Linux.")
        proc = _spawn(["/bin/sh", "-c", script], cwd=str(APP_DIR))
    else:
        raise RuntimeError("Automatic uv install is supported on Windows and Linux.")

    rc = _stream_proc(proc, jid)
    if rc != 0:
        raise RuntimeError(f"uv installer exited with code {rc}")

    uv_exe = _refresh_uv_path()
    if not uv_exe:
        raise RuntimeError("uv installed, but PyManager could not find the uv executable. Restart the app and try again.")

    version = _uv_version(uv_exe) or "uv"
    _line(jid, f"[ok] {version} installed.")
    _line(jid, f"  Path: {uv_exe}")
    return uv_exe


def _app_venv_specs():
    specs = [DEFAULT_APP_PYTHON]
    if DEFAULT_APP_PYTHON.count(".") >= 1:
        specs.append(".".join(DEFAULT_APP_PYTHON.split(".")[:2]))
    specs.extend(["3.14", "3.13", "3.12", "3.11"])
    unique = []
    for spec in specs:
        if spec and spec not in unique:
            unique.append(spec)
    return unique


def _prepare_app_venv_dir(jid):
    venv_python = _app_venv_python()
    if not APP_VENV_DIR.exists() or venv_python.is_file():
        return

    backup = APP_DIR / f".venv.incomplete-{int(time.time())}"
    _line(jid, f"> Moving incomplete .venv aside: {backup}")
    APP_VENV_DIR.rename(backup)


def _ensure_app_venv(jid, uv_exe):
    uv_exe = _refresh_uv_path(uv_exe) or uv_exe
    if not uv_exe:
        raise RuntimeError("uv is required before creating the app virtual environment.")

    venv_python = _app_venv_python()
    if venv_python.is_file():
        _line(jid, "[ok] App virtual environment already exists.")
        _line(jid, f"  Python: {venv_python}")
    else:
        _prepare_app_venv_dir(jid)
        last_rc = None
        for spec in _app_venv_specs():
            _line(jid, f"> Creating app virtual environment with Python {spec}.")
            proc = _spawn([
                uv_exe,
                "venv",
                "--managed-python",
                "--python",
                spec,
                str(APP_VENV_DIR),
            ], cwd=str(APP_DIR))
            last_rc = _stream_proc(proc, jid)
            if last_rc == 0 and venv_python.is_file():
                break
            _line(jid, f"  Python {spec} venv attempt exited with code {last_rc}.")

        if not venv_python.is_file():
            raise RuntimeError(f"Could not create .venv with uv (last exit code {last_rc}).")

        _line(jid, f"[ok] App virtual environment created at {APP_VENV_DIR}.")

    requirements = APP_DIR / "requirements.txt"
    if requirements.is_file():
        _line(jid, "> Installing/updating app requirements with uv.")
        proc = _spawn([
            uv_exe,
            "pip",
            "install",
            "--python",
            str(venv_python),
            "-r",
            str(requirements),
        ], cwd=str(APP_DIR))
        rc = _stream_proc(proc, jid)
        if rc != 0:
            raise RuntimeError(f"requirements install exited with code {rc}")

    status = _app_venv_status()
    version = status.get("version") or "unknown"
    _line(jid, f"[ok] App venv is ready. Python {version}.")


# Install helpers -------------------------------------------------------------

def _install_methods():
    methods = []
    if IS_WINDOWS:
        methods.extend([
            {
                "id": "winget",
                "label": "winget",
                "short": "WG",
                "description": "Windows Package Manager",
                "available": shutil.which("winget") is not None,
            },
            {
                "id": "direct",
                "label": "Direct download",
                "short": "DL",
                "description": "python.org Windows installer",
                "available": True,
            },
        ])

    methods.extend([
        {
            "id": "uv",
            "label": "uv",
            "short": "UV",
            "description": "uv-managed Python install",
            "available": _find_uv_executable() is not None,
        },
        {
            "id": "pyenv",
            "label": "pyenv",
            "short": "PE",
            "description": "pyenv-managed build/install",
            "available": shutil.which("pyenv") is not None,
        },
    ])

    if not IS_WINDOWS:
        methods.append({
            "id": "asdf",
            "label": "asdf",
            "short": "AS",
            "description": "asdf Python plugin",
            "available": shutil.which("asdf") is not None,
        })

    return methods


def _install_with_winget(jid, version):
    parts = version.split(".")
    package = f"Python.Python.{parts[0]}.{parts[1]}" if len(parts) >= 2 else f"Python.Python.{parts[0]}"
    _line(jid, f"> Installing Python {version} with winget.")
    _line(jid, f"  Package: {package}")
    proc = _spawn([
        "winget",
        "install",
        "-e",
        "--id",
        package,
        "--version",
        version,
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ])
    return _stream_proc(proc, jid)


def _install_direct_windows(jid, version, arch):
    suffix = "-amd64" if arch == "64" else ""
    file_name = f"python-{version}{suffix}.exe"
    url = f"https://www.python.org/ftp/python/{version}/{file_name}"
    dest = os.path.join(tempfile.gettempdir(), file_name)
    _line(jid, f"> Downloading {url}")
    last_percent = {"value": -1}

    def progress(block_count, block_size, total_size):
        if total_size <= 0:
            return
        percent = min(100, int(block_count * block_size * 100 / total_size))
        if percent != last_percent["value"] and percent % 10 == 0:
            last_percent["value"] = percent
            _line(jid, f"  Download {percent}%")

    urllib.request.urlretrieve(url, dest, reporthook=progress)
    _line(jid, "> Running the Python installer.")
    proc = _spawn([dest, "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1"])
    rc = _stream_proc(proc, jid)
    try:
        os.unlink(dest)
    except OSError:
        pass
    return rc


# Removal helpers -------------------------------------------------------------

def _find_registry_uninstall(version):
    if not IS_WINDOWS or winreg is None:
        return None, None

    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, key_path in uninstall_roots:
        try:
            with winreg.OpenKey(hive, key_path) as root:
                index = 0
                while True:
                    try:
                        subkey = winreg.EnumKey(root, index)
                        index += 1
                    except OSError:
                        break

                    try:
                        with winreg.OpenKey(root, subkey) as app_key:
                            display_name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                            uninstall, _ = winreg.QueryValueEx(app_key, "UninstallString")
                    except OSError:
                        continue

                    display = str(display_name)
                    if "Python" in display and version in display:
                        return display, str(uninstall)
        except (OSError, PermissionError):
            continue
    return None, None


def _protected_delete_roots():
    roots = [APP_DIR, Path.home()]
    if IS_WINDOWS:
        for env, fallback in [
            ("SystemRoot", r"C:\Windows"),
            ("PROGRAMFILES", r"C:\Program Files"),
            ("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            ("LOCALAPPDATA", ""),
            ("APPDATA", ""),
        ]:
            value = os.environ.get(env, fallback)
            if value:
                roots.append(Path(value))
    else:
        roots.extend([
            Path("/"),
            Path("/bin"),
            Path("/sbin"),
            Path("/etc"),
            Path("/usr"),
            Path("/usr/bin"),
            Path("/usr/local"),
            Path("/usr/local/bin"),
            Path("/opt"),
            Path.home() / ".local",
        ])
    return roots


def _dangerous_delete_root(root, exe):
    try:
        root_path = Path(root).resolve()
        exe_path = Path(exe).resolve()
    except OSError:
        return "Could not resolve the target path."

    if not root_path.is_dir():
        return "Target folder does not exist."
    if not exe_path.is_file() or not _is_relative_to(exe_path, root_path):
        return "The Python executable is not inside the target folder."
    if _norm(exe_path) == _norm(sys.executable):
        return "Cannot remove the Python that is running PyManager."
    if _is_relative_to(APP_DIR, root_path):
        return "Refusing to remove a folder that contains PyManager itself."

    for item in _protected_delete_roots():
        try:
            if root_path == item.resolve():
                return f"Refusing to remove protected folder: {root_path}"
        except OSError:
            continue

    if len(root_path.parts) < (3 if IS_WINDOWS else 4):
        return f"Refusing to remove a top-level folder: {root_path}"

    return None


def _delete_install_folder(jid, path, exe):
    root = _expand(path or os.path.dirname(exe))
    reason = _dangerous_delete_root(root, exe)
    if reason:
        _line(jid, f"[error] {reason}")
        return False

    _line(jid, f"> Deleting folder: {root}")
    try:
        shutil.rmtree(root)
        _line(jid, "[ok] Folder removed.")
        return True
    except Exception as exc:
        _line(jid, f"[error] Could not delete folder: {exc}")
        return False


def _remove_with_winget(jid, version):
    if not IS_WINDOWS:
        return False

    parts = version.split(".")
    if len(parts) < 2:
        return False
    package = f"Python.Python.{parts[0]}.{parts[1]}"
    _line(jid, f"> Trying winget uninstall: {package}")
    try:
        proc = _spawn(["winget", "uninstall", "--id", package, "--silent", "--accept-source-agreements"])
    except FileNotFoundError:
        _line(jid, "  winget was not found.")
        return False
    except Exception as exc:
        _line(jid, f"  winget error: {exc}")
        return False

    rc = _stream_proc(proc, jid)
    if rc == 0:
        _line(jid, "[ok] winget uninstall completed.")
        return True
    _line(jid, f"  winget exited with code {rc}.")
    return False


def _remove_pymanager(jid, version):
    if not IS_WINDOWS:
        return False
    spec = ".".join(version.split(".")[:2])
    _line(jid, f"> Trying pymanager uninstall: {spec}")
    try:
        proc = _spawn(["pymanager", "uninstall", "-y", spec])
        rc = _stream_proc(proc, jid)
    except Exception as exc:
        _line(jid, f"  pymanager error: {exc}")
        return False

    if rc == 0:
        _line(jid, "[ok] pymanager uninstall completed.")
        return True
    _line(jid, f"  pymanager exited with code {rc}.")
    return False


def _remove_standard(jid, version):
    if not IS_WINDOWS:
        _line(jid, "[error] System Python installs are not removed automatically on Linux.")
        return False

    if _remove_with_winget(jid, version):
        return True

    _line(jid, "> Looking for a registry uninstaller.")
    display, uninstall = _find_registry_uninstall(version)
    if not uninstall:
        _line(jid, "  No matching registry uninstaller found.")
        return False

    _line(jid, f"  Found: {display}")
    try:
        proc = subprocess.Popen(
            f'{uninstall} /quiet',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **_popen_kwargs(),
        )
        rc = _stream_proc(proc, jid)
    except Exception as exc:
        _line(jid, f"[error] Registry uninstall failed to start: {exc}")
        return False

    if rc == 0:
        _line(jid, "[ok] Registry uninstaller completed.")
        return True
    _line(jid, f"  Registry uninstaller exited with code {rc}.")
    return False


def _remove_pyenv(jid, path, exe):
    version_name = os.path.basename(_expand(path or os.path.dirname(exe)))
    _line(jid, f"> Trying pyenv uninstall: {version_name}")
    try:
        proc = _spawn(["pyenv", "uninstall", "-f", version_name])
        rc = _stream_proc(proc, jid)
        if rc == 0:
            _line(jid, "[ok] pyenv uninstall completed.")
            return True
    except Exception as exc:
        _line(jid, f"  pyenv error: {exc}")

    if "/.pyenv/versions/" in _posixish(path) or "/pyenv-win/versions/" in _posixish(path):
        return _delete_install_folder(jid, path, exe)
    return False


def _uv_spec_from_path(path, version):
    name = os.path.basename(_expand(path))
    match = re.search(r"cpython-(\d+\.\d+\.\d+)", name, re.IGNORECASE)
    return match.group(1) if match else version


def _remove_uv(jid, path, exe, version):
    spec = _uv_spec_from_path(path, version)
    _line(jid, f"> Trying uv python uninstall: {spec}")
    try:
        proc = _spawn(["uv", "python", "uninstall", spec])
        rc = _stream_proc(proc, jid)
        if rc == 0:
            _line(jid, "[ok] uv uninstall completed.")
            return True
        _line(jid, f"  uv exited with code {rc}.")
    except Exception as exc:
        _line(jid, f"  uv error: {exc}")

    if "/uv/python/" in _posixish(path):
        return _delete_install_folder(jid, path, exe)
    return False


def _remove_asdf(jid, path, exe, version):
    version_name = os.path.basename(_expand(path or os.path.dirname(exe))) or version
    _line(jid, f"> Trying asdf uninstall: {version_name}")
    try:
        proc = _spawn(["asdf", "uninstall", "python", version_name])
        rc = _stream_proc(proc, jid)
        if rc == 0:
            _line(jid, "[ok] asdf uninstall completed.")
            return True
        _line(jid, f"  asdf exited with code {rc}.")
    except Exception as exc:
        _line(jid, f"  asdf error: {exc}")

    if "/.asdf/installs/python/" in _posixish(path):
        return _delete_install_folder(jid, path, exe)
    return False


def _remove_conda_env(jid, path):
    _line(jid, f"> Trying conda env remove: {path}")
    for command in ("conda", "mamba", "micromamba"):
        try:
            proc = _spawn([command, "env", "remove", "-p", path, "-y"])
            rc = _stream_proc(proc, jid)
        except FileNotFoundError:
            continue
        except Exception as exc:
            _line(jid, f"  {command} error: {exc}")
            continue

        if rc == 0:
            _line(jid, f"[ok] {command} removed the environment.")
            return True
        _line(jid, f"  {command} exited with code {rc}.")
    return False


def _remove_scoop(jid, path):
    if not IS_WINDOWS:
        return False

    parts = Path(path).parts
    app_name = None
    for index, part in enumerate(parts):
        if part.lower() == "apps" and index + 1 < len(parts):
            app_name = parts[index + 1]
            break
    if not app_name:
        return False

    _line(jid, f"> Trying scoop uninstall: {app_name}")
    try:
        proc = _spawn(["scoop", "uninstall", app_name])
        rc = _stream_proc(proc, jid)
    except Exception as exc:
        _line(jid, f"  scoop error: {exc}")
        return False

    if rc == 0:
        _line(jid, "[ok] scoop uninstall completed.")
        return True
    _line(jid, f"  scoop exited with code {rc}.")
    return False


# Routes ----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/platform")
def app_platform():
    return jsonify(
        success=True,
        platform=_platform_label(),
        system=platform.system(),
        release=platform.release(),
        python=sys.version.split()[0],
        install_methods=_install_methods(),
    )


@app.route("/api/bootstrap")
def bootstrap_status():
    return jsonify(success=True, **_bootstrap_status())


@app.route("/api/pythons")
def list_pythons():
    try:
        return jsonify(success=True, pythons=_scan())
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 500


@app.route("/api/custom-paths", methods=["GET", "POST", "DELETE"])
def custom_paths():
    config = _load_config()

    if request.method == "GET":
        return jsonify(success=True, paths=config["custom_paths"])

    data = request.json or {}
    raw_path = str(data.get("path", "")).strip()
    if not raw_path:
        return jsonify(success=False, error="path required"), 400

    path = _expand(raw_path)

    if request.method == "DELETE":
        config["custom_paths"] = [p for p in config["custom_paths"] if _norm(p) != _norm(path)]
        _save_config(config)
        return jsonify(success=True, paths=config["custom_paths"])

    if not os.path.exists(path):
        return jsonify(success=False, error="path does not exist"), 400

    config["custom_paths"].append(path)
    _save_config(config)
    return jsonify(success=True, paths=_load_config()["custom_paths"])


@app.route("/api/available-versions")
def available_versions():
    try:
        url = "https://endoflife.date/api/python.json"
        req = urllib.request.Request(url, headers={"User-Agent": "PyManager/1.0"})
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.load(response)
        versions = [
            {
                "cycle": item["cycle"],
                "latest": item["latest"],
                "eol": item.get("eol", False),
                "support": item.get("support", ""),
                "release_date": item.get("releaseDate", ""),
            }
            for item in data
            if str(item.get("cycle", "")).startswith("3.")
        ]
        return jsonify(success=True, versions=versions)
    except Exception:
        fallback = [
            {"cycle": "3.14", "latest": "3.14.0", "eol": False, "support": "", "release_date": "2025-10-07"},
            {"cycle": "3.13", "latest": "3.13.3", "eol": False, "support": "", "release_date": "2024-10-07"},
            {"cycle": "3.12", "latest": "3.12.10", "eol": False, "support": "", "release_date": "2023-10-02"},
            {"cycle": "3.11", "latest": "3.11.13", "eol": False, "support": "", "release_date": "2022-10-24"},
            {"cycle": "3.10", "latest": "3.10.17", "eol": False, "support": "", "release_date": "2021-10-04"},
        ]
        return jsonify(success=True, versions=fallback, cached=True)


@app.route("/api/uv/install", methods=["POST"])
def install_uv():
    jid = _new_job("install_uv")

    def worker():
        try:
            uv_exe = _install_uv_if_missing(jid)
            _ensure_app_venv(jid, uv_exe)
            _done(jid, True)
        except Exception as exc:
            _line(jid, f"[error] {exc}")
            _done(jid, False, str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify(success=True, job_id=jid)


@app.route("/api/pythons/install", methods=["POST"])
def install_python():
    data = request.json or {}
    version = str(data.get("version", "")).strip()
    method = str(data.get("method", "")).strip()
    arch = str(data.get("arch", "64"))

    if not version:
        return jsonify(success=False, error="version required"), 400
    if method not in {m["id"] for m in _install_methods()}:
        return jsonify(success=False, error="unsupported install method"), 400

    jid = _new_job("install")

    def worker():
        try:
            if method == "winget":
                if not IS_WINDOWS:
                    raise RuntimeError("winget installs are only available on Windows.")
                rc = _install_with_winget(jid, version)
            elif method == "direct":
                if not IS_WINDOWS:
                    raise RuntimeError("Direct python.org installers are only available on Windows.")
                rc = _install_direct_windows(jid, version, arch)
            elif method == "uv":
                uv_exe = _find_uv_executable()
                if not uv_exe:
                    raise RuntimeError("uv is not installed. Install uv from the setup panel first.")
                _line(jid, f"> Installing Python {version} with uv.")
                rc = _stream_proc(_spawn([uv_exe, "python", "install", version]), jid)
            elif method == "pyenv":
                _line(jid, f"> Installing Python {version} with pyenv.")
                rc = _stream_proc(_spawn(["pyenv", "install", "-s", version]), jid)
            elif method == "asdf":
                _line(jid, f"> Installing Python {version} with asdf.")
                rc = _stream_proc(_spawn(["asdf", "install", "python", version]), jid)
            else:
                raise RuntimeError("Unsupported install method.")

            if rc == 0:
                _line(jid, "[ok] Installation completed.")
                _done(jid, True)
            else:
                _line(jid, f"[error] Installer exited with code {rc}.")
                _done(jid, False, f"install exit {rc}")
        except FileNotFoundError as exc:
            _line(jid, f"[error] Tool not found: {exc.filename}")
            _done(jid, False, f"tool not found: {exc.filename}")
        except Exception as exc:
            _line(jid, f"[error] {exc}")
            _done(jid, False, str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify(success=True, job_id=jid)


@app.route("/api/pythons/uninstall", methods=["POST"])
def uninstall_python():
    data = request.json or {}
    exe = str(data.get("executable", ""))
    path = str(data.get("path", "")) or os.path.dirname(exe)
    version = str(data.get("version", ""))
    kind = str(data.get("type", ""))

    if not exe or not os.path.isfile(exe):
        return jsonify(success=False, error="invalid executable"), 400
    if _norm(exe) == _norm(sys.executable):
        return jsonify(success=False, error="cannot remove the Python running PyManager"), 400

    jid = _new_job("uninstall")

    def worker():
        _line(jid, f"> Removing Python {version}")
        _line(jid, f"  Path: {path}")
        ok = False

        if kind == "standard":
            ok = _remove_standard(jid, version)
        elif kind == "pymanager":
            ok = _remove_pymanager(jid, version)
        elif kind == "pyenv":
            ok = _remove_pyenv(jid, path, exe)
        elif kind == "uv":
            ok = _remove_uv(jid, path, exe, version)
        elif kind == "asdf":
            ok = _remove_asdf(jid, path, exe, version)
        elif kind == "conda-env":
            ok = _remove_conda_env(jid, path)
        elif kind == "scoop":
            ok = _remove_scoop(jid, path)
        elif kind in {"portable", "custom"}:
            ok = _delete_install_folder(jid, path, exe)
        else:
            _line(jid, "[error] This source is detected but is not safe to remove automatically.")

        if ok:
            _done(jid, True)
        else:
            _line(jid, "[error] Automatic removal did not complete.")
            _line(jid, "  Remove it with its original manager or system package manager.")
            _done(jid, False, "automatic removal failed")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify(success=True, job_id=jid)


@app.route("/api/jobs/<jid>")
def get_job(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify(success=False, error="job not found"), 404
    return jsonify(success=True, job=dict(job))


@app.route("/api/pythons/open-folder", methods=["POST"])
def open_folder():
    path = str((request.json or {}).get("path", ""))
    if not os.path.isdir(path):
        return jsonify(success=False, error="not a directory"), 400

    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            opener = shutil.which("xdg-open") or shutil.which("gio") or shutil.which("kde-open")
            if not opener:
                return jsonify(success=False, error="no folder opener found"), 500
            args = [opener, "open", path] if os.path.basename(opener) == "gio" else [opener, path]
            subprocess.Popen(args)
        return jsonify(success=True)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 500


def _linux_terminal_command(exe, cwd):
    candidates = [
        ("gnome-terminal", ["gnome-terminal", "--working-directory", cwd, "--", exe]),
        ("konsole", ["konsole", "--workdir", cwd, "-e", exe]),
        ("xfce4-terminal", ["xfce4-terminal", "--working-directory", cwd, "--command", exe]),
        ("xterm", ["xterm", "-e", exe]),
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", exe]),
    ]
    for command, args in candidates:
        found = shutil.which(command)
        if found:
            args[0] = found
            return args
    return None


@app.route("/api/pythons/open-terminal", methods=["POST"])
def open_terminal():
    data = request.json or {}
    exe = str(data.get("executable", ""))
    path = str(data.get("path", "")) or os.path.dirname(exe)
    if not os.path.isfile(exe):
        return jsonify(success=False, error="invalid executable"), 400

    try:
        if IS_WINDOWS:
            subprocess.Popen(["cmd", "/k", exe], cwd=path, creationflags=NEW_CONSOLE)
        elif sys.platform == "darwin":
            script = f'tell application "Terminal" to do script "cd {path}; {exe}"'
            subprocess.Popen(["osascript", "-e", script])
        else:
            command = _linux_terminal_command(exe, path)
            if not command:
                return jsonify(success=False, error="no terminal emulator found"), 500
            subprocess.Popen(command, cwd=path)
        return jsonify(success=True)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 500


if __name__ == "__main__":
    def choose_port():
        if os.environ.get("PYMANAGER_PORT"):
            return int(os.environ["PYMANAGER_PORT"])
        for candidate in range(5000, 5020):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                if sock.connect_ex(("127.0.0.1", candidate)) != 0:
                    return candidate
        return 5000

    port = choose_port()
    print(f"\n  PyManager -> http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
