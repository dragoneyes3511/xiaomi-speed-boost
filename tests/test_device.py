"""Device boundaries are mocked: CI has no Android services or permissions."""

import subprocess
import unittest
from unittest.mock import patch

from xiaomi_boost import device


def android_tools(name):
    return f"/system/bin/{name}" if name in {"am", "cmd", "monkey"} else None


class StatusTests(unittest.TestCase):
    def test_permission_denied_memory_is_unknown(self):
        with patch.object(device.Path, "read_text", side_effect=PermissionError):
            self.assertEqual(device._memory_status(), {
                "total_bytes": None, "available_bytes": None,
            })

    def test_reads_memavailable_without_guessing(self):
        with patch.object(device.Path, "read_text", return_value=(
            "MemTotal: 8000000 kB\nMemFree: 1000 kB\nMemAvailable: 2000000 kB\n"
        )):
            self.assertEqual(device._memory_status(), {
                "total_bytes": 8000000 * 1024,
                "available_bytes": 2000000 * 1024,
            })

    def test_no_api_does_not_run_battery_command(self):
        with patch.object(device.shutil, "which", return_value=None), \
                patch.object(device.subprocess, "run") as run:
            self.assertIsNone(device._battery_status())
            run.assert_not_called()

    def test_missing_battery_api_application_is_unknown(self):
        with patch.object(device.shutil, "which", return_value="termux-battery-status"), \
                patch.object(device.subprocess, "run", side_effect=subprocess.TimeoutExpired("battery", 3)):
            self.assertIsNone(device._battery_status())

    def test_android_thermal_permissions_are_optional(self):
        with patch.object(device.Path, "glob", side_effect=PermissionError):
            self.assertEqual(device._thermal_status(), [])


class LaunchTests(unittest.TestCase):
    def test_package_is_validated_before_process_creation(self):
        with patch.object(device.subprocess, "run") as run:
            for value in ["com.game; rm -rf /", "--help", "com.test\nwhoami", "", "single"]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    device.launch_game(value)
            run.assert_not_called()

    def test_zero_exit_android_error_is_failure(self):
        with patch.object(device.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["am"], 0, "Error: Activity class does not exist.", ""
        )):
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                device._run_android(["am"])

    def test_resolves_and_launches_only_requested_package(self):
        with patch.object(device.shutil, "which", side_effect=android_tools), \
                patch.object(device, "_run_android", side_effect=[
                    "com.example.game/.MainActivity", "Status: ok\nActivity: com.example.game/.MainActivity",
                ]) as run:
            self.assertIn("Opened com.example.game", device.launch_game("com.example.game"))
            self.assertEqual(run.call_args_list[1].args[0], [
                "/system/bin/am", "start", "-W", "-n", "com.example.game/.MainActivity",
            ])

    def test_foreign_resolver_component_is_not_used(self):
        with patch.object(device.shutil, "which", side_effect=android_tools), \
                patch.object(device, "_run_android", side_effect=[
                    "com.attacker.app/.MainActivity", "Status: ok",
                ]) as run:
            device.launch_game("com.example.game")
            command = run.call_args_list[1].args[0]
            self.assertNotIn("-n", command)
            self.assertEqual(command[-2:], ["-p", "com.example.game"])

    def test_monkey_fallback_checks_injected_event(self):
        with patch.object(device.shutil, "which", side_effect=android_tools), \
                patch.object(device, "_run_android", side_effect=[
                    RuntimeError("resolver denied"), RuntimeError("am denied"), "Events injected: 1\nDone.",
                ]):
            self.assertIn("Launcher event sent", device.launch_game("com.example.game"))

    def test_unconfirmed_launch_never_reports_success(self):
        with patch.object(device.shutil, "which", side_effect=android_tools), \
                patch.object(device, "_run_android", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "Cannot open"):
                device.launch_game("com.example.game")

    def test_non_android_host_reports_unavailable(self):
        with patch.object(device.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Termux on Android"):
                device.launch_game("com.example.game")


class WakeLockTests(unittest.TestCase):
    def test_release_runs_after_game_exception(self):
        with patch.object(device.shutil, "which", side_effect=lambda name: name), \
                patch.object(device, "_run_android", return_value="") as run:
            with self.assertRaisesRegex(ValueError, "game stopped"):
                with device.wake_lock(True):
                    raise ValueError("game stopped")
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["termux-wake-lock"], ["termux-wake-unlock"],
            ])

    def test_disabled_lock_does_not_run_commands(self):
        with patch.object(device, "_run_android") as run:
            with device.wake_lock(False):
                pass
            run.assert_not_called()

    def test_missing_release_tool_does_not_acquire(self):
        with patch.object(device.shutil, "which", side_effect=lambda name: None if name.endswith("unlock") else name), \
                patch.object(device, "_run_android") as run:
            with self.assertRaises(RuntimeError):
                with device.wake_lock(True):
                    self.fail("context must not run without both tools")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
