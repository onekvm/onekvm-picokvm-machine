"""A good boot clears the Recovery boot counter; a bad one keeps it."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
UNITS = ROOT / "systemd"


class ClearBootRetry(unittest.TestCase):
    def clear(self, onekvm_active=True, mounted=True, counter=True):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            boot, tools = temp / "boot", temp / "bin"
            boot.mkdir()
            tools.mkdir()
            if counter:
                (boot / "boot_retry_time.txt").write_text("1\n1\n")
            for name, code in (("mountpoint", 0 if mounted else 1),
                               ("systemctl", 0 if onekvm_active else 3), ("sync", 0)):
                path = tools / name
                path.write_text(f"#!/bin/sh\nexit {code}\n")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run(["/bin/sh", str(ROOT / "picokvm-clear-boot-retry")],
                                    env={"PATH": f"{tools}:{os.environ['PATH']}",
                                         "ONEKVM_BOOT_DIR": str(boot)})
            return result.returncode, (boot / "boot_retry_time.txt").exists()

    def test_a_good_boot_clears_the_counter(self):
        self.assertEqual(self.clear(), (0, False))

    def test_the_counter_stays_when_onekvm_stopped(self):
        rc, kept = self.clear(onekvm_active=False)
        self.assertNotEqual(rc, 0)
        self.assertTrue(kept)

    def test_the_counter_stays_without_the_boot_partition(self):
        rc, kept = self.clear(mounted=False)
        self.assertNotEqual(rc, 0)
        self.assertTrue(kept)

    def test_no_counter_is_fine(self):
        self.assertEqual(self.clear(counter=False), (0, False))

    def test_the_timer_waits_two_minutes_and_skips_recovery_boots(self):
        timer = (UNITS / "onekvm-boot-success.timer").read_text().splitlines()
        self.assertIn("OnActiveSec=2min", timer)
        self.assertIn("After=multi-user.target", timer)
        self.assertIn("ConditionKernelCommandLine=!onekvm_recovery", timer)
        self.assertIn("WantedBy=multi-user.target", timer)
        service = (UNITS / "onekvm-boot-success.service").read_text().splitlines()
        self.assertIn("ExecStart=/usr/libexec/onekvm/picokvm-clear-boot-retry", service)

    def test_boot_is_mounted_read_write(self):
        mount = (ROOT / "boot.mount").read_text().splitlines()
        self.assertIn("Options=noatime", mount)
        self.assertIn("What=/dev/mmcblk1p1", mount)


if __name__ == "__main__":
    unittest.main()
