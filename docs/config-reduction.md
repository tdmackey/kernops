# Reducing the kernel config

This page is an evaluation, made on 2026-11-13. It changes nothing. It says
what the `7.3-lts` kernel ships today, what we can remove for our targets, and
how to do it.

Our targets are cloud instances (GCP, AWS, Azure) and modern servers: x86-64
and arm64, UEFI and ACPI, NVMe, Ethernet and InfiniBand, GPUs for compute.

## What the kernel ships today

Measured on the amd64 build (`scripts/config-surface.py 7.3-lts <linux-modules .deb>`).
The config is Ubuntu's generic config plus what the cloud kernels add.

| | |
|---|---|
| Options set to `y` | 3,431 |
| Options set to `m` | 6,730 |
| Modules | 6,920 |
| Modules package | 171 MB (152 MB of modules) |

`kernel/lines/7.3-lts/config/surface.tsv` puts each module in a group and
gives each group a verdict. The result:

| Verdict | Modules | Size | Meaning |
|---|---|---|---|
| drop | 4,100 (59%) | 90 MB (59%) | No target has this hardware or use. |
| review | 1,211 (18%) | 11 MB (7%) | Depends on the hardware that we deploy. |
| keep | 1,609 (23%) | 50 MB (33%) | A server or a cloud instance needs it. |

The largest "drop" groups:

| Group | Modules | Size |
|---|---|---|
| Embedded, laptop and desktop devices (sensors, flash, batteries, backlights, joysticks, laptop platform drivers) | 1,587 | 14 MB |
| Media (TV, cameras, radio) | 736 | 15 MB |
| Sound | 679 | 15 MB |
| Wi-Fi drivers | 203 | 15 MB |
| Desktop and mobile GPUs (i915, amdgpu, radeon, nouveau, xe) | 30 | 14 MB |
| Legacy buses and devices (ISDN, ATM, PCMCIA, parallel port, FireWire) | 272 | 3 MB |
| Legacy and embedded network drivers | 164 | 3 MB |
| USB gadgets and device classes | 156 | 2 MB |
| Legacy filesystems | 30 | 3 MB |

What we keep in those areas is listed first in `surface.tsv`: BMC and VM
display adapters, server and cloud NICs, server HBAs, the RDMA drivers of
servers and clouds, USB host and serial consoles, and the Intel and AMD server
CPU features under `drivers/platform/x86`.

## Limits of this measurement

- **amd64 only.** There is no arm64 build yet. On arm64 the "review" group is
  much larger: most of `drivers/soc`, `pinctrl`, `clk` and `regulator` is for
  embedded SoC families. An ACPI server needs almost none of it. Expect a
  larger reduction on arm64 than on amd64.
- **Modules only.** The 3,431 built-in options are not classified. Most of the
  gain there is on arm64 (the `ARCH_*` platform options).
- **By path, not by use.** A group is a set of directories. The exceptions in
  `surface.tsv` came from reading the lists, not from a hardware inventory.
- **No build with a reduced config was made.** The numbers are what the rows
  select, not the result of a build.

## What we gain

- **Fewer CVEs to assess.** `kpm cves --used-files` already marks a CVE whose
  code we do not build. A smaller config makes more of them not apply.
- **A smaller interface for out-of-tree modules to depend on**, and fewer
  changes of the kernel ABI.
- **A shorter build**, and a modules package of about 70 MB in place of 171 MB.
- **A smaller initramfs and image.**

The boot time and the memory use of a running system change little: a module
that is not loaded costs disk space only.

## How to do it

Do not turn off thousands of options one by one. Kconfig has a switch for most
of these areas, and the options below it then go away. The switches go in
`kernel/lines/7.3-lts/config/local.toml`, which already holds our overrides.

### Step 1: areas that no target has (low risk)

About 55 switches. Each one removes a complete area.

| Area | Switches to set to `n` |
|---|---|
| Media | `MEDIA_SUPPORT`, `RC_CORE`, `MEDIA_CEC_SUPPORT` |
| Sound | `SOUND` |
| Wireless | `WLAN`, `CFG80211`, `MAC80211`, `BT`, `NFC`, `RFKILL`, `IEEE802154`, `6LOWPAN`, `WWAN` |
| Desktop GPUs | `DRM_I915`, `DRM_XE`, `DRM_AMDGPU`, `DRM_RADEON`, `DRM_NOUVEAU`, `DRM_GMA500` |
| Embedded devices | `IIO`, `MTD`, `W1`, `GNSS`, `COMEDI`, `GREYBUS`, `MOST`, `SLIMBUS`, `SOUNDWIRE`, `SIOX`, `IPACK_BUS`, `RAPIDIO`, `FPGA` |
| Laptop and desktop | `CHROME_PLATFORMS`, `SURFACE_PLATFORMS`, `MACINTOSH_DRIVERS`, `INPUT_JOYSTICK`, `INPUT_TOUCHSCREEN`, `INPUT_TABLET`, `GAMEPORT`, `ACCESSIBILITY`, `AUXDISPLAY`, `MEMSTICK` |
| Legacy buses | `PCCARD`, `PARPORT`, `FIREWIRE`, `ATM`, `CAN`, `WAN`, `SSB`, `BCMA`, `PATA_LEGACY` |
| Other | `STAGING`, `USB_GADGET`, `ANDROID_BINDER_IPC` |
| Filesystems | `JFS_FS`, `GFS2_FS`, `OCFS2_FS`, `NILFS2_FS`, `F2FS_FS`, `HFS_FS`, `HFSPLUS_FS`, `MINIX_FS`, `UFS_FS`, `AFFS_FS`, `ADFS_FS`, `BEFS_FS`, `QNX4FS_FS`, `OMFS_FS` |

Before you set a switch, make sure that nothing we keep depends on it:

- `SOUND`: the HDMI audio of a GPU needs it. This matters only if a GPU drives
  a display.
- `RFKILL`: some server platform drivers select it.
- `FB` and `BACKLIGHT_CLASS_DEVICE` are not in the table. The text console on a
  BMC video adapter uses the frame buffer layer. Remove only the old `FB_*`
  drivers, in step 2.
- `HID_SUPPORT` and `USB_SERIAL` stay: a KVM-over-IP keyboard and a serial
  console adapter need them.

### Step 2: inside the areas that we keep (needs the hardware list)

Reduce the vendor lists to what we deploy: Ethernet vendors, SCSI adapters,
RDMA cards, `hwmon` and `platform/x86` drivers, old `FB_*` drivers, the rare
network protocols (`TIPC`, `RDS`, `AF_RXRPC`). This needs an inventory of the
machines and instance types. A wrong guess here gives a machine with no
network or no disk, so do it from a list, not from memory.

### Step 3: arm64 platforms

Turn off the `ARCH_*` SoC families that are not servers, and with them most of
`drivers/soc`, `pinctrl`, `clk`, `regulator` and `phy`. Keep what the NVIDIA,
Ampere and cloud arm64 servers need. Do this after the first arm64 build
exists, so that it can be measured and booted.

## How to make sure that nothing necessary is lost

1. Run `scripts/config-surface.py` before and after. The "keep" group must not
   become smaller.
2. Build, then run `scripts/install-test.sh`. It boots the kernel and loads
   the storage, network, DOCA and NVIDIA modules.
3. Collect the list of loaded modules (`lsmod`) from one machine of each kind
   that we run: each cloud's instance types, and each server model. Every
   module in those lists must be in the new build. This is the real test, and
   we do not have these lists yet.
4. Compare the two `sbom.used-files.txt` files. They show which source files
   left the build.

## Recommendation

Do step 1 now: it is about 55 lines in `local.toml`, it removes more than half
of the modules, and the install test covers what could go wrong. Do steps 2
and 3 when we have the module lists from real machines and an arm64 build.
