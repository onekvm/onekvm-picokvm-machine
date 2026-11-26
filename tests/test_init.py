"""The initramfs picks the slot U-Boot loaded, prepares userdata and hands the boot bar on."""
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
UNITS = ROOT / "boot-splash" / "systemd"


def init_lines():
    return (ROOT / "init").read_text().splitlines()


def function(name):
    lines = init_lines()
    start = lines.index(f"{name}() {{")
    return "\n".join(lines[start:lines.index("}", start) + 1])


def checkpoints():
    source = (ROOT / "boot-splash" / "onekvm-boot-splash.c").read_text()
    table = source[source.index("boot_stages[] = {"):]
    table = table[:table.index("};")]
    return re.findall(r'\{ "([a-z]+)", (\d+), (\d+) \}', table)


class BootSlot(unittest.TestCase):
    def slot(self, cmdline):
        script = function("boot_slot") + '\nboot_slot "$1"\n'
        return subprocess.run(["/bin/sh", "-c", script, "sh", cmdline], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_rauc_slot_from_onekvm_u_boot(self):
        self.assertEqual(self.slot("console=ttyS0 rauc.slot=B onekvm.build-id=x"), "B")
        self.assertEqual(self.slot("rauc.slot=A"), "A")

    def test_slot_a_without_one(self):
        self.assertEqual(self.slot("console=ttyS0"), "A")
        self.assertEqual(self.slot("rauc.slot=C"), "A")

    def test_slot_roots(self):
        lines = init_lines()
        self.assertIn("root=/dev/mmcblk1p2", lines)
        self.assertIn('[ "$slot" = B ] && root=/dev/mmcblk1p3', lines)


class BootBar(unittest.TestCase):
    def run_splash(self, framebuffer):
        lines = init_lines()
        start = lines.index("splash=")
        end = lines.index("fi", start)
        storage = next(line for line in lines if "progress storage" in line)
        script = "\n".join([function("wait_for")] + lines[start:end + 1] + ["wait", storage]) + "\n"
        with tempfile.TemporaryDirectory() as temp:
            tools, calls = Path(temp) / "bin", Path(temp) / "calls"
            tools.mkdir()
            for name, body in (("onekvm-boot-splash", f'printf "%s\\n" "$*" >>"{calls}"'),
                               ("usleep", ":")):
                path = tools / name
                path.write_text(f"#!/bin/sh\n{body}\n")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            script = script.replace("/dev/fb0", framebuffer or str(Path(temp) / "missing"))
            subprocess.run(["/bin/sh", "-c", script], check=True,
                           env={"PATH": f"{tools}:{os.environ['PATH']}"})
            return calls.read_text().splitlines() if calls.exists() else []

    def test_boot_screen_starts_the_animator_and_marks_storage(self):
        with tempfile.NamedTemporaryFile() as framebuffer:
            self.assertEqual(self.run_splash(framebuffer.name),
                             ["--progress initramfs boot", "animate", "progress storage"])

    def test_no_panel_means_no_bar_calls(self):
        self.assertEqual(self.run_splash(None), [])

    def test_storage_is_marked_after_the_root_mount_and_before_dev_moves(self):
        lines = init_lines()
        storage = next(i for i, line in enumerate(lines) if "progress storage" in line)
        self.assertLess(next(i for i, line in enumerate(lines) if line.startswith("mount -t overlay")), storage)
        self.assertLess(storage, next(i for i, line in enumerate(lines) if 'mount --move "/$dir"' in line))
        self.assertLess(storage, next(i for i, line in enumerate(lines) if "exec switch_root" in line))



class Storage(unittest.TestCase):
    def index(self, text):
        lines = init_lines()
        return next(i for i, line in enumerate(lines) if line.startswith(text))

    def test_overlay_on_userdata_is_ready_before_switch_root(self):
        order = [
            "ab_layout || fail",
            'mount -t squashfs -o ro "$root" /rofs',
            "if ! mount_userdata; then",
            "mkdir -p /userdata/upper /userdata/work /userdata/storage",
            "/usr/lib/onekvm/initramfs-kernel-overlay /rofs /userdata /userdata/upper",
            "mount -t overlay overlay -o lowerdir=/rofs,upperdir=/userdata/upper,workdir=/userdata/work /newroot",
            "mount --bind /userdata/storage /newroot/mnt/storage",
            'for dir in dev proc sys run; do',
            "exec switch_root",
        ]
        positions = [self.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))

    def test_userdata_problems_never_stop_the_boot(self):
        lines = init_lines()
        fallback = lines.index("if ! mount_userdata; then")
        self.assertIn("mount -t tmpfs -o mode=0755 tmpfs /userdata || fail 'cannot mount temporary userdata'",
                      [line.strip() for line in lines[fallback:fallback + 6]])
        text = "/usr/lib/onekvm/initramfs-kernel-overlay"
        statement = lines[self.index(text)]
        if statement.endswith("||"):
            statement += lines[self.index(text) + 1]
        self.assertIn("|| echo", statement.replace("||    ", "|| "))
        self.assertNotRegex(statement, r"\bfail\b")

    def test_userdata_is_created_and_formatted_only_by_the_storage_tool(self):
        self.assertIn("chroot /rofs /usr/libexec/onekvm/picokvm-prepare-storage",
                      function("prepare_userdata"))
        source = (ROOT / "init").read_text()
        for tool in ("mkfs", "sfdisk", "partx"):
            self.assertNotIn(tool, source)


class PrepareStorage(unittest.TestCase):
    """picokvm-prepare-storage against a fake card, with the tools stubbed."""

    def run_tool(self, card_sectors=60000000, p4_start=None, blkid_rc=2):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            block, tools, calls = temp / "block", temp / "bin", temp / "calls"
            (block / "mmcblk1").mkdir(parents=True)
            (block / "mmcblk1" / "size").write_text("%d\n" % card_sectors)
            tools.mkdir()
            p4 = block / "mmcblk1p4"

            def add_p4(start):
                p4.mkdir()
                (p4 / "start").write_text("%d\n" % start)
                (temp / "mmcblk1p4").write_text("")

            if p4_start is not None:
                add_p4(p4_start)
            stubs = {
                "sfdisk": f'printf "sfdisk %s <%s>\\n" "$*" "$(cat)" >>"{calls}"',
                "partx": f'echo "partx $*" >>"{calls}"; mkdir -p "{p4}"; echo 1343488 >"{p4}/start"; '
                         f': >"{temp}/mmcblk1p4"',
                "blkid": f'echo "blkid $*" >>"{calls}"; exit {blkid_rc}',
                "mkfs.ext4": f'echo "mkfs.ext4 $*" >>"{calls}"',
                "sleep": ":",
            }
            for name, body in stubs.items():
                path = tools / name
                path.write_text(f"#!/bin/sh\n{body}\n")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            script = ((ROOT / "picokvm-prepare-storage").read_text()
                      .replace("/sys/class/block", str(block))
                      .replace("disk=/dev/mmcblk1", f"disk={temp}/mmcblk1")
                      .replace('[ -b "$part" ]', '[ -e "$part" ]'))
            result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                                    env={"PATH": f"{tools}:{os.environ['PATH']}"})
            lines = calls.read_text().replace(str(temp), "").splitlines() if calls.exists() else []
            return result.returncode, lines

    def test_a_new_card_gets_userdata_up_to_its_end(self):
        rc, calls = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [
            "sfdisk --no-reread --no-tell-kernel --wipe never --wipe-partitions never -q -N 4 "
            "/mmcblk1 <start=1343488, type=83>",
            "partx --add --nr 4 /mmcblk1",
            "blkid -p -D /mmcblk1p4",
            "mkfs.ext4 -q -F -L userdata -m 0 -E lazy_itable_init=1,lazy_journal_init=1 /mmcblk1p4",
        ])

    def test_existing_userdata_is_kept(self):
        self.assertEqual(self.run_tool(p4_start=1343488, blkid_rc=0), (0, ["blkid -p -D /mmcblk1p4"]))

    def test_a_blank_userdata_partition_is_formatted(self):
        rc, calls = self.run_tool(p4_start=1343488)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[-1].split()[0], "mkfs.ext4")

    def test_other_partitions_and_small_cards_are_left_alone(self):
        self.assertEqual(self.run_tool(p4_start=2758208), (1, []))
        self.assertEqual(self.run_tool(card_sectors=1343488 + 524287), (1, []))
        self.assertEqual(self.run_tool(p4_start=1343488, blkid_rc=4), (1, ["blkid -p -D /mmcblk1p4"]))


class Checkpoints(unittest.TestCase):
    def test_checkpoints_follow_the_boot(self):
        names = [name for name, _, _ in checkpoints()]
        self.assertEqual(names, ["initramfs", "storage", "early", "sysinit", "basic", "screen", "done"])
        percents = [int(percent) for _, percent, _ in checkpoints()]
        # U-Boot draws 2% and 4% before Linux.
        self.assertGreater(percents[0], 4)
        self.assertEqual(percents, sorted(set(percents)))
        self.assertEqual(percents[-1], 100)

    def test_units_mark_root_checkpoints_without_holding_up_targets(self):
        self.assertEqual(sorted(path.name for path in UNITS.iterdir()), [
            "onekvm-boot-progress-basic.service",
            "onekvm-boot-progress-early.service",
            "onekvm-boot-progress-sysinit.service",
        ])
        for stage in ("early", "sysinit", "basic"):
            lines = (UNITS / f"onekvm-boot-progress-{stage}.service").read_text().splitlines()
            self.assertIn(f"ExecStart=-/usr/sbin/onekvm-boot-splash progress {stage}", lines)
            self.assertIn("Before=onekvm-device-screen-display.service", lines)
            self.assertIn("DefaultDependencies=no", lines)
            self.assertIn("RemainAfterExit=yes", lines)
            self.assertFalse([line for line in lines if re.match(r"Before=.*\.target", line)])
        self.assertIn("After=sysinit.target onekvm-boot-progress-early.service",
                      (UNITS / "onekvm-boot-progress-sysinit.service").read_text().splitlines())
        self.assertIn("After=basic.target onekvm-boot-progress-sysinit.service",
                      (UNITS / "onekvm-boot-progress-basic.service").read_text().splitlines())


@unittest.skipUnless(shutil.which("busybox"), "needs BusyBox, as in the initramfs")
class Recovery(unittest.TestCase):
    """The shared Recovery hook runs before any slot is mounted."""

    def index(self, text):
        # A call is its own line; other statements start with the text.
        lines = init_lines()
        return next(i for i, line in enumerate(lines)
                    if line == text or (" " in text and line.startswith(text)))

    def run_script(self, script, tools=(), files=None):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            for name, data in (files or {}).items():
                (temp / name).write_bytes(data)
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            busybox = shutil.which("busybox")
            for applet in ("hexdump", "sed", "mkdir", "printf", "cat", "grep"):
                os.symlink(busybox, bin_dir / applet)
            for name, body in tools:
                path = bin_dir / name
                path.write_text(f"#!{busybox} sh\n{body.replace('@TEMP@', str(temp))}\n")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            script = script.replace("/run/", f"{temp}/run/").replace("@TEMP@", str(temp))
            result = subprocess.run([busybox, "sh", "-c", script], env={"PATH": str(bin_dir)},
                                    capture_output=True, text=True, check=True)
            calls = (temp / "calls").read_text() if (temp / "calls").exists() else ""
            status = temp / "run" / "onekvm-boot-fat" / "status"
            return result.stdout, calls, status.read_text().strip() if status.exists() else None

    def test_recovery_runs_before_the_slot_root_and_after_the_layout_check(self):
        order = [
            "ab_layout || fail",
            "boot_fat_check",
            "first_boot_install || installed=",
            'if [ -n "$installed" ] && [ ! -e /sys/class/block/mmcblk1p4 ] &&',
            "recovery_check",
            '[ -n "$installed" ] || fail',
            'mount -t squashfs -o ro "$root" /rofs',
        ]
        positions = [self.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        # The new userdata partition is made with the slot root released again.
        create = self.index('if [ -n "$installed" ] && [ ! -e /sys/class/block/mmcblk1p4 ]')
        self.assertIn("umount /rofs || fail 'cannot release the slot root'",
                      [line.strip() for line in init_lines()[create:create + 4]])

    def test_board_hooks_draw_the_screen(self):
        check = function("recovery_check")
        self.assertIn(". /init.d/20-recovery", check)
        self.assertIn("recovery_board_enter() { picokvm_recovery_enter; }", check)
        self.assertIn('recovery_board_addresses() { picokvm_recovery_addresses "$1"; }', check)
        self.assertIn("recovery_board_usb_ready() { picokvm_recovery_usb_ready; }", check)
        self.assertIn("ONEKVM_RECOVERY_USB_PRODUCT='OneKVM PicoKVM Recovery'", check)
        self.assertLess(check.index(". /init.d/20-recovery"), check.index("recovery_board_enter()"))

    def test_screen_lists_the_recovery_addresses(self):
        banner = ("OneKVM recovery web (system, dhcp)\\n  DHCP http://10.0.0.5/\\n  https://10.0.0.5/\\n"
                  "  FALLBACK http://192.168.233.10/\\n  https://192.168.233.10/\\n")
        script = "\n".join([function("recovery_text"), function("picokvm_recovery_enter"),
                            function("picokvm_recovery_addresses")]) + f"""
splash=1
picokvm_recovery_enter
picokvm_recovery_addresses "$(printf '{banner}')"
"""
        splash = 'for a in "$@"; do printf "%s|" "$a" >>@TEMP@/calls; done; printf "#" >>@TEMP@/calls'
        _, calls, _ = self.run_script(script, [("onekvm-boot-splash", splash)])
        usb = "USB: the whole SD card is a drive. Write the OneKVM PicoKVM image to it, then restart."
        finish, enter, addresses, _ = calls.split("#")
        self.assertEqual(finish, "finish|")
        self.assertEqual(enter, "--message|Recovery mode\nWaiting for an IP address\n\n" + usb + "|recovery|")
        self.assertEqual(addresses, "--message|Recovery mode\nhttps://10.0.0.5/\nhttps://192.168.233.10/\n\n"
                         + usb + "|recovery|")

    def test_no_panel_means_no_recovery_screen(self):
        script = "\n".join([function("recovery_text"), function("picokvm_recovery_enter"),
                            function("picokvm_recovery_addresses")]) + """
splash=
picokvm_recovery_enter
picokvm_recovery_addresses '  https://10.0.0.5/'
"""
        _, calls, _ = self.run_script(script, [("onekvm-boot-splash", "echo $* >>@TEMP@/calls")])
        self.assertEqual(calls, "")

    def usb_ready(self, answer_after, answer="SDP"):
        """Charger detection answers after `answer_after` waits (None: never)."""
        script = function("picokvm_recovery_usb_ready").replace(
            "/sys/class/extcon/", "@TEMP@/extcon/") + "\npicokvm_recovery_usb_ready\n"
        lines = "USB=0\nUSB-HOST=0\nSDP=0\nCDP=0\nDCP=0\nSLOW-CHARGER=0\n"
        done = lines.replace(f"{answer}=0", f"{answer}=1")
        usleep = (f'echo "usleep $1" >>@TEMP@/calls; '
                  f'[ $(grep -c usleep @TEMP@/calls) -lt {answer_after or 10**6} ] || '
                  f"printf '{done}' >@TEMP@/extcon/extcon0/state")
        initial = done if answer_after == 0 else lines
        script = f"mkdir -p @TEMP@/extcon/extcon0; printf '{initial}' >@TEMP@/extcon/extcon0/state\n" + script
        out, calls, _ = self.run_script(script, [("usleep", usleep)])
        return out, calls.splitlines()

    def test_the_gadget_waits_for_charger_detection(self):
        self.assertEqual(self.usb_ready(0), ("", []))
        out, calls = self.usb_ready(7)
        self.assertEqual((out, calls), ("", ["usleep 100000"] * 7))
        # A charger answer ends the wait too; the gadget then cannot connect anyway.
        self.assertEqual(len(self.usb_ready(3, "DCP")[1]), 3)

    def test_without_a_usb_host_the_gadget_starts_after_five_seconds(self):
        out, calls = self.usb_ready(None)
        self.assertEqual(len(calls), 50)
        self.assertIn("no USB host detected", out)

    def fat_check(self, state, fsck_rc=0):
        script = function("bytes_hex") + "\n" + function("boot_fat_check") + "\nboot_fat_check\n"
        script = script.replace("/dev/mmcblk1p1", "@TEMP@/p1")
        fsck = f'echo "fsck $*" >>@TEMP@/calls; exit {fsck_rc}'
        boot_sector = bytes(0x41) + bytes([state]) + bytes(512 - 0x42)
        out, calls, status = self.run_script(script, [("fsck.fat", fsck)], {"p1": boot_sector})
        return out, calls.splitlines(), status

    def test_a_clean_boot_partition_is_not_checked(self):
        _, calls, status = self.fat_check(0x00)
        self.assertEqual((calls, status), ([], None))
        _, calls, status = self.fat_check(0x02)
        self.assertEqual((calls, status), ([], None))

    def test_a_dirty_boot_partition_is_repaired(self):
        out, calls, status = self.fat_check(0x01, fsck_rc=1)
        self.assertEqual(len(calls), 1)
        self.assertIn("-a -w", calls[0])
        self.assertIsNone(status)
        self.assertIn("repaired", out)

    def test_a_failed_repair_is_recorded_for_recovery(self):
        _, calls, status = self.fat_check(0x01, fsck_rc=4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(status, "failed")


def squashfs(size=8192, used=5000, magic=b"hsqs"):
    data = bytearray(range(256)) * (size // 256) + bytearray(size % 256)
    data[0:4] = magic
    struct.pack_into("<Q", data, 40, used)
    return bytes(data)


@unittest.skipUnless(shutil.which("busybox"), "needs BusyBox, as in the initramfs")
class FirstBootInstall(unittest.TestCase):
    """The root image on the FAT becomes both slots before slot A boots."""

    def install(self, root=None, slot="A", marker=True, broken_slot=False, splash="1"):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            fat = temp / "fat"
            (fat / "onekvm").mkdir(parents=True)
            (fat / "boot-a.itb").write_bytes(b"fit" * 1000)
            if root is not None:
                (fat / "onekvm" / "root.squashfs").write_bytes(root)
            if marker:
                (fat / "onekvm" / "install-b").write_text("copy\n")
            for n in (2, 3):
                (temp / f"p{n}").write_bytes(b"\xff" * 8192)
            if broken_slot:
                (temp / "p3").unlink()
                (temp / "p3").mkdir()
            tools = temp / "bin"
            tools.mkdir()
            busybox = shutil.which("busybox")
            for applet in ("hexdump", "dd", "sha256sum", "stat", "rm", "cp", "mv", "cat", "mkdir",
                           "printf", "tr"):
                os.symlink(busybox, tools / applet)
            for name in ("mount", "umount", "sync", "onekvm-boot-splash"):
                # One line per call: the install message has line breaks.
                (tools / name).write_text(f'#!{busybox} sh\n'
                                          f'printf "%s\\n" "{name} $(printf %s "$*" | tr "\\n" "~")" '
                                          f'>>{temp}/calls\n')
                (tools / name).chmod(0o755)
            functions = [function(name) for name in ("bytes_hex", "squashfs_bytes", "install_roots",
                                                     "install_slot_b", "first_boot_install")]
            script = (f"install_fat={fat}\n"
                      "root_file=$install_fat/onekvm/root.squashfs\n"
                      "install_marker=$install_fat/onekvm/install-b\n"
                      + "\n".join(functions)
                      + f"\nslot={slot}\nsplash={splash}\nfirst_boot_install\nrc=$?\nwait\necho rc=$rc\n")
            script = (script.replace("/dev/mmcblk1p$part", f"{temp}/p$part")
                      .replace("/proc/sys/vm/drop_caches", f"{temp}/drop"))
            out = subprocess.run([busybox, "sh", "-c", script], env={"PATH": str(tools)},
                                 capture_output=True, text=True, check=True).stdout
            calls = (temp / "calls").read_text().splitlines() if (temp / "calls").exists() else []
            slots = [(temp / f"p{n}").read_bytes() if (temp / f"p{n}").is_file() else None
                     for n in (2, 3)]
            files = sorted(str(path.relative_to(fat)) for path in fat.rglob("*") if path.is_file())
            boot_b = (fat / "boot-b.itb").read_bytes() if (fat / "boot-b.itb").exists() else None
            return int(out.split("rc=")[-1]), slots, files, boot_b, calls

    def test_the_root_image_becomes_both_slots_and_slot_b_gets_the_fit(self):
        root = squashfs()
        rc, slots, files, boot_b, calls = self.install(root)
        self.assertEqual(rc, 0)
        self.assertEqual(slots, [root, root])
        self.assertEqual(files, ["boot-a.itb", "boot-b.itb"])
        self.assertEqual(boot_b, b"fit" * 1000)
        splash = [call for call in calls if call.startswith("onekvm-boot-splash")]
        self.assertEqual(splash[0], "onekvm-boot-splash finish")
        self.assertEqual(splash[1], "onekvm-boot-splash --message "
                         "Installing OneKVM~~Do not disconnect power. message")
        self.assertEqual(splash[2:], ["onekvm-boot-splash --progress initramfs boot",
                                      "onekvm-boot-splash animate"])
        self.assertEqual(calls[-3], "umount " + calls[0].split()[-1])

    def test_a_failed_write_keeps_the_root_image_for_the_next_boot(self):
        rc, _, files, boot_b, _ = self.install(squashfs(), broken_slot=True)
        self.assertEqual(rc, 1)
        self.assertEqual(files, ["boot-a.itb", "onekvm/install-b", "onekvm/root.squashfs"])
        self.assertIsNone(boot_b)

    def test_bad_root_images_are_not_written(self):
        for root in (squashfs(magic=b"sqsh"), squashfs(size=5000, used=4000),
                     squashfs(used=9000), squashfs(used=0)):
            rc, slots, files, _, _ = self.install(root)
            self.assertEqual(rc, 1)
            self.assertEqual(slots, [b"\xff" * 8192] * 2)
            self.assertIn("onekvm/root.squashfs", files)

    def test_an_installed_root_still_gets_slot_b(self):
        rc, slots, files, boot_b, _ = self.install(None)
        self.assertEqual(rc, 0)
        self.assertEqual(slots, [b"\xff" * 8192] * 2)
        self.assertEqual(files, ["boot-a.itb", "boot-b.itb"])
        self.assertEqual(boot_b, b"fit" * 1000)

    def test_an_installed_card_boots_without_the_install_screen(self):
        rc, _, files, _, calls = self.install(None, marker=False)
        self.assertEqual(rc, 0)
        self.assertEqual(files, ["boot-a.itb"])
        self.assertEqual([call.split()[0] for call in calls], ["mount", "umount"])

    def test_only_slot_a_installs(self):
        rc, slots, files, _, calls = self.install(squashfs(), slot="B")
        self.assertEqual(rc, 0)
        self.assertEqual(slots, [b"\xff" * 8192] * 2)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
