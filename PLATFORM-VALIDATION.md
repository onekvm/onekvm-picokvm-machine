# PicoKVM platform integration

## Stable Ethernet identity

The RV1106 OTP provider exposes only the first 64 bytes of the read-only
public window. CPU-id is the 16 bytes at offset `0x0a`. Reads use ECC-enabled
two-byte words, including unaligned reads, following the compatible read
sequence and clock/reset names in Rockchip sources.
No bootloader environment variables are read.

PicoKVM's GMAC consumes that cell as `cpu-id`. Without a valid explicit DT
MAC, it takes SHA-256(CPU-id)'s first six bytes and changes the first byte to
`(byte & 0xfc) | 0x02` (local unicast). This happens before kernel DHCP/NFS
root, not just in userspace. Other boards without this cell are unchanged.
Valid explicit DT MACs take precedence. OTP failures deliberately prevent
Ethernet probe rather than silently returning to a random identity.

```sh
dd if=/sys/bus/nvmem/devices/rockchip-otp0/nvmem bs=1 skip=10 count=16 2>/dev/null | od -An -tx1
dd if=/sys/bus/nvmem/devices/rockchip-otp0/nvmem bs=1 skip=10 count=16 2>/dev/null | sha256sum
cat /sys/class/net/eth0/address /sys/class/net/eth0/addr_assign_type
dmesg | grep -Ei 'otp|stmmac|dwmac|ethernet'
```

Expect 16 CPU-id bytes that are neither all zero nor all `ff`, a MAC matching
the digest/mask rule, assignment type `0`, and no OTP timeout/probe failure.
Compare at least two cold boots: the address must stay identical. Also boot
the existing NFSv3 rescue path: `findmnt -n -o FSTYPE /` must report `nfs`
and kernel DHCP must work before userspace.

## R7 config and distro overrides

The fragment removes unrelated ARM platforms, SMP, unrelated Rockchip
clocks, PCI/SATA/flash, host USB controllers, DRM, host HID, absent PMICs and
controllers, wireless/CAN/NFC, and unused filesystems/protocols. IPv6,
optional storage/VPN modules and legacy/nft firewall facilities remain.
The host USB core stays enabled for the distro's optional `USB_STORAGE=m`
package checks, but no PicoKVM host controller is enabled. Capture,
ISP/VEPU/KMPP, RGA, crypto, panel, touch and DMA/CMA implementations remain.

Keep `CONFIG_IP_PNP_RARP`, `CONFIG_DWMAC_GENERIC`, `CONFIG_SPI_ROCKCHIP_SFC`,
`CONFIG_MTD` and `CONFIG_MTD_SPI_NOR` disabled in the PicoKVM NFS fragment.

The later `do_configure_storage_modules` task restores NFSv4/NFSD from the
shared storage fragment. In the PicoKVM-only recipe append, use
`scripts/config --file ${B}/.config --disable NFS_V4 --disable NFSD` after
that merge, then rerun `olddefconfig` with the existing OE toolchain arguments.
`do_configure_storage_modules:append:onekvm-picokvm()` can do this. Do not
change other machines' shared fragments. XFS/Btrfs staying optional `=m`
does not add their text to the resident kernel. NFSv3/root-NFS stay built in.

Verify the final target config, not only the source fragment:

| Expected | Symbols |
| --- | --- |
| Built in | `ARCH_ROCKCHIP`, `CLK_RV1106`, `STMMAC_ETH`, `DWMAC_ROCKCHIP`, `NVMEM_ROCKCHIP_OTP`, `NVMEM_LAYOUTS`, `HW_RANDOM_ROCKCHIP` |
| Built in | `NFS_FS`, `NFS_V3`, `ROOT_NFS`, `IP_PNP_DHCP`, `EXT4_FS`, `OVERLAY_FS`, `SQUASHFS` |
| Built in | `USB_DWC3_GADGET`, `USB_CONFIGFS_F_HID`, `USB_CONFIGFS_MASS_STORAGE`, `USB_CONFIGFS_F_FS`, `USB_CONFIGFS_ECM`, `USB_CONFIGFS_NCM` |
| Built in | `VIDEO_ROCKCHIP_CIF`, `ROCKCHIP_KMPP`, `VIDEO_ROCKCHIP_RGA`, `CRYPTO_DEV_ROCKCHIP_V3`, `FB_TFT_ST7789V`, `TOUCHSCREEN_HYNITRON_CST816X` |
| Built in | `CGROUPS`, `CGROUP_SCHED`, `CGROUP_PIDS`, `CGROUP_DEVICE`, `FHANDLE`, `INOTIFY_USER`, `EPOLL`, `SIGNALFD`, `TIMERFD`, `UNIX98_PTYS`, `SECCOMP_FILTER` |
| Available modules | `F2FS_FS`, `BLK_DEV_NBD`, `ZRAM`, `CIFS`, `NF_TABLES`, `NFT_CT`, `NFT_COMPAT`, `NETFILTER_XTABLES` |
| Enabled | `NF_TABLES_INET=y` |
| Disabled | Other ARM platforms, `SMP`, `PCI`, `ATA`, `MTD`, `DRM`, `USB_XHCI_HCD`, `USB_EHCI_HCD`, `USB_OHCI_HCD`, `NFS_V4`, `NFSD` |

After installing matching optional modules, run:

```sh
systemctl is-active onekvm.service
systemctl --failed
findmnt / /mnt/.rofs /media/userdata /mnt/storage /data
ls /sys/class/udc
ls /sys/kernel/config/usb_gadget/*/functions
ls /dev/fb0 /dev/input/event* /dev/video* /dev/onekvm-crypto-offload
modprobe f2fs
modprobe nbd
grep -E 'ext4|f2fs|overlay|squashfs' /proc/filesystems
cat /proc/swaps
nft add table inet onekvm_r7_probe && nft delete table inet onekvm_r7_probe
```

Expect active Core, no new failed units, an overlay root on the SquashFS slot with
its upper on ext4 userdata, `/data` inside that upper,
a UDC and configured gadget functions, panel/touch/video/crypto nodes,
successful module loads and active zram swap. The nft command must exit zero
without output and leave existing rules untouched. Test the actual firewall
extension too. Verify HID/virtual media/ECM/NCM enumeration on the connected
host; exercise capture/encode, resolution switches, RGA, panel and touch.

## Watchdog and thermal

PicoKVM enables its existing RV1106 DesignWare watchdog DT node; driver and
sysfs support are built in. Add a PicoKVM-only distro manager drop-in:

```ini
[Manager]
RuntimeWatchdogSec=30s
```

Apply through the normal image/reboot path, then run:

```sh
cat /sys/class/watchdog/watchdog0/identity
cat /sys/class/watchdog/watchdog0/state /sys/class/watchdog/watchdog0/timeout
systemctl show -p RuntimeWatchdogUSec -p WatchdogDevice
journalctl -b --no-pager | grep -i watchdog
```

Expect a DesignWare watchdog, `active`, a positive hardware timeout (possibly
rounded), and manager interval `30s`. Check that the system stays up for at least twice the hardware timeout.

**Thermal.** The kernel ports the vendor RV1106 TSADC as
`rockchip,rv1106-tsadc`: the VO GRF TSEN and analog sequence, `Q_MAX`
0x400, power-down flow control, `AUTO_Q_SEL_EN` and the vendor code table.
The sensor requires `AUTO_Q_SEL_EN` for correct readings. Hardware TSHUT resets the SoC through the CRU at 120 C.
`soc-thermal` throttles the CPU through cpufreq from 85 C (2 C hysteresis)
and its 115 C critical trip reboots (`critical-action = "reboot"`): a halted
board cannot power itself back on. The CPU node is `cpu@0`, matching the
A7's MPIDR affinity, so cpufreq-dt and cacheinfo bind. The default governor
is `performance`: U-Boot leaves the A7 at 1.2 GHz while the clock tree reads
816 MHz, and throttling must return to 1.2 GHz, not a real 816 MHz.

```sh
for zone in /sys/class/thermal/thermal_zone*; do cat "$zone/type" "$zone/temp"; done
for device in /sys/class/thermal/cooling_device*; do cat "$device/type" "$device/cur_state"; done
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
```

Expect `soc-thermal`, four `cpufreq-cpu0` cooling states and the
`performance` governor at 1.2 GHz below the throttling threshold. Core reads
the zone through `rv1106-temperature.json` in `/usr/share/onekvm/device-backends`.

## Lazy CMA

Retained lookup walks the same first 64 entries under the same spinlock,
chooses the smallest acceptable block within the same slack, prefers the
newest equal-size release, and keeps the first entry on timestamp ties.
Selection uses one best pointer under the spinlock.

Check that the arm32 module build no longer warns about the
`lazy_cma_take_retained` frame. Record
`cat /sys/devices/platform/onekvm-lazy-cma/stats` before and after repeated
video start/stop. Expect no new failures, OOMs or kernel warnings. Compatible repeats should increase `hits` when retention is enabled.

## Dropbear PTY sessions

Dropbear 2026.94 used the PTY master as both channel descriptors. An EOF from
the client closed the write side and with it the master, so
`ssh -tt host cmd </dev/null` hung up the command's terminal before it ran.
The distro's Dropbear append writes through a duplicate of the master, as
OpenSSH does; EOF then closes only the duplicate.

```sh
ssh -tt root@PicoKVM 'tty; exec 3<>/dev/tty; echo ctty-ok' </dev/null
sleep 10 | ssh -tt root@PicoKVM 'tty; exec 3<>/dev/tty; echo ctty-ok'
```

Both print `/dev/pts/N` and `ctty-ok`.

## Host tests

`ONEKVM_PICOKVM_KERNEL=/path/to/linux PYTHONDONTWRITEBYTECODE=1 make test`
also tests extracted OTP/MAC helpers: all aligned/unaligned ranges in the
public window, ECC errors, timeout cleanup, MAC gating, explicit precedence,
deferral/read errors, blank/short ids and every first digest byte. Hardware
and SHA-256 are mocked; this cannot establish OTP hardware correctness.
