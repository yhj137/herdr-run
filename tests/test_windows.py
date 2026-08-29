import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "herdr_run.py"
SPEC = importlib.util.spec_from_file_location("herdr_run", SCRIPT)
herdr_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(herdr_run)


@unittest.skipUnless(os.name == "nt", "Windows-specific behavior")
class WindowsShellCommandTests(unittest.TestCase):
    def test_log_is_utf8_without_bom_and_marker_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "output.log")
            marker = log + ".running"
            command = "Write-Output 'yhj137'; Write-Output '中文日志'"

            with mock.patch.object(herdr_run, "ON_WINDOWS", True):
                shell_command = herdr_run.build_shell_command(
                    tmp, "[herdr-run] launch=test started=", command,
                    log, marker)

            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", shell_command],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15)

            self.assertEqual(result.returncode, 0, result.stderr)
            data = Path(log).read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            text = data.decode("utf-8")
            self.assertIn("launch=test", text)
            self.assertIn("yhj137", text)
            self.assertIn("中文日志", text)
            self.assertFalse(os.path.exists(marker))

    def test_running_marker_overrides_incorrect_idle_process_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "job.log.running")
            Path(marker).write_text("123", encoding="ascii")
            entries = [{"pane": "w1:p1", "running_marker": marker}]

            with mock.patch.object(herdr_run, "herdr_try") as herdr_try:
                self.assertFalse(
                    herdr_run.pane_is_idle("w1:p1", entries))
                herdr_try.assert_not_called()

    def test_herdr_cli_is_always_decoded_as_utf8(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}", stderr="")
        with mock.patch.object(
                herdr_run.subprocess, "run", return_value=completed) as run:
            herdr_run.run_herdr_process(["workspace", "list"])

        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_posix_timestamp_command_is_linux_and_macos_portable(self):
        with mock.patch.object(herdr_run, "ON_WINDOWS", False):
            shell_command = herdr_run.build_shell_command(
                "/tmp/project", "[herdr-run] started=", "sleep 1",
                "/tmp/job.log", "/tmp/job.log.running")

        self.assertNotIn("date -I", shell_command)
        self.assertIn("date '+%Y-%m-%dT%H:%M:%S%z'", shell_command)
        self.assertIn("tee -a", shell_command)


if __name__ == "__main__":
    unittest.main()
