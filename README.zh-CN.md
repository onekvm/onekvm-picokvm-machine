# onekvm-picokvm-machine

[English](README.md) | 简体中文

Luckfox PicoKVM（Rockchip RV1106，ARMv7）的机型文件，由 `onekvm-distro`
（`meta-luckfox/meta-picokvm`，配方 `onekvm-machine-picokvm`）构建。

## 软件包

| 软件包 | 文件 | 内容 |
| --- | --- | --- |
| `onekvm-picokvm-init` | `init` | initramfs `/init`：接着 U-Boot 的启动画面推进进度条，执行 OneKVM Recovery 检查，挂载 U-Boot 所选槽位的根分区并启动 systemd |
| `onekvm-boot-splash` | `boot-splash/` | 屏幕上的启动画面、进度条及其动画，供 initramfs、根系统和屏幕服务使用；Recovery 和安装画面 |
| `onekvm-boot-progress` | `boot-splash/systemd/` | 根系统启动过程中推进进度条的单元 |
| `onekvm-machine-picokvm` | `machine`、`machine.json`、`edid/`、`picokvm-prepare-storage`、`boot.mount`、`systemd/`、`picokvm-clear-boot-retry` | 机型标识；新卡上 initramfs 用 `picokvm-prepare-storage` 创建并格式化 userdata；`/boot`；正常启动后清零 Recovery 启动计数 |

`build-sd.py` 为 `onekvm-picokvm-sd` 配方生成 SD 镜像。

## 启动

| 阶段 | 屏幕 |
| --- | --- |
| OneKVM U-Boot（`onekvm-picokvm-uboot`） | 画出带空进度条的启动画面，读完槽位启动镜像填到 2%，启动 Linux 前填到 4%；给屏幕节点加 `onekvm,boot-logo`，并把 `rauc.slot` 设为所加载的槽位 |
| Linux `fb_st7789v` | 保留 U-Boot 的画面：不复位屏幕，不发送空白首帧 |
| initramfs `init` | 以 `initramfs` 进度画出启动画面并启动 `onekvm-boot-splash animate`；修复断电留下脏标记的启动分区，新写的卡先安装根槽，执行 [Recovery](#recovery)；以 SquashFS 只读挂载 A 槽根（`mmcblk1p2`）或 B 槽根（`mmcblk1p3`），上面叠加 upper 在 userdata（`mmcblk1p4`）上的 overlay，标记 `storage` 后 `switch_root` |
| `onekvm-boot-progress-*.service` | 依次标记 `early`、`sysinit`、`basic` |
| 屏幕服务 | 标记 `screen`，运行 `onekvm-boot-splash finish` 后画出第一帧 |

没有 `rauc.slot=` 时 initramfs 启动 A 槽。卡不是下面的布局或根分区挂载失败时，
在串口打印原因，10 秒后重启。

## SD 卡

`build-sd.py` 为下面的卡布局生成安装镜像，单位为 512 字节扇区：

| 扇区 | 内容 |
| --- | --- |
| 0 | MBR |
| 64 | `idbloader.img`：RV1106 DDR 初始化程序和 SPL |
| 16384 | `u-boot.img` |
| 32768 | p1，FAT32，128 MiB：`boot-a.itb`、`boot-b.itb`、`boot-slot` 和 `recovery.zip` |
| 294912 | p2，A 槽根，256 MiB SquashFS |
| 819200 | p3，B 槽根，256 MiB SquashFS |
| 1343488 | p4，userdata，直到卡末尾 |

与 NanoKVM Go 相同，镜像只含 MBR、引导程序和 FAT 已用部分，约 90 MiB：p1 中有
`boot-a.itb`、`boot-slot`、`recovery.zip`、`onekvm/install-b`，最后是根镜像
`onekvm/root.squashfs`。写入镜像不碰两个根槽和 userdata。从 A 槽首次启动时显示安装画面，
把根镜像写入 p2 和 p3，从卡上读回校验后删除该文件；成功前 A 槽不启动，安装失败时先经
Recovery 检查计数再重启。随后把 `boot-a.itb` 复制为 `boot-b.itb`，读回校验后删除
`onekvm/install-b`；失败时 A 槽照常启动但没有回退槽，下次启动再复制。之后新卡由 initramfs
把 p4 加进分区表并格式化为 ext4。根镜像必须能和启动文件一起放进 FAT，约 115 MiB。

## 存储

| 路径 | 位置 |
| --- | --- |
| `/` | overlay：槽位只读 SquashFS 在下，上层是 userdata 的 `upper/`、`work/`，A/B 槽共用 |
| `/data` | userdata 的 upper 内 |
| `/mnt/storage` | userdata 的 `storage/` |
| `/media/userdata`、`/mnt/.rofs` | 整个 userdata 分区和槽位根 |
| `/boot` | p1（FAT32），读写，由 `boot.mount` 挂载 |

重新刷写 SD 镜像后 userdata 保留，设置、`/data` 和已装扩展都不丢。userdata 没有文件系统时
格式化为 ext4；是无法识别的文件系统时保持不动，此时改动只保留到下次重启。

## Recovery

与 NanoKVM Go 相同，initramfs 在挂载任何分区之前载入共用的 OneKVM Recovery hook
（`onekvm-nanokvm-recovery` 的 `/init.d/20-recovery`）。Recovery 校验全部四个分区，
所以没有 userdata 的卡先建立 userdata。

| 入口 | 条件 |
| --- | --- |
| 网页 | 系统设置中的 Recovery 写入 `/boot/nextboot-mode.txt` 后重启 |
| 内核命令行 | `onekvm_recovery=1`，例如在 U-Boot 提示符下临时设置 |
| 启动计数 | 连续 10 次启动都没有在 `onekvm.service` 运行的情况下满 2 分钟（`onekvm-boot-success.timer`） |

PicoKVM 没有 Linux 能读取的按键，因此没有按键入口。Recovery 在 USB 口上显示为
`3346:1009`（`OneKVM PicoKVM Recovery`），整张 SD 卡是一个可写的盘，另有 CDC ACM 控制台；
串口控制台为 `ttyS0`。gadget 要等 USB PHY 区分完电脑与充电器后才绑定（没有线缆时最多等
5 秒）：检测期间绑定会让 PHY 把电脑当作充电器并关闭端口。
p1 上有 `recovery.zip` 时，还在以太网上提供 Recovery 网页（80、443 端口），可重置网页用户、
恢复出厂设置和重启；PicoKVM 没有固件更新包，网页不提供固件上传。屏幕显示 Recovery 地址
和写卡方法。恢复出厂设置把 userdata 格式化为 ext4。

每次启动前，启动分区的脏标记（字节 0x41 的 bit 0，读写挂载时断电留下）置位时用
`fsck.fat -a` 修复；修复失败时，本次启动不计数，也不会自动进入 Recovery。

## 启动画面

`onekvm-boot-splash` 按屏幕本身的方向画在 240×240 RGB565 的 `fb_st7789v` 帧缓冲上，
版式（172 行）、颜色和进度条与 NanoKVM Go（`onekvm-nanokvm-go-machine`）一致。
`boot_stages[]` 中各检查点的百分比和时长按 PicoKVM 从 SD 卡启动的实测设定；
两个检查点之间动画向下一个检查点推进，某一步比平时慢时逐渐放缓。

`boot-splash/generate-assets.py`（Pillow、DejaVu Sans）由 `logo.png` 重新生成已提交的
`assets.h`；加 `--uboot-header PATH` 时还会生成 U-Boot 用的完整启动画面，即
`onekvm-u-boot/board/luckfox/picokvm/logo.h`。OE 构建和设备都不需要 Pillow 或字体。

开发机上可用 `--output FILE.ppm` 渲染启动画面。

## EDID 预设

`onekvm-machine-picokvm` 把 `edid/*.edid.bin` 安装到
`/usr/share/onekvm/edid/presets/`，与所有机型共用的 `factory.edid.bin`、
`1080p60.edid.bin`（`onekvm-distro` 的 `onekvm-machine-files.inc`）放在一起。
60 Hz 预设从 640×480 到 1920×1080，分 1:1、5:4、4:3、3:2、16:10、16:9、17:9、
21:9 和 32:9 几组，只收两种采集桥片都能接收的模式：TC358743 支持 640×350 起、
最高 165 MHz，四条 CSI 通道不够 1080p90，编码器最大 1920×1080。每个预设使用
独立的 PnP 产品号，Windows 不会沿用为其他预设缓存的 EDID。

`python3 tools/generate-edid.py` 可重新生成 `edid/`。`tools/edid_modes.py`
与 NanoKVM、NanoKVM Pro 的机型仓库共用，各份需保持一致。

## 测试

```
make test
```

检查帧缓冲格式、版式、进度条更新和动画、SD 布局、initramfs 的槽位选择与布局检查、userdata 创建以及各检查点的顺序。
