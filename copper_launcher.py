#!/usr/bin/env python3
"""Copper Launcher — post-startup check and sequential Ubuntu app launcher.

Native PySide6 (Qt 6) desktop app. Edit the configuration block below if a
binary lives somewhere unusual or a service uses a different port.

Setup (from this directory):

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python copper_launcher.py
    .venv/bin/python copper_launcher.py --auto-start
"""

from __future__ import annotations

import copy
import glob
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AUTO_START_FLAG = "--auto-start"
STOP_ON_FAILURE = False
VERIFY_TIMEOUT_SEC = 20
TELEMETRY_INTERVAL_MS = 2000
HTTP_TIMEOUT_SEC = 0.8
LOG_DIR = Path.home() / ".local" / "state" / "copper-launcher" / "logs"
SEQUENCE_PATH = Path.home() / ".config" / "copper-launcher" / "sequence.json"

# Optional absolute overrides. Leave "" to auto-detect.
LM_STUDIO_EXECUTABLE = ""
OLLAMA_EXECUTABLE = ""
LEMONADE_EXECUTABLE = ""
YOUTUBE_MUSIC_EXECUTABLE = ""
BRAVE_EXECUTABLE = ""
CURSOR_EXECUTABLE = ""

LM_STUDIO_PORT = 1234
OLLAMA_PORT = 11434
# Current `lemonade` builds default to 13305. The launcher binds LEMONADE_PORT
# and still treats 13305 as already-running so it does not start a second copy.
LEMONADE_PORT = 8000
LEMONADE_ALT_PORTS = [13305]

LM_STUDIO_FLATPAK_ID = "ai.lmstudio.lm-studio"
OLLAMA_SERVICE = "ollama"
# Brave PWA id for YouTube Music on this machine. A matching desktop file wins
# when its Exec line carries a different --app-id.
YOUTUBE_MUSIC_APP_ID = "cinhimbnkkaeohfgghhklpknlkffjgod"

LM_STUDIO_GLOBS = [
    "~/Applications/LM-Studio*.AppImage",
    "~/Applications/lm-studio*.AppImage",
    "~/Applications/LM Studio*.AppImage",
    "~/Downloads/LM-Studio*.AppImage",
    "~/Downloads/lm-studio*.AppImage",
    "~/Downloads/*LM*Studio*.AppImage",
    "~/.local/bin/LM-Studio*.AppImage",
    "~/.local/bin/lm-studio*.AppImage",
]
LEMONADE_GLOBS = [
    "~/Applications/lemonade*.AppImage",
    "~/Applications/Lemonade*.AppImage",
    "~/Downloads/lemonade*.AppImage",
    "~/Downloads/Lemonade*.AppImage",
]
CURSOR_GLOBS = [
    "~/Applications/cursor*.AppImage",
    "~/Applications/Cursor*.AppImage",
    "~/Downloads/cursor*.AppImage",
    "~/Downloads/Cursor*.AppImage",
]
YOUTUBE_MUSIC_FLATPAKS = [
    "app.ytmdesktop.ytmdesktop",
    "com.github.th_ch.youtube_music",
]
BRAVE_CANDIDATES = ["brave-browser", "brave-browser-stable", "brave"]
CURSOR_CANDIDATES = ["/usr/share/cursor/cursor", "cursor"]
DESKTOP_FILE_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
]

PENDING = "Pending"
CHECKING = "Checking..."
LAUNCHING = "Launching..."
VERIFIED = "Verified Running"
FAILED = "Failed"

INSTALL_HELP = """\
Copper Launcher needs PySide6, psutil, and requests.

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python copper_launcher.py
"""

try:
    import psutil
    import requests
    from PySide6.QtCore import (
        QEasingCurve,
        QPoint,
        QProcess,
        QRectF,
        QSize,
        QThread,
        QTimer,
        Qt,
        QVariantAnimation,
        Signal,
    )
    from PySide6.QtGui import (
        QColor,
        QConicalGradient,
        QFont,
        QFontDatabase,
        QIcon,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPainterPathStroker,
        QPalette,
        QPen,
        QPolygon,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDialog,
        QFormLayout,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    sys.stderr.write(INSTALL_HELP)
    raise SystemExit(1)


HTTP = requests.Session()
HTTP.trust_env = False

_RESOLVED: dict[str, tuple[list[str] | None, list[str]]] = {}


def _app(
    key: str,
    name: str,
    short: str,
    mark: str,
    process_label: str,
    verify: str,
    port: int | None = None,
    alt_ports: list[int] | None = None,
    http_path: str | None = None,
    basenames: list[str] | None = None,
    cmd_contains: list[str] | None = None,
    cmd_excludes: list[str] | None = None,
    timeout: int = VERIFY_TIMEOUT_SEC,
) -> dict:
    detail_bits = []
    if port:
        detail_bits.append(f"port {port}")
    detail_bits.append(process_label)
    return {
        "key": key,
        "name": name,
        "short": short,
        "mark": mark,
        "process_label": process_label,
        "detail": " · ".join(detail_bits),
        "verify": verify,
        "port": port,
        "alt_ports": list(alt_ports or []),
        "http_path": http_path,
        "basenames": list(basenames or []),
        "cmd_contains": list(cmd_contains or []),
        "cmd_excludes": list(cmd_excludes or []),
        "timeout": timeout,
    }


# Built-in catalog. The live sequence is loaded from sequence.json and can drop
# these or add custom programs.
DEFAULT_APPS: list[dict] = [
    _app(
        "lmstudio",
        "LM Studio",
        "LM",
        "LM",
        "lm-studio",
        "process_or_port",
        port=LM_STUDIO_PORT,
        http_path="/v1/models",
        basenames=["lm-studio", "lmstudio", "LM-Studio"],
        cmd_contains=["ai.lmstudio.lm-studio", "LM-Studio", "lm-studio", "lmstudio"],
        cmd_excludes=["--type="],
        timeout=25,
    ),
    _app(
        "ollama",
        "Ollama",
        "Ollama",
        "Ol",
        "ollama",
        "http",
        port=OLLAMA_PORT,
        http_path="/api/tags",
        basenames=["ollama"],
        cmd_contains=["ollama serve"],
        timeout=20,
    ),
    _app(
        "lemonade",
        "Lemonade",
        "Lemon",
        "Le",
        "lemond",
        "process_or_port",
        port=LEMONADE_PORT,
        alt_ports=LEMONADE_ALT_PORTS,
        basenames=["lemond", "lemonade-server"],
        cmd_contains=["lemonade-server", "lemond"],
        timeout=20,
    ),
    _app(
        "youtube",
        "YouTube Music",
        "Music",
        "YT",
        "youtube-music",
        "process",
        basenames=["youtube-music", "youtube-music-desktop"],
        cmd_contains=[
            f"--app-id={YOUTUBE_MUSIC_APP_ID}",
            "music.youtube.com",
        ],
        cmd_excludes=["--type="],
        timeout=15,
    ),
    _app(
        "brave",
        "Brave",
        "Brave",
        "Br",
        "brave-browser",
        "process",
        basenames=["brave", "brave-browser", "brave-browser-stable"],
        cmd_excludes=["--type=", "--app-id="],
        timeout=15,
    ),
    _app(
        "cursor",
        "Cursor",
        "Cursor",
        "Cu",
        "cursor",
        "process",
        basenames=["cursor"],
        cmd_contains=["/usr/share/cursor/cursor", "cursor.AppImage"],
        cmd_excludes=["--type=", "copper_launcher.py"],
        timeout=20,
    ),
]


# ---------------------------------------------------------------------------
# Detection helpers (called from worker threads only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    running: bool
    port_open: bool
    open_port: int | None
    http_ok: bool
    summary: str


def _which(name: str) -> str | None:
    if not name:
        return None
    path = Path(name).expanduser()
    if path.is_absolute() or name.startswith("."):
        return str(path) if path.is_file() else None
    return shutil.which(name)


def _first_existing(candidates: list[str]) -> str | None:
    for candidate in candidates:
        found = _which(candidate)
        if found:
            return found
    return None


def _glob_matches(patterns: list[str]) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        found.extend(sorted(glob.glob(os.path.expanduser(pattern))))
    return found


def _flatpak_installed(app_id: str) -> bool:
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return False
    try:
        result = subprocess.run(
            [flatpak, "info", app_id],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _desktop_entries():
    for root in DESKTOP_FILE_DIRS:
        if not root.is_dir():
            continue
        yield from root.glob("*.desktop")


def _parse_desktop(path: Path) -> tuple[str | None, list[str]]:
    name = None
    exec_line = None
    in_entry = False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, []
    for raw in lines:
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "Name" and name is None:
            name = value.strip()
        elif key == "Exec" and exec_line is None:
            exec_line = value.strip()
    if not exec_line:
        return name, []
    try:
        parts = shlex.split(exec_line)
    except ValueError:
        parts = exec_line.split()
    argv = [part for part in parts if part and not part.startswith("%")]
    return name, argv


def _remember_match_tokens(app: dict, argv: list[str]) -> None:
    for token in argv:
        if token.startswith("--app-id=") and token not in app["cmd_contains"]:
            app["cmd_contains"].append(token)


def _resolve_lmstudio() -> tuple[list[str] | None, list[str]]:
    notes: list[str] = []
    if LM_STUDIO_EXECUTABLE:
        notes.append(LM_STUDIO_EXECUTABLE)
        found = _which(LM_STUDIO_EXECUTABLE)
        if found:
            return [found], notes
    for match in _glob_matches(LM_STUDIO_GLOBS):
        notes.append(match)
        return [match], notes
    for name in ("lm-studio", "lmstudio"):
        notes.append(name)
        found = shutil.which(name)
        if found:
            return [found], notes
    notes.append(f"flatpak:{LM_STUDIO_FLATPAK_ID}")
    if _flatpak_installed(LM_STUDIO_FLATPAK_ID):
        return ["flatpak", "run", LM_STUDIO_FLATPAK_ID], notes
    lms = shutil.which("lms")
    notes.append("lms")
    if lms:
        return [lms, "server", "start"], notes
    return None, notes


def _resolve_ollama() -> tuple[list[str] | None, list[str]]:
    notes = [OLLAMA_EXECUTABLE or "ollama"]
    found = _which(OLLAMA_EXECUTABLE or "ollama")
    if not found:
        return None, notes
    return [found, "serve"], notes


def _resolve_lemonade() -> tuple[list[str] | None, list[str]]:
    notes: list[str] = []
    port = str(LEMONADE_PORT)
    if LEMONADE_EXECUTABLE:
        notes.append(LEMONADE_EXECUTABLE)
        found = _which(LEMONADE_EXECUTABLE)
        if found:
            return [found, "--port", port], notes
    for match in _glob_matches(LEMONADE_GLOBS):
        notes.append(match)
        return [match, "--port", port], notes
    lemond = shutil.which("lemond")
    notes.append("lemond")
    if lemond:
        return [lemond, "--port", port], notes
    server = shutil.which("lemonade-server")
    notes.append("lemonade-server")
    if server:
        return [server, "serve", "--port", port], notes
    return None, notes


def _resolve_youtube() -> tuple[list[str] | None, list[str]]:
    notes: list[str] = []
    if YOUTUBE_MUSIC_EXECUTABLE:
        notes.append(YOUTUBE_MUSIC_EXECUTABLE)
        if Path(YOUTUBE_MUSIC_EXECUTABLE).expanduser().is_file() or shutil.which(
            YOUTUBE_MUSIC_EXECUTABLE
        ):
            argv = shlex.split(os.path.expanduser(YOUTUBE_MUSIC_EXECUTABLE))
            return argv, notes
    for name in ("youtube-music", "youtube-music-desktop"):
        notes.append(name)
        found = shutil.which(name)
        if found:
            return [found], notes
    desktop_hits: list[list[str]] = []
    for path in _desktop_entries():
        name, argv = _parse_desktop(path)
        if name == "YouTube Music" and argv:
            notes.append(str(path))
            desktop_hits.append(argv)
    if desktop_hits:
        preferred = next(
            (argv for argv in desktop_hits if any(part.startswith("--app-id=") for part in argv)),
            desktop_hits[0],
        )
        return preferred, notes
    for app_id in YOUTUBE_MUSIC_FLATPAKS:
        notes.append(f"flatpak:{app_id}")
        if _flatpak_installed(app_id):
            return ["flatpak", "run", app_id], notes
    return None, notes


def _resolve_from_candidates(
    override: str,
    candidates: list[str],
    globs_list: list[str] | None = None,
) -> tuple[list[str] | None, list[str]]:
    notes: list[str] = []
    if override:
        notes.append(override)
        found = _which(override)
        if found:
            return [found], notes
    for match in _glob_matches(globs_list or []):
        notes.append(match)
        return [match], notes
    for candidate in candidates:
        notes.append(candidate)
        found = _which(candidate)
        if found:
            return [found], notes
    return None, notes


RESOLVERS = {
    "lmstudio": _resolve_lmstudio,
    "ollama": _resolve_ollama,
    "lemonade": _resolve_lemonade,
    "youtube": _resolve_youtube,
    "brave": lambda: _resolve_from_candidates(BRAVE_EXECUTABLE, BRAVE_CANDIDATES),
    "cursor": lambda: _resolve_from_candidates(CURSOR_EXECUTABLE, CURSOR_CANDIDATES, CURSOR_GLOBS),
}


def _resolve_command_string(command: str) -> tuple[list[str] | None, list[str]]:
    command = (command or "").strip()
    if not command:
        return None, ["(empty command)"]
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return None, [f"{command} ({exc})"]
    return (argv or None), [command]


def resolved_command(app: dict, refresh: bool = False) -> tuple[list[str] | None, list[str]]:
    key = app["key"]
    if refresh or key not in _RESOLVED:
        if app.get("custom"):
            argv, notes = _resolve_command_string(app.get("command", ""))
        else:
            argv, notes = RESOLVERS[key]()
        if argv:
            _remember_match_tokens(app, argv)
        _RESOLVED[key] = (argv, notes)
    return _RESOLVED[key]


def make_custom_app(
    *,
    name: str,
    command: str,
    process: str = "",
    port: int | None = None,
    verify: str = "process",
    http_path: str = "",
    key: str | None = None,
) -> dict:
    """Build a user-added program. `command` is a shell-style argument string."""
    name = name.strip() or "Program"
    command = command.strip()
    process_name = process.strip()
    if not process_name:
        argv, _notes = _resolve_command_string(command)
        process_name = Path(argv[0]).name if argv else name
    if verify not in {"process", "process_or_port", "http"}:
        verify = "process"
    mark = "".join(part[0] for part in name.split()[:2] if part).upper()[:2] or "Pr"
    app = _app(
        key or f"custom-{uuid.uuid4().hex[:8]}",
        name,
        name.split()[0][:10],
        mark,
        process_name,
        verify,
        port=port,
        http_path=http_path.strip() or ("/" if verify == "http" else None),
        basenames=[process_name],
        cmd_contains=[process_name],
        cmd_excludes=["--type="],
    )
    app["custom"] = True
    app["command"] = command
    return app


def _clone_builtin(key: str) -> dict | None:
    for app in DEFAULT_APPS:
        if app["key"] == key:
            return copy.deepcopy(app)
    return None


def load_sequence() -> list[dict]:
    """Return the saved launch order, or the built-in six when nothing is saved."""
    if not SEQUENCE_PATH.is_file():
        return [copy.deepcopy(app) for app in DEFAULT_APPS]
    try:
        raw = json.loads(SEQUENCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [copy.deepcopy(app) for app in DEFAULT_APPS]
    if not isinstance(raw, list):
        return [copy.deepcopy(app) for app in DEFAULT_APPS]
    loaded: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("custom"):
            port = item.get("port")
            try:
                port = int(port) if port else None
            except (TypeError, ValueError):
                port = None
            loaded.append(
                make_custom_app(
                    name=str(item.get("name") or "Program"),
                    command=str(item.get("command") or ""),
                    process=str(item.get("process") or ""),
                    port=port,
                    verify=str(item.get("verify") or "process"),
                    http_path=str(item.get("http_path") or ""),
                    key=str(item.get("key") or "") or None,
                )
            )
            continue
        key = str(item.get("key") or "")
        if key in seen:
            continue
        builtin = _clone_builtin(key)
        if builtin:
            loaded.append(builtin)
            seen.add(key)
    return loaded


def save_sequence() -> str | None:
    payload = []
    for app in APPS:
        if app.get("custom"):
            payload.append(
                {
                    "key": app["key"],
                    "custom": True,
                    "name": app["name"],
                    "command": app.get("command", ""),
                    "process": app["process_label"],
                    "port": app.get("port"),
                    "verify": app["verify"],
                    "http_path": app.get("http_path") or "",
                }
            )
        else:
            payload.append({"key": app["key"]})
    try:
        SEQUENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEQUENCE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return str(exc)
    return None


APPS: list[dict] = load_sequence()


def listening_ports() -> set[int]:
    """TCP/UDP listen ports from `ss`. Empty when ss is unavailable."""
    ports: set[int] = set()
    ss_bin = shutil.which("ss")
    if not ss_bin:
        return ports
    try:
        result = subprocess.run([ss_bin, "-H", "-tuln"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ports
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[4]
        port_text = local.rsplit(":", 1)[-1]
        if port_text.isdigit():
            ports.add(int(port_text))
    return ports


def port_connects(port: int) -> bool:
    targets: list[tuple[int, tuple]] = [(socket.AF_INET, ("127.0.0.1", port))]
    if socket.has_ipv6:
        targets.append((socket.AF_INET6, ("::1", port, 0, 0)))
    for family, address in targets:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.25)
                if sock.connect_ex(address) == 0:
                    return True
        except OSError:
            continue
    return False


def http_ok(port: int, path: str) -> bool:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        response = HTTP.get(url, timeout=HTTP_TIMEOUT_SEC)
    except requests.RequestException:
        return False
    return bool(response.ok)


def process_matches(app: dict) -> bool:
    basenames = set(app["basenames"])
    contains = [token for token in app["cmd_contains"] if token]
    excludes = [token for token in app["cmd_excludes"] if token]
    own_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            info = proc.info
            pid = info.get("pid") or proc.pid
            if pid == own_pid:
                continue
            name = info.get("name") or ""
            exe = info.get("exe") or ""
            cmdline = info.get("cmdline") or []
        except (psutil.Error, OSError):
            continue
        blob = " ".join([name, exe, *[part for part in cmdline if part]])
        if "copper_launcher.py" in blob:
            continue
        if any(token in blob for token in excludes):
            continue
        base = Path(exe or name).name
        if base in basenames or name in basenames:
            return True
        if any(token in blob for token in contains):
            return True
    return False


def inspect_app(app: dict, ports: set[int] | None = None) -> Probe:
    if ports is None:
        ports = listening_ports()
    open_port = None
    candidates = []
    if app.get("port"):
        candidates.append(app["port"])
    candidates.extend(app.get("alt_ports") or [])
    for candidate in candidates:
        if port_connects(candidate) or candidate in ports:
            open_port = candidate
            break
    running = process_matches(app)
    http = False
    if app["verify"] == "http" and app.get("http_path"):
        for candidate in candidates:
            if http_ok(candidate, app["http_path"]):
                http = True
                open_port = candidate
                break
    elif open_port and app.get("http_path"):
        http = http_ok(open_port, app["http_path"])

    bits = ["process running" if running else "no matching process"]
    if app.get("port"):
        if open_port:
            bits.append(f"port {open_port} open")
        else:
            bits.append(f"port {app['port']} closed")
    if app.get("http_path") and (app["verify"] == "http" or http):
        bits.append(f"HTTP {app['http_path']} {'ok' if http else 'not ready'}")
    return Probe(running, open_port is not None, open_port, http, "; ".join(bits))


def detail_line(app: dict, probe: Probe) -> str:
    bits = []
    if probe.open_port:
        bits.append(f"port {probe.open_port}")
    elif app.get("port"):
        bits.append(f"port {app['port']}")
    bits.append(app["process_label"])
    return " · ".join(bits)


def is_verified(app: dict, probe: Probe) -> bool:
    if app["verify"] == "http":
        return probe.http_ok
    if app["verify"] == "process":
        return probe.running
    return probe.running or probe.port_open or probe.http_ok


def gpu_status() -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result and result.returncode == 0 and result.stdout.strip():
        line = result.stdout.strip().splitlines()[0]
        name, _, util = line.rpartition(",")
        util = util.strip() or "0"
        label = f"GPU {util}%" if util.replace(".", "", 1).isdigit() else f"GPU {util}"
        return label, name.strip() or "NVIDIA GPU"
    cards = []
    drm = Path("/sys/class/drm")
    if drm.is_dir():
        cards = sorted(path.name for path in drm.glob("card[0-9]*") if path.is_dir() or path.exists())
        cards = [name for name in cards if name.startswith("card") and "-" not in name]
    if cards:
        return "GPU present", "DRM " + ", ".join(cards)
    return "GPU n/a", "No GPU status available"


def _hwmon_fans() -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    root = Path("/sys/class/hwmon")
    if not root.is_dir():
        return found
    for hw in sorted(root.glob("hwmon*")):
        try:
            chip = (hw / "name").read_text(encoding="utf-8").strip()
        except OSError:
            chip = hw.name
        for fan in sorted(hw.glob("fan*_input")):
            try:
                rpm = int(fan.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            label_file = hw / fan.name.replace("_input", "_label")
            try:
                label = label_file.read_text(encoding="utf-8").strip()
            except OSError:
                label = fan.name.replace("_input", "")
            found.append((chip, label or "fan", rpm))
    return found


def _nvidia_fan() -> tuple[int, str] | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=fan.speed,name", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    speed, _, name = result.stdout.strip().splitlines()[0].partition(",")
    speed = speed.strip()
    if not speed or "N/A" in speed.upper():
        return None
    try:
        percent = int(float(speed))
    except ValueError:
        return None
    return percent, name.strip() or "NVIDIA GPU"


def fan_status() -> tuple[str, str, bool]:
    """CPU fan RPM when the kernel exposes one, otherwise the GPU fan speed."""
    fans = _hwmon_fans()

    def cpuish(item: tuple[str, str, int]) -> bool:
        blob = f"{item[0]} {item[1]}".lower()
        return any(word in blob for word in ("cpu", "processor", "k10", "tctl", "pump"))

    cpu_fans = [item for item in fans if cpuish(item)]
    chosen = cpu_fans[0] if cpu_fans else (max(fans, key=lambda item: item[2]) if fans else None)
    if chosen:
        chip, label, rpm = chosen
        tip = f"{label} · {chip} · {rpm} rpm"
        if len(fans) > 1:
            tip += " · " + ", ".join(f"{lab} {spd} rpm" for _chip, lab, spd in fans)
        return f"Fan {rpm} rpm", tip, rpm > 0
    nvidia = _nvidia_fan()
    if nvidia:
        percent, name = nvidia
        return (
            f"Fan {percent}%",
            f"{name} fan. No CPU fan sensor is exposed under /sys/class/hwmon.",
            percent > 0,
        )
    return "Fan n/a", "No fan sensor under /sys/class/hwmon or nvidia-smi.", False


def telemetry_snapshot(ports: set[int] | None = None) -> dict:
    if ports is None:
        ports = listening_ports()
    cpu = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    gpu_label, gpu_tip = gpu_status()
    fan_label, fan_tip, fan_hot = fan_status()
    pills = []
    for app in APPS:
        if not app.get("port"):
            continue
        hot_port = None
        if port_connects(app["port"]) or app["port"] in ports:
            hot_port = app["port"]
        else:
            for alt in app.get("alt_ports") or []:
                if port_connects(alt) or alt in ports:
                    hot_port = alt
                    break
        if hot_port:
            pills.append(
                {
                    "key": app["key"],
                    "label": f"{app['short']} {hot_port}",
                    "hot": True,
                    "tip": "Accepting local connections",
                }
            )
        else:
            pills.append(
                {
                    "key": app["key"],
                    "label": f"{app['short']} {app['port']}",
                    "hot": False,
                    "tip": "Nothing listening on this port",
                }
            )
    return {
        "cpu": f"CPU {cpu:.0f}%",
        "cpu_tip": f"{psutil.cpu_count() or 0} logical CPUs",
        "ram": f"RAM {memory.percent:.0f}%",
        "ram_tip": f"{memory.used / 1024**3:.1f} / {memory.total / 1024**3:.1f} GB",
        "fan": fan_label,
        "fan_tip": fan_tip,
        "fan_hot": fan_hot,
        "gpu": gpu_label,
        "gpu_tip": gpu_tip,
        "ports": pills,
    }


def service_state(unit: str) -> str:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return "unknown"
    try:
        result = subprocess.run(
            [systemctl, "is-active", unit],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout or result.stderr or "unknown").strip().splitlines()[0]


def command_exists(argv: list[str]) -> bool:
    program = argv[0]
    if os.sep in program:
        path = Path(program)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(program) is not None


def normalize_argv(argv: list[str]) -> list[str]:
    program = argv[0]
    if os.sep not in program:
        found = shutil.which(program)
        if found:
            return [found, *argv[1:]]
    return list(argv)


class LogTail:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.pos = path.stat().st_size
        except OSError:
            self.pos = 0

    def read_new(self) -> str:
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.pos)
                data = handle.read()
                self.pos = handle.tell()
        except OSError:
            return ""
        return data


def prepare_log(app: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{app['key']}.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    return path


def spawn_detached(argv: list[str], log_path: Path) -> tuple[bool, int]:
    """Start a process that outlives this app and appends its output to log_path."""
    argv = normalize_argv(argv)
    script = "exec " + " ".join(shlex.quote(part) for part in argv)
    script += f" >> {shlex.quote(str(log_path))} 2>&1"
    bash = shutil.which("bash") or "/bin/bash"
    result = QProcess.startDetached(bash, ["-c", script], str(Path.home()))
    if isinstance(result, tuple):
        ok = bool(result[0])
        pid = int(result[1]) if len(result) > 1 and result[1] else 0
        return ok, pid
    return bool(result), 0


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

class SystemCheckWorker(QThread):
    log_line = Signal(str)
    telemetry_ready = Signal(object)
    statuses_ready = Signal(object)

    def __init__(self, refresh: bool = False, announce: bool = False):
        super().__init__()
        self.refresh = refresh
        self.announce = announce
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            ports = listening_ports()
            snapshot = telemetry_snapshot(ports)
            rows = []
            if self.announce:
                self.log_line.emit(
                    f"Pre-flight {snapshot['cpu']}, {snapshot['ram']}, {snapshot['gpu']}."
                )
            for index, app in enumerate(APPS):
                if self._cancel:
                    return
                if self.refresh:
                    argv, notes = resolved_command(app, refresh=True)
                    if self.announce:
                        if argv:
                            self.log_line.emit(f"{app['name']} command: {' '.join(argv)}")
                        else:
                            self.log_line.emit(
                                f"{app['name']} executable not found. Searched: {', '.join(notes)}"
                            )
                probe = inspect_app(app, ports)
                status = VERIFIED if is_verified(app, probe) else PENDING
                rows.append((index, status, detail_line(app, probe)))
                if self.announce:
                    self.log_line.emit(f"{app['name']}: {probe.summary}")
            self.telemetry_ready.emit(snapshot)
            self.statuses_ready.emit(rows)
        except Exception as exc:  # keep the UI thread alive
            self.log_line.emit(f"System check failed: {exc}")
            self.telemetry_ready.emit({})
            self.statuses_ready.emit([])


class LaunchWorker(QThread):
    log_line = Signal(str)
    status = Signal(int, str)
    progress = Signal(int, int)
    finished_ok = Signal(bool)

    def __init__(self, indices: list[int]):
        super().__init__()
        self.indices = list(indices)
        self._cancel = False
        self._tail: LogTail | None = None

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total = len(self.indices)
        success = True
        try:
            for step, index in enumerate(self.indices, start=1):
                if self._cancel:
                    success = False
                    break
                self.progress.emit(step - 1, total)
                if not self._run_one(index):
                    success = False
                    if STOP_ON_FAILURE:
                        break
            self.progress.emit(total, total)
        except Exception as exc:
            success = False
            self.log_line.emit(f"Launch sequence failed: {exc}")
        self.finished_ok.emit(success)

    def _run_one(self, index: int) -> bool:
        app = APPS[index]
        self.status.emit(index, CHECKING)
        self.log_line.emit(f"--- {app['name']} ---")
        argv, notes = resolved_command(app, refresh=True)
        if argv:
            self.log_line.emit("Command: " + " ".join(argv))
        else:
            self.log_line.emit("Executable not found. Searched: " + ", ".join(notes))
        probe = inspect_app(app)
        self.log_line.emit(probe.summary)
        if is_verified(app, probe):
            self.status.emit(index, VERIFIED)
            self.log_line.emit(f"{app['name']} already running. Skipping launch.")
            return True
        self.status.emit(index, LAUNCHING)
        if app["key"] == "ollama":
            return self._launch_ollama(app, index, argv)
        if not argv or not command_exists(argv):
            if argv and os.sep in argv[0] and Path(argv[0]).is_file():
                self.log_line.emit(f"{argv[0]} exists but is not executable.")
            self.status.emit(index, FAILED)
            self.log_line.emit(f"{app['name']} could not be launched.")
            return False
        return self._spawn_and_verify(app, index, argv)

    def _launch_ollama(self, app: dict, index: int, argv: list[str] | None) -> bool:
        state = service_state(OLLAMA_SERVICE)
        self.log_line.emit(f"systemd {OLLAMA_SERVICE}.service is {state}.")
        probe = inspect_app(app)
        if probe.running or probe.port_open or state in {"active", "activating"}:
            self.log_line.emit(f"Waiting for HTTP {app['http_path']} on the existing service.")
            if self._wait_until_verified(app, index):
                return True
            self.status.emit(index, FAILED)
            self.log_line.emit("Ollama is present, but /api/tags did not succeed.")
            return False
        try:
            result = subprocess.run(
                ["systemctl", "start", OLLAMA_SERVICE],
                capture_output=True,
                text=True,
                timeout=4,
            )
            message = (result.stderr or result.stdout or "").strip()
            if message:
                self.log_line.emit(message)
            if result.returncode == 0:
                self.log_line.emit(f"Started {OLLAMA_SERVICE}.service.")
                if self._wait_until_verified(app, index):
                    return True
                self.status.emit(index, FAILED)
                self.log_line.emit("ollama.service started, but /api/tags did not succeed.")
                return False
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log_line.emit(f"systemctl start {OLLAMA_SERVICE} did not finish: {exc}")
        if inspect_app(app).running or inspect_app(app).port_open:
            if self._wait_until_verified(app, index):
                return True
            self.status.emit(index, FAILED)
            return False
        if not argv or not command_exists(argv):
            self.status.emit(index, FAILED)
            self.log_line.emit("ollama serve is not available.")
            return False
        self.log_line.emit("Starting detached `ollama serve`.")
        return self._spawn_and_verify(app, index, argv)

    def _spawn_and_verify(self, app: dict, index: int, argv: list[str]) -> bool:
        log_path = prepare_log(app)
        self._tail = LogTail(log_path)
        self.log_line.emit(f"Launching {' '.join(normalize_argv(argv))}")
        self.log_line.emit(f"Logging to {log_path}")
        try:
            ok, pid = spawn_detached(argv, log_path)
        except Exception as exc:
            self.status.emit(index, FAILED)
            self.log_line.emit(f"Failed to detach {app['name']}: {exc}")
            return False
        if not ok:
            self.status.emit(index, FAILED)
            self.log_line.emit(f"QProcess.startDetached returned failure for {app['name']}.")
            return False
        self.log_line.emit(f"Detached pid {pid}.")
        if self._wait_until_verified(app, index):
            return True
        self._drain_log()
        self.status.emit(index, FAILED)
        self.log_line.emit(f"{app['name']} was not verified within {app['timeout']}s.")
        return False

    def _wait_until_verified(self, app: dict, index: int) -> bool:
        deadline = time.monotonic() + app["timeout"]
        while time.monotonic() < deadline:
            if self._cancel:
                return False
            self._drain_log()
            probe = inspect_app(app)
            if is_verified(app, probe):
                self.status.emit(index, VERIFIED)
                self.log_line.emit(f"{app['name']} verified. {probe.summary}")
                return True
            self.msleep(400)
        return False

    def _drain_log(self) -> None:
        if not self._tail:
            return
        text = self._tail.read_new()
        for line in text.splitlines():
            cleaned = line.rstrip()
            if cleaned:
                self.log_line.emit(cleaned)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

STYLESHEET = """
QWidget#root {
    background: #0E1015;
    color: #E7E9F2;
    font-family: Inter, "Noto Sans", sans-serif;
    font-size: 13px;
}
QLabel { background: transparent; color: #E7E9F2; }
QLabel#titleLabel { font-size: 22px; font-weight: 650; letter-spacing: 0.2px; }
QLabel#subtitleLabel, QLabel#cardDetail { color: #9AA3B2; font-size: 12px; }
QLabel#cardTitle { font-size: 15px; font-weight: 600; }
QLabel#deckLabel, QLabel#stepLabel, QLabel#logCaption {
    color: #7A9E7E;
    font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
    font-size: 11px;
    letter-spacing: 1.4px;
}
QFrame#headerCard, QFrame#telemetryCard, QFrame#logFrame, QFrame#appCard, QFrame#deckCard {
    background-color: #161922;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
}
QLabel#pill, QLabel#statusPill, QLabel#headerBadge {
    font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
}
QLabel#pill {
    background-color: #0E1015;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 4px 10px;
    color: #C5C8D4;
    font-size: 11px;
}
QLabel#pill[hot="true"] {
    color: #D5E6D6;
    border: 1px solid #7A9E7E;
    background-color: #141C18;
}
QLabel#pill[tone="patina"] { color: #C9E0CC; border: 1px solid rgba(122, 158, 126, 0.45); }
QLabel#pill[tone="patina"][hot="true"] {
    color: #E7F4E8;
    border: 1px solid #7A9E7E;
    background: #18211C;
}
QLabel#headerBadge {
    border-radius: 8px;
    padding: 4px 10px;
    background: #0E1015;
    color: #C5C8D4;
    border: 1px solid rgba(255, 255, 255, 0.08);
}
QLabel#headerBadge[state="busy"] { color: #E4D9FF; border: 1px solid #9D7BFF; background: #161222; }
QLabel#headerBadge[state="ok"] { color: #D5E6D6; border: 1px solid #7A9E7E; background: #141C18; }
QLabel#headerBadge[state="warn"] { color: #F0C9C9; border: 1px solid #A86464; background: #221416; }
QLabel#statusPill {
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 11px;
    background: #0E1015;
    color: #9AA3B2;
    border: 1px solid rgba(255, 255, 255, 0.08);
}
QLabel#statusPill[state="pending"] { color: #9AA3B2; }
QLabel#statusPill[state="checking"] { color: #E4D9FF; border: 1px solid #9D7BFF; background: #161222; }
QLabel#statusPill[state="launching"] { color: #F4EEFF; border: 1px solid #9D7BFF; background: #241C38; }
QLabel#statusPill[state="verified"] { color: #E7F4E8; border: 1px solid #7A9E7E; background: #18211C; }
QLabel#statusPill[state="failed"] { color: #F0C9C9; border: 1px solid #A86464; background: #221416; }
QPlainTextEdit#logView {
    background: #0E1015;
    color: #D5D8E4;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    padding: 8px;
    selection-background-color: #9D7BFF;
    selection-color: #0E1015;
}
QProgressBar {
    background: #0E1015;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9D7BFF, stop:1 #7A9E7E);
    border-radius: 3px;
}
QLineEdit, QComboBox, QListWidget {
    background: #0E1015;
    color: #E7E9F2;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: #9D7BFF;
    selection-color: #0E1015;
    font-family: Inter, "Noto Sans", sans-serif;
}
QListWidget::item { padding: 6px 8px; border-radius: 6px; }
QListWidget::item:selected { background: #241C38; color: #E7E9F2; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #161922;
    color: #E7E9F2;
    selection-background-color: #241C38;
}
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 0; }
QScrollBar::handle:vertical { background: rgba(157, 123, 255, 0.45); border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""

STATUS_STATE = {
    PENDING: "pending",
    CHECKING: "checking",
    LAUNCHING: "launching",
    VERIFIED: "verified",
    FAILED: "failed",
}


def polish(widget: QWidget, prop: str, value: str) -> None:
    widget.setProperty(prop, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class _Mechanical(QWidget):
    """Shared press travel for the control-deck switches."""

    def _arm(self, down_ms: int, up_ms: int) -> None:
        self._depth = 0.0
        self._hover = False
        self._held = False
        self._down_ms = down_ms
        self._up_ms = up_ms
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set_depth)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def _set_depth(self, value) -> None:
        self._depth = max(0.0, min(1.0, float(value)))
        self.update()

    def _travel_to(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._depth)
        self._anim.setEndValue(target)
        self._anim.setDuration(self._down_ms if target > self._depth else self._up_ms)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        if self._held:
            self._held = False
            self._release_travel()
        self.update()
        super().leaveEvent(event)

    def _release_travel(self) -> None:
        self._travel_to(0.0)

    def _ui_font(self, size: int, bold: bool = True) -> QFont:
        font = QFont("Inter", size)
        font.setBold(bold)
        if not font.exactMatch():
            font = QFont("Noto Sans", size)
            font.setBold(bold)
        return font

    def _mono_font(self, size: int) -> QFont:
        font = QFont("JetBrains Mono", size)
        if not font.exactMatch():
            font = QFont("DejaVu Sans Mono", size)
        return font


class GlowSwitch(_Mechanical):
    """Primary tactile switch with a cycling lavender/sage rim."""

    clicked = Signal()
    _live: list["GlowSwitch"] = []
    _clock: QTimer | None = None

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = text
        self._angle = 0.0
        self._border_key = None
        self._border = None
        self._arm(90, 140)
        self.setMinimumHeight(56)
        hint = self.sizeHint()
        self.setMinimumWidth(max(168, hint.width()))

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:  # noqa: N802
        width = self.fontMetrics().horizontalAdvance(self._text) + 56
        return QSize(max(168, width), 56)

    def showEvent(self, event) -> None:  # noqa: N802
        if self not in GlowSwitch._live:
            GlowSwitch._live.append(self)
        if GlowSwitch._clock is None:
            GlowSwitch._clock = QTimer()
            GlowSwitch._clock.timeout.connect(GlowSwitch._spin)
            GlowSwitch._clock.start(32)
        elif not GlowSwitch._clock.isActive():
            GlowSwitch._clock.start(32)
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        if self in GlowSwitch._live:
            GlowSwitch._live.remove(self)
        if not GlowSwitch._live and GlowSwitch._clock is not None:
            GlowSwitch._clock.stop()
        super().hideEvent(event)

    @staticmethod
    def _spin() -> None:
        for switch in list(GlowSwitch._live):
            switch._angle = (switch._angle + 2.2) % 360.0
            switch.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._held = True
            self._travel_to(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._held and event.button() == Qt.MouseButton.LeftButton:
            self._held = False
            self._travel_to(0.0)
            if self.isEnabled() and self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self.isEnabled() and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            if not self._held:
                self._held = True
                self._travel_to(1.0)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if self._held and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self._held = False
            self._travel_to(0.0)
            if self.isEnabled():
                self.clicked.emit()
            return
        super().keyReleaseEvent(event)

    def _ring(self, rect: QRectF) -> QPainterPath:
        key = (round(rect.width()), round(rect.height()))
        if key != self._border_key:
            local = QPainterPath()
            local.addRoundedRect(QRectF(0, 0, rect.width(), rect.height()), 11, 11)
            stroker = QPainterPathStroker()
            stroker.setWidth(1.6)
            self._border = stroker.createStroke(local)
            self._border_key = key
        return self._border

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        depth = self._depth
        lip_h = 4.0 - 3.0 * depth
        travel = 3.0 * depth
        body = QRectF(8, 6, self.width() - 16, self.height() - 12)
        cap = QRectF(body.x(), body.y() + travel, body.width(), body.height() - 4)
        lip = QRectF(body.x() + 2, body.bottom() - lip_h, body.width() - 4, lip_h)
        enabled = self.isEnabled()
        if enabled and (self._hover or depth > 0.05):
            glow = QColor("#9D7BFF")
            glow.setAlpha(64 if depth > 0.4 else 36 if self._hover else 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawRoundedRect(body.adjusted(-3, -2, 3, 2), 14, 14)
            sage = QColor("#7A9E7E")
            sage.setAlpha(28 if self._hover else 10)
            painter.setBrush(sage)
            painter.drawRoundedRect(body.adjusted(-6, -4, 6, 4), 16, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#08090C"))
        painter.drawRoundedRect(lip, 5, 5)
        face = QPainterPath()
        face.addRoundedRect(cap, 11, 11)
        painter.setClipPath(face)
        metal = QLinearGradient(cap.topLeft(), cap.bottomLeft())
        if enabled:
            metal.setColorAt(0.0, QColor("#32384A"))
            metal.setColorAt(0.16, QColor("#1C2130"))
            metal.setColorAt(0.55, QColor("#141822"))
            metal.setColorAt(1.0, QColor("#10131B"))
        else:
            metal.setColorAt(0.0, QColor("#23262E"))
            metal.setColorAt(1.0, QColor("#15181F"))
        painter.fillRect(cap, metal)
        if enabled:
            painter.setPen(QPen(QColor(255, 255, 255, 16), 1))
            streak = cap.top() + 5
            while streak < cap.bottom() - 3:
                painter.drawLine(QPoint(int(cap.left() + 10), int(streak)), QPoint(int(cap.right() - 10), int(streak)))
                streak += 3
        painter.setClipping(False)
        painter.setPen(QPen(QColor(255, 255, 255, 51 if enabled else 18), 1))
        painter.drawLine(
            QPoint(int(cap.left() + 12), int(cap.top() + 1.4)),
            QPoint(int(cap.right() - 12), int(cap.top() + 1.4)),
        )
        if enabled:
            cone = QConicalGradient(cap.center(), -self._angle)
            cone.setColorAt(0.0, QColor("#9D7BFF"))
            cone.setColorAt(0.5, QColor("#7A9E7E"))
            cone.setColorAt(1.0, QColor("#9D7BFF"))
            painter.save()
            painter.translate(cap.topLeft())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(cone)
            painter.drawPath(self._ring(cap))
            painter.restore()
        else:
            painter.setPen(QPen(QColor(255, 255, 255, 28), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(cap, 11, 11)
        painter.setPen(QColor("#E7E9F2") if enabled else QColor("#8E93A3"))
        painter.setFont(self._ui_font(11))
        painter.drawText(cap, Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()


class DetentToggle(_Mechanical):
    """Latching rocker with an embedded LED. ON stays slightly depressed."""

    toggled = Signal(bool)

    def __init__(self, label: str, checked: bool = False, led: str = "sage", parent: QWidget | None = None):
        super().__init__(parent)
        self._label = label
        self._on = bool(checked)
        self._led = led
        self._arm(70, 120)
        self._depth = 1.0 if self._on else 0.0
        self.setMinimumSize(150, 52)
        self.setMinimumWidth(150)

    def text(self) -> str:
        return self._label

    def isChecked(self) -> bool:  # noqa: N802
        return self._on

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        checked = bool(checked)
        if checked == self._on:
            self._travel_to(1.0 if checked else 0.0)
            return
        self._on = checked
        self._travel_to(1.0 if checked else 0.0)
        self.toggled.emit(self._on)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(max(150, self.fontMetrics().horizontalAdvance(self._label) + 64), 52)

    def _release_travel(self) -> None:
        self._travel_to(1.0 if self._on else 0.0)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._held = True
            self._travel_to(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._held and event.button() == Qt.MouseButton.LeftButton:
            self._held = False
            inside = self.rect().contains(event.position().toPoint())
            if self.isEnabled() and inside:
                self.setChecked(not self._on)
            else:
                self._release_travel()
        super().mouseReleaseEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if self.isEnabled() and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.setChecked(not self._on)
            return
        super().keyReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(2, 6, self.width() - 4, self.height() - 10)
        painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
        painter.setBrush(QColor("#08090C"))
        painter.drawRoundedRect(track, 12, 12)
        painter.setPen(QPen(QColor(0, 0, 0, 140), 2))
        painter.drawLine(
            QPoint(int(track.left() + 10), int(track.top() + 2)),
            QPoint(int(track.right() - 10), int(track.top() + 2)),
        )
        depth = self._depth
        cap = track.adjusted(5, 5 + 2.5 * depth, -5, -7 + 1.2 * depth)
        if self._on and self.isEnabled():
            bloom = QColor("#7A9E7E" if self._led == "sage" else "#9D7BFF")
            bloom.setAlpha(70)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bloom)
            painter.drawRoundedRect(cap.adjusted(-2, -2, 2, 2), 10, 10)
        bevel = 2.0 * (1.0 - depth)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#08090C"))
        painter.drawRoundedRect(QRectF(cap.x(), cap.bottom() - bevel, cap.width(), bevel + 1), 4, 4)
        face = QLinearGradient(cap.topLeft(), cap.bottomLeft())
        face.setColorAt(0.0, QColor("#2A3040") if self.isEnabled() else QColor("#23262E"))
        face.setColorAt(1.0, QColor("#161A24"))
        painter.setBrush(face)
        painter.drawRoundedRect(cap, 9, 9)
        painter.setPen(QPen(QColor(255, 255, 255, 46 if self.isEnabled() else 16), 1))
        painter.drawLine(
            QPoint(int(cap.left() + 8), int(cap.top() + 1)),
            QPoint(int(cap.right() - 8), int(cap.top() + 1)),
        )
        led = QRectF(cap.left() + 8, cap.center().y() - 3.5, 16, 7)
        if self._on and self.isEnabled():
            color = QColor("#B7D7BA" if self._led == "sage" else "#D4C6FF")
            painter.setPen(Qt.PenStyle.NoPen)
            halo = QColor(color)
            halo.setAlpha(90)
            painter.setBrush(halo)
            painter.drawEllipse(led.adjusted(-4, -4, 4, 4))
            painter.setBrush(color)
            painter.drawRoundedRect(led, 3, 3)
        else:
            painter.setBrush(QColor("#2A2E38"))
            painter.setPen(QPen(QColor(255, 255, 255, 24), 1))
            painter.drawRoundedRect(led, 3, 3)
        painter.setPen(QColor("#E7E9F2") if self.isEnabled() else QColor("#8E93A3"))
        painter.setFont(self._ui_font(9))
        label_rect = QRectF(led.right() + 6, cap.top(), cap.right() - led.right() - 12, cap.height() * 0.62)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)
        painter.setPen(QColor("#7A9E7E") if self._on else QColor("#6E7484"))
        painter.setFont(self._mono_font(8))
        state_rect = QRectF(led.right() + 6, cap.center().y(), cap.right() - led.right() - 12, cap.height() * 0.42)
        painter.drawText(state_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "ON" if self._on else "OFF")
        painter.end()


class UtilityKeycap(_Mechanical):
    """Compact mechanical keycap for secondary actions and shortcuts."""

    clicked = Signal()

    def __init__(self, label: str, shortcut: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._label = label
        self._shortcut = shortcut
        self._arm(50, 80)
        self.setMinimumHeight(46)
        self.setMinimumWidth(self.sizeHint().width())

    def text(self) -> str:
        return self._label

    def sizeHint(self) -> QSize:  # noqa: N802
        width = self.fontMetrics().horizontalAdvance(self._label) + (28 if self._shortcut else 24)
        return QSize(max(58, width), 46)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._held = True
            self._travel_to(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._held and event.button() == Qt.MouseButton.LeftButton:
            self._held = False
            self._travel_to(0.0)
            if self.isEnabled() and self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self.isEnabled() and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            if not self._held:
                self._held = True
                self._travel_to(1.0)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if self._held and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self._held = False
            self._travel_to(0.0)
            if self.isEnabled():
                self.clicked.emit()
            return
        super().keyReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        depth = self._depth
        bevel = 2.0 * (1.0 - depth)
        body = QRectF(2, 2, self.width() - 4, self.height() - 4)
        cap = QRectF(body.x(), body.y() + 2.0 * depth, body.width(), body.height() - 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#08090C"))
        painter.drawRoundedRect(QRectF(cap.x(), body.bottom() - bevel, cap.width(), bevel + 1), 3, 3)
        face = QLinearGradient(cap.topLeft(), cap.bottomLeft())
        if self.isEnabled():
            face.setColorAt(0.0, QColor("#262B38") if self._hover else QColor("#222733"))
            face.setColorAt(1.0, QColor("#151922"))
        else:
            face.setColorAt(0.0, QColor("#1C2028"))
            face.setColorAt(1.0, QColor("#14171E"))
        painter.setBrush(face)
        painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
        painter.drawRoundedRect(cap, 8, 8)
        painter.setPen(QPen(QColor(255, 255, 255, 48 if self.isEnabled() else 16), 1))
        painter.drawLine(
            QPoint(int(cap.left() + 7), int(cap.top() + 1)),
            QPoint(int(cap.right() - 7), int(cap.top() + 1)),
        )
        painter.setPen(QColor("#E7E9F2") if self.isEnabled() else QColor("#8E93A3"))
        painter.setFont(self._ui_font(9))
        painter.drawText(cap.adjusted(0, -2 if self._shortcut else 0, -8 if self._shortcut else 0, 0), Qt.AlignmentFlag.AlignCenter, self._label)
        if self._shortcut:
            painter.setPen(QColor("#9D7BFF") if self.isEnabled() else QColor("#6E7484"))
            painter.setFont(self._mono_font(7))
            painter.drawText(cap.adjusted(0, 0, -6, -3), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, self._shortcut)
        painter.end()


class SequenceDialog(QDialog):
    """Add, remove, and reorder the programs launched in sequence."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Launch sequence")
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLESHEET)
        self.resize(680, 720)
        self.items = [copy.deepcopy(app) for app in APPS]

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        title = QLabel("Launch sequence")
        title.setObjectName("titleLabel")
        hint = QLabel("Add a built-in program, drop one, or type your own command.")
        hint.setObjectName("subtitleLabel")
        root.addWidget(title)
        root.addWidget(hint)

        self.list = QListWidget()
        self.list.setMinimumHeight(180)
        root.addWidget(self.list, stretch=1)

        order_row = QHBoxLayout()
        self.up_key = UtilityKeycap("Up", "↑")
        self.down_key = UtilityKeycap("Down", "↓")
        self.remove_key = UtilityKeycap("Remove")
        self.up_key.clicked.connect(lambda: self._move(-1))
        self.down_key.clicked.connect(lambda: self._move(1))
        self.remove_key.clicked.connect(self._remove_current)
        order_row.addWidget(self.up_key)
        order_row.addWidget(self.down_key)
        order_row.addWidget(self.remove_key)
        order_row.addStretch(1)
        root.addLayout(order_row)

        builtin_row = QHBoxLayout()
        self.catalog = QComboBox()
        self.add_builtin = UtilityKeycap("Add", "+")
        self.add_builtin.clicked.connect(self._add_builtin)
        builtin_row.addWidget(self.catalog, stretch=1)
        builtin_row.addWidget(self.add_builtin)
        root.addLayout(builtin_row)

        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Notes")
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("gedit /home/livingroom/notes.txt")
        self.process_edit = QLineEdit()
        self.process_edit.setPlaceholderText("process name, optional")
        self.port_edit = QLineEdit()
        self.port_edit.setPlaceholderText("optional port")
        self.http_edit = QLineEdit()
        self.http_edit.setPlaceholderText("/  or  /api/tags")
        self.verify_box = QComboBox()
        self.verify_box.addItem("Process is running", "process")
        self.verify_box.addItem("Process or port", "process_or_port")
        self.verify_box.addItem("HTTP check", "http")
        form.addRow("Name", self.name_edit)
        form.addRow("Command", self.command_edit)
        form.addRow("Process", self.process_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("HTTP path", self.http_edit)
        form.addRow("Verify", self.verify_box)
        root.addLayout(form)

        self.form_note = QLabel("")
        self.form_note.setObjectName("subtitleLabel")
        root.addWidget(self.form_note)

        action_row = QHBoxLayout()
        self.add_custom = UtilityKeycap("Add program")
        self.restore_key = UtilityKeycap("Defaults")
        self.cancel_key = UtilityKeycap("Cancel")
        self.save_key = GlowSwitch("Save sequence")
        self.add_custom.clicked.connect(self._add_custom)
        self.restore_key.clicked.connect(self._restore)
        self.cancel_key.clicked.connect(self.reject)
        self.save_key.clicked.connect(self._save)
        action_row.addWidget(self.add_custom)
        action_row.addWidget(self.restore_key)
        action_row.addStretch(1)
        action_row.addWidget(self.cancel_key)
        action_row.addWidget(self.save_key)
        root.addLayout(action_row)
        self._reload(0)

    def _reload(self, select: int) -> None:
        self.list.clear()
        for index, app in enumerate(self.items, start=1):
            kind = "custom" if app.get("custom") else "built-in"
            port = f" · port {app['port']}" if app.get("port") else ""
            self.list.addItem(f"{index}. {app['name']}{port}  ({kind})")
        if self.items:
            self.list.setCurrentRow(max(0, min(select, len(self.items) - 1)))
        self._refill_catalog()

    def _refill_catalog(self) -> None:
        self.catalog.clear()
        present = {app["key"] for app in self.items}
        for app in DEFAULT_APPS:
            if app["key"] not in present:
                self.catalog.addItem(app["name"], app["key"])
        self.add_builtin.setEnabled(self.catalog.count() > 0)

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self.items):
            return
        self.items[row], self.items[target] = self.items[target], self.items[row]
        self._reload(target)

    def _remove_current(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        del self.items[row]
        self._reload(row)

    def _add_builtin(self) -> None:
        key = self.catalog.currentData()
        app = _clone_builtin(str(key)) if key else None
        if not app:
            return
        self.items.append(app)
        self._reload(len(self.items) - 1)

    def _add_custom(self) -> None:
        name = self.name_edit.text().strip()
        command = self.command_edit.text().strip()
        verify = str(self.verify_box.currentData() or "process")
        port_text = self.port_edit.text().strip()
        port = None
        if port_text:
            if not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
                self.form_note.setText("Port needs to be a number from 1 to 65535.")
                return
            port = int(port_text)
        if not name or not command:
            self.form_note.setText("Name and command are both required.")
            return
        if verify != "process" and port is None:
            self.form_note.setText("Process-or-port and HTTP checks need a port.")
            return
        self.items.append(
            make_custom_app(
                name=name,
                command=command,
                process=self.process_edit.text().strip(),
                port=port,
                verify=verify,
                http_path=self.http_edit.text().strip(),
            )
        )
        self.name_edit.clear()
        self.command_edit.clear()
        self.process_edit.clear()
        self.port_edit.clear()
        self.http_edit.clear()
        self.form_note.setText(f"Added {name}. Save the sequence to keep it.")
        self._reload(len(self.items) - 1)

    def _restore(self) -> None:
        self.items = [copy.deepcopy(app) for app in DEFAULT_APPS]
        self.form_note.setText("Defaults are back in the list. Save to apply them.")
        self._reload(0)

    def _save(self) -> None:
        APPS[:] = self.items
        _RESOLVED.clear()
        error = save_sequence()
        if error:
            self.form_note.setText(f"Could not save the sequence: {error}")
            return
        self.accept()


class Sparkle(QWidget):
    """Gemini-style sparkle. Draws the shipped SVG, then a vector fallback."""

    def __init__(self, icon_path: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(36, 36)
        self._icon = QIcon(str(icon_path)) if icon_path.is_file() else QIcon()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#9D7BFF"))
        painter.drawPolygon(
            QPolygon(
                [
                    QPoint(18, 3),
                    QPoint(22, 14),
                    QPoint(33, 18),
                    QPoint(22, 22),
                    QPoint(18, 33),
                    QPoint(14, 22),
                    QPoint(3, 18),
                    QPoint(14, 14),
                ]
            )
        )
        painter.setBrush(QColor("#7A9E7E"))
        painter.drawPolygon(
            QPolygon(
                [
                    QPoint(28, 5),
                    QPoint(29, 9),
                    QPoint(33, 10),
                    QPoint(29, 11),
                    QPoint(28, 15),
                    QPoint(27, 11),
                    QPoint(23, 10),
                    QPoint(27, 9),
                ]
            )
        )
        painter.end()


class Monogram(QWidget):
    def __init__(self, letters: str, patina: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.letters = letters
        self._fill = QColor("#7A9E7E" if patina else "#9D7BFF")
        self._ink = QColor("#0E1015")
        self.setFixedSize(40, 40)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._fill)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 12, 12)
        painter.setPen(self._ink)
        font = QFont("Ubuntu", 9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.letters)
        painter.end()


class AppCard(QFrame):
    launch_clicked = Signal(int)
    remove_clicked = Signal(int)

    def __init__(self, index: int, app: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.index = index
        self.app = app
        self.status_text = PENDING
        self.setObjectName("appCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(84)
        patina = index % 2 == 1
        polish(self, "patina", "true" if patina else "false")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(12)
        layout.addWidget(Monogram(app["mark"], patina=patina))

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title = QLabel(app["name"])
        title.setObjectName("cardTitle")
        self.detail_lbl = QLabel(app["detail"])
        self.detail_lbl.setObjectName("cardDetail")
        text_col.addWidget(title)
        text_col.addWidget(self.detail_lbl)
        layout.addLayout(text_col, stretch=1)

        self.status_lbl = QLabel(PENDING)
        self.status_lbl.setObjectName("statusPill")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_lbl.setMinimumWidth(148)
        self.status_lbl.setProperty("state", "pending")
        layout.addWidget(self.status_lbl)

        self.launch_button = UtilityKeycap("Launch", "⏎")
        self.launch_button.clicked.connect(lambda: self.launch_clicked.emit(self.index))
        layout.addWidget(self.launch_button)

        self.remove_button = UtilityKeycap("Remove", "×")
        self.remove_button.clicked.connect(lambda: self.remove_clicked.emit(self.index))
        layout.addWidget(self.remove_button)

    def set_detail(self, text: str) -> None:
        self.detail_lbl.setText(text)

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.status_lbl.setText(text)
        polish(self.status_lbl, "state", STATUS_STATE.get(text, "pending"))


class CopperLauncher(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Copper Launcher")
        icon_path = Path(__file__).with_name("copper-launcher.svg")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setMinimumSize(1100, 900)
        self.resize(1280, 1180)
        self.setStyleSheet(STYLESHEET)

        self._check: SystemCheckWorker | None = None
        self._launch: LaunchWorker | None = None
        self._closing = False
        self._busy = False
        self.cards: list[AppCard] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("headerCard")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(12)
        header_layout.addWidget(Sparkle(icon_path))

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Copper Launcher")
        title.setObjectName("titleLabel")
        subtitle = QLabel("Post-startup check and sequential launch")
        subtitle.setObjectName("subtitleLabel")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)

        self.badge = QLabel("Ready")
        self.badge.setObjectName("headerBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setMinimumWidth(120)
        header_layout.addWidget(self.badge)
        root.addWidget(header)

        deck = QFrame()
        deck.setObjectName("deckCard")
        deck.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        deck_layout = QVBoxLayout(deck)
        deck_layout.setContentsMargins(14, 12, 14, 14)
        deck_layout.setSpacing(10)
        deck_label = QLabel("CONTROL DECK")
        deck_label.setObjectName("deckLabel")
        deck_layout.addWidget(deck_label)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(8)
        self.recheck_button = UtilityKeycap("Re-check", "R")
        self.recheck_button.clicked.connect(lambda: self.recheck(refresh=True, announce=True))
        self.sequence_button = UtilityKeycap("Sequence", "S")
        self.sequence_button.clicked.connect(self.edit_sequence)
        tool_row.addWidget(self.recheck_button)
        tool_row.addWidget(self.sequence_button)
        tool_row.addStretch(1)
        self.telemetry_toggle = DetentToggle("Telemetry", checked=True, led="sage")
        self.halt_toggle = DetentToggle("Halt on fail", checked=STOP_ON_FAILURE, led="violet")
        self.log_latch = DetentToggle("Terminal", checked=True, led="sage")
        self.telemetry_toggle.toggled.connect(self._on_telemetry_latch)
        self.halt_toggle.toggled.connect(self._on_halt_latch)
        self.log_latch.toggled.connect(self._on_log_latch)
        tool_row.addWidget(self.telemetry_toggle)
        tool_row.addWidget(self.halt_toggle)
        tool_row.addWidget(self.log_latch)
        deck_layout.addLayout(tool_row)

        self.launch_button = GlowSwitch("Launch Sequence")
        self.launch_button.clicked.connect(self.start_sequence)
        deck_layout.addWidget(self.launch_button)
        root.addWidget(deck)

        telemetry = QFrame()
        telemetry.setObjectName("telemetryCard")
        telemetry.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        telemetry_row = QHBoxLayout(telemetry)
        telemetry_row.setContentsMargins(12, 8, 12, 8)
        telemetry_row.setSpacing(8)
        self.cpu_pill = self._make_pill("CPU —")
        self.fan_pill = self._make_pill("Fan —")
        polish(self.fan_pill, "tone", "patina")
        self.ram_pill = self._make_pill("RAM —")
        self.gpu_pill = self._make_pill("GPU —")
        telemetry_row.addWidget(self.cpu_pill)
        telemetry_row.addWidget(self.fan_pill)
        telemetry_row.addWidget(self.ram_pill)
        telemetry_row.addWidget(self.gpu_pill)
        self.port_pills: dict[str, QLabel] = {}
        self._telemetry_layout = telemetry_row
        telemetry_row.addStretch(1)
        self._rebuild_port_pills()
        root.addWidget(telemetry)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self.step_label = QLabel("Idle")
        self.step_label.setObjectName("stepLabel")
        self.progress = QProgressBar()
        self.progress.setRange(0, len(APPS))
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        progress_row.addWidget(self.step_label)
        progress_row.addWidget(self.progress, stretch=1)
        root.addLayout(progress_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("cardHost")
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 2, 0)
        host_layout.setSpacing(10)
        self._host_layout = host_layout
        self._rebuild_cards()
        scroll.setWidget(host)
        scroll.viewport().setAutoFillBackground(False)
        self.scroll = scroll
        root.addWidget(scroll, stretch=1)

        log_frame = QFrame()
        log_frame.setObjectName("logFrame")
        log_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(12, 8, 12, 10)
        log_layout.setSpacing(6)
        log_caption = QLabel("TERMINAL")
        log_caption.setObjectName("logCaption")
        log_layout.addWidget(log_caption)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(112)
        self.log_view.setMaximumBlockCount(1500)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        log_layout.addWidget(self.log_view)
        root.addWidget(log_frame)

        self._timer = QTimer(self)
        self._timer.setInterval(TELEMETRY_INTERVAL_MS)
        self._timer.timeout.connect(lambda: self.recheck(refresh=False, announce=False))
        self._timer.start()

    def _on_telemetry_latch(self, engaged: bool) -> None:
        if engaged:
            if not self._timer.isActive():
                self._timer.start()
            self.recheck(refresh=False, announce=False)
        else:
            self._timer.stop()

    def _on_halt_latch(self, engaged: bool) -> None:
        global STOP_ON_FAILURE
        STOP_ON_FAILURE = engaged
        self.append_log("Halt on failure is " + ("engaged." if engaged else "released."))

    def _on_log_latch(self, engaged: bool) -> None:
        self.log_view.setVisible(engaged)

    def _make_pill(self, text: str) -> QLabel:
        pill = QLabel(text)
        pill.setObjectName("pill")
        pill.setProperty("hot", "false")
        return pill

    def append_log(self, message: str) -> None:
        if self._closing:
            return
        stamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {message}")

    def set_badge(self, text: str, state: str) -> None:
        if self._closing:
            return
        self.badge.setText(text)
        polish(self.badge, "state", state)

    def recheck(self, refresh: bool = False, announce: bool = False) -> None:
        if self._closing or (self._check and self._check.isRunning()):
            return
        if announce:
            self.set_badge("Checking...", "busy")
            self.step_label.setText("Reading ports and processes")
        worker = SystemCheckWorker(refresh=refresh, announce=announce)
        worker.setParent(self)
        worker.log_line.connect(self.append_log)
        worker.telemetry_ready.connect(self.apply_telemetry)
        worker.statuses_ready.connect(lambda rows, forced=announce: self.apply_statuses(rows, forced))
        worker.start()
        self._check = worker

    def apply_telemetry(self, snapshot: dict) -> None:
        if self._closing or not snapshot:
            return
        self.cpu_pill.setText(snapshot["cpu"])
        self.cpu_pill.setToolTip(snapshot["cpu_tip"])
        self.ram_pill.setText(snapshot["ram"])
        self.ram_pill.setToolTip(snapshot["ram_tip"])
        self.fan_pill.setText(snapshot.get("fan", "Fan n/a"))
        self.fan_pill.setToolTip(snapshot.get("fan_tip", ""))
        polish(self.fan_pill, "hot", "true" if snapshot.get("fan_hot") else "false")
        self.gpu_pill.setText(snapshot["gpu"])
        self.gpu_pill.setToolTip(snapshot["gpu_tip"])
        polish(self.gpu_pill, "hot", "true" if snapshot["gpu"].endswith("%") else "false")
        by_key = {item["key"]: item for item in snapshot.get("ports") or []}
        for key, pill in self.port_pills.items():
            item = by_key.get(key)
            if not item:
                continue
            pill.setText(item["label"])
            pill.setToolTip(item["tip"])
            polish(pill, "hot", "true" if item["hot"] else "false")
        if not self._busy:
            self._refresh_badge()

    def apply_statuses(self, rows: list, force: bool) -> None:
        if self._closing or self._busy:
            return
        for row in rows:
            index, status = row[0], row[1]
            if not (0 <= index < len(self.cards)):
                continue
            detail = row[2] if len(row) > 2 else None
            if detail:
                self.cards[index].set_detail(detail)
            current = self.cards[index].status_text
            if not force and current in {CHECKING, LAUNCHING, FAILED}:
                continue
            self.cards[index].set_status(status)
        verified = sum(card.status_text == VERIFIED for card in self.cards)
        if not self._busy:
            self.progress.setRange(0, max(len(self.cards), 1))
            self.progress.setValue(verified)
            self.step_label.setText(f"{verified} / {len(self.cards)} verified" if self.cards else "Idle")
        self._refresh_badge()
        if force:
            self.append_log("Pre-flight check finished.")

    def _refresh_badge(self) -> None:
        if self._busy:
            return
        texts = [card.status_text for card in self.cards]
        verified = sum(text == VERIFIED for text in texts)
        if texts and verified == len(texts):
            self.set_badge("All running", "ok")
        elif any(text == FAILED for text in texts):
            self.set_badge("Needs attention", "warn")
        elif any(text == CHECKING for text in texts):
            self.set_badge("Checking...", "busy")
        elif verified:
            self.set_badge(f"{verified} running", "ok")
        else:
            self.set_badge("Ready", "ready")

    def start_sequence(self) -> None:
        if not APPS:
            self.append_log("The launch sequence is empty. Use Sequence to add a program.")
            return
        self._start_launch(list(range(len(APPS))), "Launch sequence")

    def launch_one(self, index: int) -> None:
        if not (0 <= index < len(APPS)):
            return
        self._start_launch([index], APPS[index]["name"])

    def remove_app(self, index: int) -> None:
        if self._busy:
            self.append_log("Wait for the launch to finish.")
            return
        if self._check and self._check.isRunning():
            self._check.cancel()
            self._check.wait(1500)
        if not (0 <= index < len(APPS)):
            return
        removed = APPS.pop(index)
        _RESOLVED.pop(removed["key"], None)
        error = save_sequence()
        self.rebuild_pipeline()
        if error:
            self.append_log(f"Removed {removed['name']}, but the sequence file was not saved: {error}")
        else:
            self.append_log(f"Removed {removed['name']} from the sequence.")
        self.recheck(refresh=True, announce=False)

    def edit_sequence(self) -> None:
        if self._busy:
            self.append_log("Finish the launch before editing the sequence.")
            return
        if self._check and self._check.isRunning():
            self._check.cancel()
            self._check.wait(1500)
        dialog = SequenceDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.rebuild_pipeline()
        self.append_log("Saved the launch sequence.")
        self.recheck(refresh=True, announce=True)

    def rebuild_pipeline(self) -> None:
        self._rebuild_port_pills()
        self._rebuild_cards()
        self.progress.setRange(0, max(len(APPS), 1))
        self.progress.setValue(0)
        self._refresh_badge()

    def _rebuild_port_pills(self) -> None:
        for pill in self.port_pills.values():
            self._telemetry_layout.removeWidget(pill)
            pill.deleteLater()
        self.port_pills.clear()
        stretch_index = self._telemetry_layout.count() - 1
        insert_at = stretch_index
        for app in APPS:
            if not app.get("port"):
                continue
            pill = self._make_pill(f"{app['short']} {app['port']}")
            self.port_pills[app["key"]] = pill
            self._telemetry_layout.insertWidget(insert_at, pill)
            insert_at += 1

    def _rebuild_cards(self) -> None:
        while self._host_layout.count():
            item = self._host_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards.clear()
        if not APPS:
            empty = QLabel("The sequence is empty. Press Sequence to add a program.")
            empty.setObjectName("subtitleLabel")
            self._host_layout.addWidget(empty)
        for index, app in enumerate(APPS):
            card = AppCard(index, app)
            card.launch_clicked.connect(self.launch_one)
            card.remove_clicked.connect(self.remove_app)
            self.cards.append(card)
            self._host_layout.addWidget(card)
        self._host_layout.addStretch(1)
        if not self._busy:
            self._set_controls_enabled(True)

    def _start_launch(self, indices: list[int], label: str) -> None:
        if self._closing or self._busy:
            self.append_log("A launch is already in progress.")
            return
        self._busy = True
        self._set_controls_enabled(False)
        self.progress.setRange(0, len(indices))
        self.progress.setValue(0)
        self.step_label.setText(label)
        self.set_badge("Launching", "busy")
        self.append_log(f"Starting {label}.")
        worker = LaunchWorker(indices)
        worker.setParent(self)
        worker.log_line.connect(self.append_log)
        worker.status.connect(self._on_launch_status)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_launch_thread_done)
        worker.start()
        self._launch = worker
        if indices:
            self.scroll.ensureWidgetVisible(self.cards[indices[0]])

    def _on_progress(self, completed: int, total: int) -> None:
        if self._closing:
            return
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(completed)
        self.step_label.setText(f"{completed} / {total} complete")

    def _on_launch_status(self, index: int, status: str) -> None:
        if self._closing or index < 0:
            return
        self.cards[index].set_status(status)
        self.scroll.ensureWidgetVisible(self.cards[index])
        if status in {CHECKING, LAUNCHING}:
            self.step_label.setText(f"{APPS[index]['name']} · {status}")

    def _on_launch_thread_done(self) -> None:
        if self._closing:
            return
        self._busy = False
        self._set_controls_enabled(True)
        failed = [card.app["name"] for card in self.cards if card.status_text == FAILED]
        if failed:
            self.set_badge("Needs attention", "warn")
            self.append_log("Finished with failures: " + ", ".join(failed) + ".")
        else:
            targeted = self._launch.indices if self._launch else []
            all_targeted_ok = all(self.cards[i].status_text == VERIFIED for i in targeted)
            if all_targeted_ok and all(card.status_text == VERIFIED for card in self.cards):
                self.set_badge("All running", "ok")
            elif all_targeted_ok:
                self.set_badge("Ready", "ok")
            else:
                self.set_badge("Needs attention", "warn")
            self.append_log("Launch finished.")
        verified = sum(card.status_text == VERIFIED for card in self.cards)
        self.step_label.setText(f"{verified} / {len(self.cards)} verified")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.launch_button.setEnabled(enabled)
        self.recheck_button.setEnabled(enabled)
        self.sequence_button.setEnabled(enabled)
        self.telemetry_toggle.setEnabled(enabled)
        self.halt_toggle.setEnabled(enabled)
        self.log_latch.setEnabled(enabled)
        for card in self.cards:
            card.launch_button.setEnabled(enabled)
            card.remove_button.setEnabled(enabled)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self._timer.stop()
        for worker in (self._check, self._launch):
            if worker and worker.isRunning():
                worker.cancel()
                worker.wait(4000)
        event.accept()


def _apply_palette(app: QApplication) -> None:
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0E1015"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#E7E9F2"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0E1015"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#E7E9F2"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#161922"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#E7E9F2"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#9D7BFF"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#0E1015"))
    app.setPalette(palette)


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    QApplication.setApplicationName("Copper Launcher")
    QApplication.setDesktopFileName("copper-launcher")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    families = set()
    font_dir = Path(__file__).with_name("fonts")
    if font_dir.is_dir():
        for face in font_dir.glob("*.ttf"):
            QFontDatabase.addApplicationFont(str(face))
    families = set(QFontDatabase.families())
    ui_family = "Inter" if "Inter" in families else "Noto Sans"
    mono_family = "JetBrains Mono" if "JetBrains Mono" in families else "DejaVu Sans Mono"
    app.setFont(QFont(ui_family, 11))
    _apply_palette(app)
    window = CopperLauncher()
    window.show()
    auto = AUTO_START_FLAG in sys.argv
    QTimer.singleShot(250, lambda: window.recheck(refresh=True, announce=not auto))
    if auto:
        window.append_log("Auto-start is on. Launch sequence begins after the window is up.")
        QTimer.singleShot(700, window.start_sequence)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
