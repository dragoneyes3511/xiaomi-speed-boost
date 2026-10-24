"""Read-only phone telemetry and explicit, non-root game launching.

Everything here works with Python's standard library. Android may deny telemetry
to Termux; missing readings stay unknown instead of pretending to be zero.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import subprocess


PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_ANDROID_ERRORS = re.compile(
    r"(?:\berror\b|exception|permission denial|permission denied|securityexception|"
    r"unable to resolve|no activity found|does not exist|not found|"
    r"no activities found|aborted|not allowed|unknown command)",
    re.IGNORECASE,
)


def _memory_status() -> dict:
    result = {"total_bytes": None, "available_bytes": None}
    try:
        entries = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, _, value = line.partition(":")
            fields = value.split()
            if name in ("MemTotal", "MemAvailable") and fields:
                number = int(fields[0])
                if number >= 0:
                    entries[name] = number * 1024
        result["total_bytes"] = entries.get("MemTotal")
        result["available_bytes"] = entries.get("MemAvailable")
    except (OSError, ValueError):
        pass
    return result


def _battery_status() -> dict | None:
    executable = shutil.which("termux-battery-status")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable], capture_output=True, text=True, timeout=3, check=False
        )
        if result.returncode:
            return None
        battery = json.loads(result.stdout)
        if isinstance(battery, dict) and battery and "error" not in battery:
            return battery
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _thermal_status() -> list[dict]:
    readings = []
    try:
        zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*"))
    except OSError:
        return readings
    for zone in zones:
        try:
            temperature = float((zone / "temp").read_text().strip())
            # Linux thermal sysfs specifies millidegrees Celsius.
            temperature /= 1000
            if not -20 <= temperature <= 150:
                continue
            name = (zone / "type").read_text().strip() or zone.name
            # sysfs/vendor labels should not be able to inject terminal controls.
            name = "".join(c for c in name if c.isprintable())[:80]
            readings.append({"name": name, "celsius": temperature})
        except (OSError, ValueError):
            continue
    return readings


def collect_status() -> dict:
    """Return current metrics without requiring storage, root, or API access."""
    try:
        disk = shutil.disk_usage(Path.home())
        disk_status = {"total_bytes": disk.total, "free_bytes": disk.free}
    except OSError:
        disk_status = {"total_bytes": None, "free_bytes": None}
    return {
        "memory": _memory_status(),
        "disk": disk_status,
        "battery": _battery_status(),
        "thermal": _thermal_status(),
    }


def _run_android(arguments: list[str], *, timeout: int = 15) -> str:
    try:
        completed = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Android command timed out; check the phone screen.") from error
    except OSError as error:
        raise RuntimeError(f"Cannot run Android command: {error}") from error
    output = "\n".join(
        text.strip() for text in (completed.stdout, completed.stderr) if text.strip()
    )
    if completed.returncode or _ANDROID_ERRORS.search(output):
        detail = output[-1000:] or f"exit code {completed.returncode}"
        raise RuntimeError(detail)
    return output


def launch_game(package: str) -> str:
    """Open a game's existing launcher activity; do not modify phone settings.

    Android package visibility and vendor permissions can prevent launch. Only
    the requested package is passed, as a literal argument, to Android tools.
    """
    if not PACKAGE_RE.fullmatch(package):
        raise ValueError("Use an Android package name, for example com.example.game.")

    am = shutil.which("am")
    cmd = shutil.which("cmd")
    monkey = shutil.which("monkey")
    errors = []
    component = None
    if cmd and am:
        try:
            resolution = _run_android(
                [cmd, "package", "resolve-activity", "--brief", "-a",
                 "android.intent.action.MAIN", "-c",
                 "android.intent.category.LAUNCHER", "-p", package]
            )
            for line in resolution.splitlines():
                candidate = line.strip()
                if re.fullmatch(re.escape(package) + r"/[A-Za-z0-9_.$]+", candidate):
                    component = candidate
        except RuntimeError as error:
            errors.append(str(error))

    if am:
        arguments = [am, "start", "-W"]
        if component:
            arguments += ["-n", component]
        else:
            arguments += ["-a", "android.intent.action.MAIN", "-c",
                          "android.intent.category.LAUNCHER", "-p", package]
        try:
            output = _run_android(arguments)
            # am -W reports a status even when an existing activity is resumed.
            if not re.search(r"^Status:\s*ok\s*$", output, re.MULTILINE | re.IGNORECASE):
                raise RuntimeError("Android did not confirm launch with Status: ok.")
            return f"Opened {package}.\n{output}"
        except RuntimeError as error:
            errors.append(str(error))

    if monkey:
        try:
            output = _run_android(
                [monkey, "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
            )
            if not re.search(r"Events injected:\s*1\b", output):
                raise RuntimeError("Android did not confirm a launcher event.")
            return f"Launcher event sent to {package}.\n{output}"
        except RuntimeError as error:
            errors.append(str(error))

    if not am and not monkey:
        raise RuntimeError("Android launcher tools are unavailable; run this command in Termux on Android.")
    detail = "; ".join(errors[-2:])
    raise RuntimeError(f"Cannot open {package}. It may be missing or Android denied access. {detail}")


@contextmanager
def wake_lock(enabled: bool):
    """Optionally hold Termux's CPU wake lock until this context exits.

    This keeps the Termux task awake; it does not raise CPU frequency or FPS.
    Do not use this flag when a separate Termux task owns a wake lock, because
    Termux wake locks are shared by the app rather than reference-counted here.
    """
    if not enabled:
        yield
        return
    acquire = shutil.which("termux-wake-lock")
    release = shutil.which("termux-wake-unlock")
    if not acquire or not release:
        raise RuntimeError("Install/run Termux to use termux-wake-lock and termux-wake-unlock.")
    _run_android([acquire], timeout=5)
    try:
        yield
    finally:
        _run_android([release], timeout=5)
