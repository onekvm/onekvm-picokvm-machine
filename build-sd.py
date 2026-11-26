#!/usr/bin/env python3
"""Create a PicoKVM A/B SD install image from checked boot inputs; never write devices.

512-byte sectors of the installed card:

  0        MBR. The boot code area stays zero.
  64       idbloader.img: the RV1106 DDR blob and the SPL, read by the boot ROM
  16384    u-boot.img, read by the SPL
  32768    p1 FAT32 128 MiB, active: boot-a.itb, boot-b.itb, boot-slot and
           recovery.zip (the Recovery web page and its tools)
  294912   p2 read-only root A, 256 MiB SquashFS
  819200   p3 read-only root B, 256 MiB
  1343488  p4 userdata, created by the initramfs on first boot up to the card end

The image holds only the loaders and the used part of the FAT, as on the
NanoKVM Go: the root image is the FAT's last file, onekvm/root.squashfs. The
first boot from slot A writes it to p2 and p3 and copies boot-a.itb to
boot-b.itb, as onekvm/install-b asks. Writing the image leaves the rest of
the card untouched.
"""
import argparse
from pathlib import Path
import struct
import subprocess

DISK_ID = 0x4F4B504B  # OneKVM PicoKVM A/B layout; the initramfs checks it
IDBLOADER_START = 64
UBOOT_START = 16384
BOOT_START = 32768
BOOT_SECTORS = 262144
SLOT_SECTORS = 524288
ROOT_A = BOOT_START + BOOT_SECTORS
ROOT_B = ROOT_A + SLOT_SECTORS
USERDATA_START = ROOT_B + SLOT_SECTORS
SQUASHFS_MAGIC = b"hsqs"
IDBLOCK_MAGIC = b"RKNS"  # Rockchip boot ROM header v2 (mkimage -T rksd)
LEGACY_MAGIC = b"\x27\x05\x19\x56"  # u-boot.img
FDT_MAGIC = b"\xd0\x0d\xfe\xed"
# U-Boot reads a FIT to 0x02000000 and bootm loads the DT to 0x08300000.
FIT_MAX = 0x08300000 - 0x02000000
ROOT_FILE = "onekvm/root.squashfs"
INSTALL_MARKER = "onekvm/install-b"


def check(path, magic, limit, what):
    data = path.read_bytes()
    if not data.startswith(magic):
        raise ValueError("%s is not %s" % (path, what))
    if len(data) > limit:
        raise ValueError("%s is %d bytes; at most %d fit" % (path, len(data), limit))
    return data


def mbr():
    table = bytearray(512)
    struct.pack_into("<I", table, 440, DISK_ID)
    # CHS fields are saturated; U-Boot and Linux use LBA.
    for n, (active, typ, start, size) in enumerate((
            (0x80, 0x0C, BOOT_START, BOOT_SECTORS),
            (0, 0x83, ROOT_A, SLOT_SECTORS),
            (0, 0x83, ROOT_B, SLOT_SECTORS))):
        struct.pack_into("<B3sB3sII", table, 446 + n * 16, active, b"\xfe\xff\xff", typ,
                         b"\xfe\xff\xff", start, size)
    table[510:512] = b"\x55\xaa"
    return bytes(table)


def boot_fat(path, fit, rootfs, recovery=None):
    """p1 with slot A's FIT, boot-slot A, recovery.zip and the root to install.

    The root image goes last, so the used part of the FAT ends with it. Every
    copy is read back.
    """
    with path.open("wb") as f:
        f.truncate(BOOT_SECTORS * 512)
    # Hidden sectors = partition start.
    subprocess.run(["mkfs.vfat", "--invariant", "-F", "32", "-h", str(BOOT_START),
                    "-n", "ONEKVMBOOT", str(path)], check=True, stdout=subprocess.DEVNULL)
    slot_file = path.with_suffix(".slot")
    slot_file.write_text("A\n")
    marker = path.with_suffix(".install")
    marker.write_text("The first boot from slot A copies boot-a.itb to boot-b.itb.\n")
    files = [(fit, "boot-a.itb"), (slot_file, "boot-slot")]
    if recovery:
        files.append((recovery, "recovery.zip"))
    for source, name in files:
        subprocess.run(["mcopy", "-i", str(path), str(source), "::" + name], check=True)
    subprocess.run(["mmd", "-i", str(path), "::onekvm"], check=True)
    subprocess.run(["mcopy", "-i", str(path), str(marker), "::" + INSTALL_MARKER], check=True)
    if subprocess.run(["mcopy", "-i", str(path), str(rootfs), "::" + ROOT_FILE]).returncode:
        raise ValueError("the root image does not fit on the boot FAT next to the boot files")
    slot_file.unlink()
    marker.unlink()
    for source, name in files + [(rootfs, ROOT_FILE)]:
        if source == slot_file:
            continue
        copy = subprocess.run(["mtype", "-i", str(path), "::" + name], check=True,
                              stdout=subprocess.PIPE).stdout
        if copy != source.read_bytes():
            raise ValueError("FAT copy differs: " + name)


def fat_end(path):
    """Length of the FAT32 image up to the end of its last allocated cluster."""
    with path.open("rb") as f:
        boot = f.read(512)
        sector, cluster, reserved, fats = struct.unpack_from("<HBHB", boot, 11)
        fat_sectors = struct.unpack_from("<I", boot, 36)[0]
        f.seek(reserved * sector)
        table = f.read(fat_sectors * sector)
    entries = struct.unpack("<%dI" % (len(table) // 4), table)
    last = max(n for n in range(2, len(entries)) if entries[n] & 0x0FFFFFFF)
    # Cluster 2 is the first cluster of the data area.
    return (reserved + fats * fat_sectors + (last - 1) * cluster) * sector


def write_at(output, source, sector, size=None):
    count = ["count=%d" % size, "iflag=count_bytes"] if size is not None else []
    subprocess.run(["dd", "if=" + str(source), "of=" + str(output), "bs=512",
                    "seek=" + str(sector), *count, "conv=notrunc,sparse", "status=none"], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("idbloader", "uboot", "boot", "rootfs", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--recovery", type=Path, help="recovery.zip for p1")
    a = p.parse_args()
    loader = check(a.idbloader, IDBLOCK_MAGIC, (UBOOT_START - IDBLOADER_START) * 512,
                   "a Rockchip idblock")
    uboot = check(a.uboot, LEGACY_MAGIC, (BOOT_START - UBOOT_START) * 512, "a legacy U-Boot image")
    check(a.boot, FDT_MAGIC, FIT_MAX, "a FIT image")
    root = check(a.rootfs, SQUASHFS_MAGIC, SLOT_SECTORS * 512, "a SquashFS image")
    # The initramfs reads the installed slots back in 4 KiB blocks.
    if len(root) % 4096:
        raise ValueError("%s is not padded to 4 KiB" % a.rootfs)
    if a.recovery and not a.recovery.read_bytes().startswith(b"PK\x03\x04"):
        raise ValueError("%s is not a zip archive" % a.recovery)
    bootfs = a.output.with_suffix(".fat")
    boot_fat(bootfs, a.boot, a.rootfs, a.recovery)
    end = fat_end(bootfs)
    with a.output.open("wb") as f:
        f.truncate(BOOT_START * 512 + end)
        f.write(mbr())
    # Free FAT space after the last cluster stays as it is on the card.
    for source, sector, size in ((a.idbloader, IDBLOADER_START, None), (a.uboot, UBOOT_START, None),
                                 (bootfs, BOOT_START, end)):
        write_at(a.output, source, sector, size)
    fat = bootfs.read_bytes()[:end]
    bootfs.unlink()
    # Read every region back.
    with a.output.open("rb") as f:
        for data, sector in ((loader, IDBLOADER_START), (uboot, UBOOT_START), (fat, BOOT_START)):
            f.seek(sector * 512)
            if f.read(len(data)) != data:
                raise ValueError("SD image differs at sector %d" % sector)


if __name__ == "__main__":
    main()
