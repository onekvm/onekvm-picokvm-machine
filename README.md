# onekvm-picokvm-machine

English | [简体中文](README.zh-CN.md)

Machine files for the Luckfox PicoKVM (Rockchip RV1106, ARMv7), built by
`onekvm-distro` (`meta-luckfox/meta-picokvm`, recipe `onekvm-machine-picokvm`).

## Packages

| Package | Files | Content |
| --- | --- | --- |
| `onekvm-picokvm-init` | `init` | Initramfs `/init`: continues the U-Boot boot screen and keeps its bar moving, runs the OneKVM Recovery check, mounts the root of the slot U-Boot loaded and starts systemd |
| `onekvm-boot-splash` | `boot-splash/` | Boot screen, boot bar and its animator on the panel, for the initramfs, the root system and the screen service; the Recovery and installing screens |
| `onekvm-boot-progress` | `boot-splash/systemd/` | Units that advance the boot bar while the root system starts |
| `onekvm-machine-picokvm` | `machine`, `machine.json`, `edid/`, `picokvm-prepare-storage`, `boot.mount`, `systemd/`, `picokvm-clear-boot-retry` | Machine identity; the initramfs runs `picokvm-prepare-storage` to create userdata on a new card and format it; `/boot`; clears the Recovery boot counter after a good boot |

`build-sd.py` composes the SD image for the `onekvm-picokvm-sd` recipe.

## Boot

| Step | Panel |
| --- | --- |
| OneKVM U-Boot (`onekvm-picokvm-uboot`) | Draws the boot screen with an empty bar, fills 2% once the slot's boot image is read and 4% before Linux starts. Marks the panel node `onekvm,boot-logo` and sets `rauc.slot` to the slot it loaded |
| Linux `fb_st7789v` | Keeps the U-Boot screen: no panel reset, no empty first frame |
| Initramfs `init` | Draws the boot screen at `initramfs` and starts `onekvm-boot-splash animate`; repairs a boot partition left dirty by a power cut, installs the root slots from a newly written card, runs [Recovery](#recovery); mounts root A (`mmcblk1p2`) or root B (`mmcblk1p3`) read-only as SquashFS below an overlay whose upper is on userdata (`mmcblk1p4`), marks `storage` and runs `switch_root` |
| `onekvm-boot-progress-*.service` | Mark `early`, `sysinit` and `basic` |
| Screen service | Marks `screen`, runs `onekvm-boot-splash finish` and draws its first frame |

Without `rauc.slot=` the initramfs starts slot A. When the card does not
have the layout below or the root cannot be mounted, it prints the reason on
the serial console and restarts after 10 s.

## SD card

`build-sd.py` composes an install image for the card below; in 512-byte
sectors:

| Sector | Content |
| --- | --- |
| 0 | MBR |
| 64 | `idbloader.img`: the RV1106 DDR blob and the SPL |
| 16384 | `u-boot.img` |
| 32768 | p1, FAT32, 128 MiB: `boot-a.itb`, `boot-b.itb`, `boot-slot` and `recovery.zip` |
| 294912 | p2, root A, 256 MiB SquashFS |
| 819200 | p3, root B, 256 MiB SquashFS |
| 1343488 | p4, userdata up to the end of the card |

As on the NanoKVM Go, the image holds only the MBR, the loaders and the used
part of the FAT, about 90 MiB: p1 carries `boot-a.itb`, `boot-slot`,
`recovery.zip`, `onekvm/install-b` and, last, the root image
`onekvm/root.squashfs`. Writing it leaves both root slots and userdata
untouched. The first boot from slot A shows the installing screen, writes the
root image to p2 and p3, reads both back from the card and deletes the file;
slot A does not boot until this succeeds, and a failed install restarts after
the Recovery check has counted the boot. It then copies `boot-a.itb` to
`boot-b.itb`, reads it back and deletes `onekvm/install-b`; if that fails,
slot A boots without a fallback slot and the copy runs again at the next boot.
Next, on a new card, the initramfs adds p4 to the partition table and formats
it as ext4. The root image must fit on the FAT next to the boot files, about
115 MiB.

## Storage

| Path | Location |
| --- | --- |
| `/` | Overlay: the slot's read-only SquashFS below `upper/` and `work/` on userdata, shared by slots A and B |
| `/data` | Inside the userdata upper |
| `/mnt/storage` | `storage/` on userdata |
| `/media/userdata`, `/mnt/.rofs` | The whole userdata partition and the slot root |
| `/boot` | p1 (FAT32), read-write, mounted by `boot.mount` |

Writing the SD image again keeps userdata and with it settings, `/data` and
installed extensions. A userdata partition without a file system is formatted
as ext4; one with an unknown file system is left alone, and changes then last
only until the next restart.

## Recovery

The initramfs sources the shared OneKVM Recovery hook (`onekvm-nanokvm-recovery`,
`/init.d/20-recovery`) before it mounts any partition, as on the NanoKVM Go.
A card without userdata gets it first, since Recovery checks all four
partitions.

| Entry | Condition |
| --- | --- |
| Web UI | Recovery in the system settings writes `/boot/nextboot-mode.txt` and restarts |
| Kernel command line | `onekvm_recovery=1`, e.g. set at the U-Boot prompt |
| Boot counter | Ten boots in a row that did not run two minutes with `onekvm.service` active (`onekvm-boot-success.timer`) |

PicoKVM has no button that Linux can read, so there is no button entry.
Recovery shows on the USB port as `3346:1009` (`OneKVM PicoKVM Recovery`)
with the whole SD card as a writable drive and a CDC ACM console; the serial
console is `ttyS0`. The gadget binds only after the USB PHY has finished
telling a computer from a charger (at most 5 s without a cable): bound during
that check, the PHY takes the computer for a charger and switches the port
off. With
`recovery.zip` on p1 it also serves the Recovery web page on Ethernet
(ports 80 and 443) for resetting the Web user, factory reset and restart;
PicoKVM has no firmware update package, so the page offers no firmware
upload. The screen shows the Recovery addresses and how to write the card.
A factory reset formats userdata as ext4.

Before any boot, a boot partition whose dirty flag is set (byte 0x41, bit
0, left by a power cut while mounted read-write) is repaired with
`fsck.fat -a`; if that fails, this boot is neither counted nor sent to
Recovery automatically.

## Boot screen

`onekvm-boot-splash` draws on the 240×240 RGB565 `fb_st7789v` framebuffer in
the panel's own orientation, with the same 172-row layout, colors and bar as
the NanoKVM Go (`onekvm-nanokvm-go-machine`). The checkpoint percentages and
durations in `boot_stages[]` follow a PicoKVM boot from SD; between two
checkpoints the animator moves the bar toward the next one and slows down
when a step takes longer than usual.

`boot-splash/generate-assets.py` (Pillow, DejaVu Sans) regenerates the
checked-in `assets.h` from `logo.png`. With `--uboot-header PATH` it also
writes the full boot screen for U-Boot as
`onekvm-u-boot/board/luckfox/picokvm/logo.h`. OE and the device need neither
Pillow nor fonts.

`--output FILE.ppm` renders the boot screen on the development host.

## EDID presets

`onekvm-machine-picokvm` installs `edid/*.edid.bin` in
`/usr/share/onekvm/edid/presets/`, next to the `factory.edid.bin` and
`1080p60.edid.bin` every machine shares (`onekvm-machine-files.inc` in
`onekvm-distro`). The 60 Hz presets run from 640×480 to 1920×1080 in the
1:1, 5:4, 4:3, 3:2, 16:10, 16:9, 17:9, 21:9 and 32:9 groups, limited to what
both capture bridges take: TC358743 accepts 640×350 up to 165 MHz, its four
CSI lanes stop below 1080p90, and the encoder takes at most 1920×1080. Each
preset has its own PnP product ID, so Windows does not reuse an EDID it cached
for another preset.

`python3 tools/generate-edid.py` regenerates `edid/`. `tools/edid_modes.py`
is shared with the NanoKVM and NanoKVM Pro machine repositories; keep the
copies identical.

## Tests

See [platform integration and validation](PLATFORM-VALIDATION.md) for kernel
overrides, watchdog policy, thermal protection, SSH PTY sessions and device
checks.

```
make test
```

Checks the framebuffer format, layout, bar updates and animator, the SD
layout, the initramfs slot choice and layout check, userdata creation and the
order of the boot checkpoints.
Set `ONEKVM_PICOKVM_KERNEL` to a kernel checkout to also run offline OTP and
stable-MAC helper tests with mocked hardware and hashing.
