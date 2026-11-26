"""Keep the SD layout readable by the boot ROM, the SPL, U-Boot and the initramfs."""
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("picokvm_sd", ROOT / "build-sd.py")
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


def init_function(name):
    lines = (ROOT / "init").read_text().splitlines()
    start = lines.index(name + "() {")
    return "\n".join(lines[start:lines.index("}", start) + 1])


class Layout(unittest.TestCase):
    def test_mbr_keeps_boot_area_and_slot_geometry(self):
        table = sd.mbr()
        self.assertEqual(table[:440], bytes(440))
        self.assertEqual(table[440:444], bytes.fromhex("4b504b4f"))
        self.assertEqual(table[510:], b"\x55\xaa")
        entries = [struct.unpack_from("<B3sB3sII", table, 446 + n * 16) for n in range(4)]
        self.assertEqual([(e[0], e[2], e[4], e[5]) for e in entries], [
            (0x80, 0x0C, 32768, 262144),
            (0, 0x83, 294912, 524288),
            (0, 0x83, 819200, 524288),
            (0, 0, 0, 0),
        ])
        self.assertEqual(sd.USERDATA_START, 1343488)

    def test_loaders_sit_where_the_boot_rom_and_spl_read_them(self):
        # Boot ROM: idblock at sector 64. SPL: CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR.
        self.assertEqual(sd.IDBLOADER_START, 64)
        self.assertEqual(sd.UBOOT_START, 0x4000)
        self.assertLessEqual(sd.UBOOT_START, sd.BOOT_START)

    def test_the_initramfs_and_storage_tool_check_the_same_geometry(self):
        layout = init_function("ab_layout")
        self.assertIn("= 4b504b4f", layout)
        self.assertIn("part_is 1 %d %d" % (sd.BOOT_START, sd.BOOT_SECTORS), layout)
        self.assertIn("part_is 2 %d %d" % (sd.ROOT_A, sd.SLOT_SECTORS), layout)
        self.assertIn("part_is 3 %d %d" % (sd.ROOT_B, sd.SLOT_SECTORS), layout)
        self.assertIn('= %d ]' % sd.USERDATA_START, layout)
        self.assertIn("start=%d\n" % sd.USERDATA_START,
                      (ROOT / "picokvm-prepare-storage").read_text())

    def test_inputs_need_their_magic_and_must_fit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input"
            path.write_bytes(b"hsqs" + bytes(60))
            self.assertEqual(len(sd.check(path, sd.SQUASHFS_MAGIC, 64, "SquashFS")), 64)
            with self.assertRaises(ValueError):
                sd.check(path, sd.SQUASHFS_MAGIC, 63, "SquashFS")
            with self.assertRaises(ValueError):
                sd.check(path, sd.IDBLOCK_MAGIC, 64, "an idblock")

    def fat_files(self, recovery=None, root_fits=True):
        """Files boot_fat() copies, in order, with mtools mocked."""
        copied, mkfs, order = {}, [], []

        def run(command, check=False, stdout=None):
            if command[0] == "mkfs.vfat":
                mkfs.extend(command)
            elif command[0] == "mcopy":
                if command[-1] == "::" + sd.ROOT_FILE and not root_fits:
                    return types.SimpleNamespace(returncode=1, stdout=b"")
                copied[command[-1]] = Path(command[-2]).read_bytes()
                order.append(command[-1])
            elif command[0] == "mmd":
                order.append(command[-1] + "/")
            elif command[0] == "mtype":
                return types.SimpleNamespace(stdout=copied[command[-1]])
            return types.SimpleNamespace(returncode=0, stdout=b"")

        with tempfile.TemporaryDirectory() as temp:
            fit = Path(temp) / "boot.itb"
            fit.write_bytes(b"fit")
            root = Path(temp) / "root.squashfs"
            root.write_bytes(b"hsqs root")
            zip_file = None
            if recovery:
                zip_file = Path(temp) / "onekvm-recovery.zip"
                zip_file.write_bytes(recovery)
            with patch.object(sd.subprocess, "run", side_effect=run):
                sd.boot_fat(Path(temp) / "boot.fat", fit, root, zip_file)
        return copied, mkfs, order

    def test_fat_holds_slot_a_and_the_root_to_install(self):
        copied, mkfs, order = self.fat_files()
        self.assertEqual(copied, {"::boot-a.itb": b"fit", "::boot-slot": b"A\n",
                                  "::onekvm/install-b": copied["::onekvm/install-b"],
                                  "::onekvm/root.squashfs": b"hsqs root"})
        # The root goes last, so the used part of the FAT ends with it.
        self.assertEqual(order, ["::boot-a.itb", "::boot-slot", "::onekvm/", "::onekvm/install-b",
                                 "::onekvm/root.squashfs"])
        self.assertEqual(mkfs[mkfs.index("-F") + 1], "32")
        self.assertEqual(mkfs[mkfs.index("-h") + 1], str(sd.BOOT_START))

    def test_fat_carries_recovery_zip(self):
        copied, _, order = self.fat_files(b"PK\x03\x04zip")
        self.assertEqual(copied["::recovery.zip"], b"PK\x03\x04zip")
        self.assertEqual(order[-1], "::onekvm/root.squashfs")

    def test_a_root_too_large_for_the_fat_is_rejected(self):
        with self.assertRaises(ValueError):
            self.fat_files(root_fits=False)

    def test_fat_end_is_the_end_of_the_last_allocated_cluster(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boot.fat"
            sector, cluster, reserved, fats, fat_sectors = 512, 2, 32, 2, 4
            boot = bytearray(512)
            struct.pack_into("<HBHB", boot, 11, sector, cluster, reserved, fats)
            struct.pack_into("<I", boot, 36, fat_sectors)
            table = bytearray(fat_sectors * sector)
            # Clusters 2-5 used, 6-9 free, 10 used (end of chain), the rest free.
            for n in (2, 3, 4, 5, 10):
                struct.pack_into("<I", table, n * 4, 0x0FFFFFFF)
            path.write_bytes(bytes(boot) + bytes((reserved - 1) * sector) + bytes(table) * fats)
            self.assertEqual(sd.fat_end(path), (reserved + fats * fat_sectors + 9 * cluster) * sector)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("mkfs.vfat", "mcopy", "mtype", "mmd")),
                     "needs dosfstools and mtools, as in the OE build")
class InstallImage(unittest.TestCase):
    """build-sd.py end to end: loaders, then the FAT up to the root image, nothing more."""

    def test_the_image_ends_with_the_root_on_the_fat(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            inputs = {"idbloader": sd.IDBLOCK_MAGIC + bytes(1020), "uboot": sd.LEGACY_MAGIC + bytes(2044),
                      "boot": sd.FDT_MAGIC + bytes(4092), "rootfs": b"hsqs" + bytes(3 * 1024 * 1024 - 4),
                      "recovery": b"PK\x03\x04" + bytes(100)}
            args = []
            for name, data in inputs.items():
                (temp / name).write_bytes(data)
                args += ["--" + name, str(temp / name)]
            image = temp / "sd.img"
            subprocess.run([sys.executable, str(ROOT / "build-sd.py"), *args, "--output", str(image)],
                           check=True)
            data = image.read_bytes()
            self.assertEqual(data[:512], sd.mbr())
            self.assertEqual(data[sd.IDBLOADER_START * 512:][:1024], inputs["idbloader"])
            self.assertEqual(data[sd.UBOOT_START * 512:][:2048], inputs["uboot"])
            # Far smaller than the slots it fills on the card.
            self.assertLess(len(data), sd.ROOT_A * 512)
            fat = "%s@@%d" % (image, sd.BOOT_START * 512)

            def fat_file(name):
                return subprocess.run(["mtype", "-i", fat, "::" + name], check=True,
                                      stdout=subprocess.PIPE).stdout

            self.assertEqual(fat_file(sd.ROOT_FILE), inputs["rootfs"])
            self.assertEqual(fat_file("boot-a.itb"), inputs["boot"])
            self.assertEqual(fat_file("boot-slot"), b"A\n")
            self.assertEqual(fat_file("recovery.zip"), inputs["recovery"])
            self.assertTrue(fat_file(sd.INSTALL_MARKER))
            # The image stops at a whole FAT sector.
            self.assertEqual(len(data) % 512, 0)


@unittest.skipUnless(shutil.which("busybox"), "needs BusyBox, as in the initramfs")
class LayoutCheck(unittest.TestCase):
    """ab_layout() against a fake disk and sysfs."""

    def layout(self, disk_id=sd.DISK_ID, parts=None):
        parts = parts or {1: (sd.BOOT_START, sd.BOOT_SECTORS), 2: (sd.ROOT_A, sd.SLOT_SECTORS),
                          3: (sd.ROOT_B, sd.SLOT_SECTORS)}
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            table = bytearray(sd.mbr())
            struct.pack_into("<I", table, 440, disk_id)
            (temp / "disk").write_bytes(bytes(table))
            for n, (start, size) in parts.items():
                block = temp / "block" / ("mmcblk1p%d" % n)
                block.mkdir(parents=True)
                (block / "start").write_text("%d\n" % start)
                (block / "size").write_text("%d\n" % size)
            tools = temp / "bin"
            tools.mkdir()
            busybox = shutil.which("busybox")
            for applet in ("hexdump", "cat"):
                os.symlink(busybox, tools / applet)
            script = "\n".join(init_function(name) for name in ("bytes_hex", "part_is", "ab_layout"))
            script = (script.replace("/sys/class/block", str(temp / "block"))
                      .replace("/dev/mmcblk1", str(temp / "disk")) + "\nab_layout\n")
            return subprocess.run([busybox, "sh", "-c", script], env={"PATH": str(tools)}).returncode == 0

    def test_accepts_the_built_layout_with_or_without_userdata(self):
        self.assertTrue(self.layout())
        parts = {1: (32768, 262144), 2: (294912, 524288), 3: (819200, 524288), 4: (1343488, 60000000)}
        self.assertTrue(self.layout(parts=parts))

    def test_rejects_other_cards(self):
        self.assertFalse(self.layout(disk_id=0x4F4B5647))  # NanoKVM Go
        self.assertFalse(self.layout(parts={1: (32768, 262144), 2: (294912, 524288)}))
        self.assertFalse(self.layout(parts={1: (32768, 262144), 2: (294912, 524288),
                                            3: (819200, 524288), 4: (2758208, 58264000)}))


if __name__ == "__main__":
    unittest.main()
