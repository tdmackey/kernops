# Reducing the kernel config

This page says what the `7.3-lts` kernel shipped before the reduction, what
we removed, what we can still remove for our targets, and how to do it. Step 1
is done (2026-11-13). Steps 2 and 3 are not.

Our targets are cloud instances (GCP, AWS, Azure) and modern servers: x86-64
and arm64, UEFI and ACPI, NVMe, Ethernet and InfiniBand, GPUs for compute.

## Result of step 1

Measured on the amd64 build for noble, before and after.

| | Before | After |
|---|---|---|
| Options set to `y` | 3,431 | 2,985 |
| Options set to `m` | 6,730 | 3,788 |
| Modules | 6,920 | 3,847 |
| Modules package | 171 MB | 98 MB |
| Modules by verdict: drop | 4,100 | 1,097 |
| Modules by verdict: review | 1,211 | 1,158 |
| Modules by verdict: keep | 1,609 | 1,592 |

The build passes, and `scripts/install-test.sh 7.3-lts noble` passes with the
DOCA and NVIDIA modules built against the new kernel.

The "keep" group lost 17 modules. Each one was in a directory that we keep,
and depends on an area that step 1 turned off:

- PCMCIA, parallel port and FireWire: `sdricoh_cs`, `sl811_cs`, `pps_parport`,
  `parkbd`, `sbp_target`
- SLIMbus, SoundWire, camera bus (SCCB) and RapidIO: `regmap-slimbus`,
  `regmap-sdw`, `regmap-sdw-mbq`, `regmap-sccb`, `rionet`
- USB gadget mode: `9pnet_usbg`, `usbip-vudc`
- CAN, ChromeOS, sensors, 3G modems: `em_canid`, `cros_ec_ucsi`,
  `rtc-hid-sensor-time`, `hso`
- `qt2025` is different. It is a network PHY driver written in Rust, and the
  noble build has no Rust (`rustc` 1.75; the kernel needs 1.82). The config did
  not remove it. The resolute build has it.

What "drop" still has (1,097 modules): the areas that step 1 left on purpose
(`MTD`, `FPGA`, i915, xe and amdgpu) and the vendor lists of step 2.

## What the kernel shipped before

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
- **noble only.** The reduced config was built and tested for noble. The
  config is the same for resolute, but that build was not made again.

## What we gain

- **Fewer CVEs to assess.** `kpm cves --used-files` already marks a CVE whose
  code we do not build. A smaller config makes more of them not apply.
- **A smaller interface for out-of-tree modules to depend on**, and fewer
  changes of the kernel ABI.
- **A shorter build**, and a smaller modules package: 98 MB in place of 171 MB after step 1,
  about 70 MB after step 2.
- **A smaller initramfs and image.**

The boot time and the memory use of a running system change little: a module
that is not loaded costs disk space only.

## How to do it

Do not turn off thousands of options one by one. Kconfig has a switch for most
of these areas, and the options below it then go away. The switches go in
`kernel/lines/7.3-lts/config/local.toml`, which already holds our overrides.

### Step 1: areas that no target has (done)

60 switches in `local.toml`. Each one removes a complete area.

| Area | Switches to set to `n` |
|---|---|
| Media | `MEDIA_SUPPORT`, `RC_CORE`, `MEDIA_CEC_SUPPORT` |
| Sound | `SOUND` |
| Wireless | `WLAN`, `CFG80211`, `MAC80211`, `BT`, `NFC`, `RFKILL`, `IEEE802154`, `6LOWPAN`, `WWAN` |
| Old and unused GPUs | `DRM_RADEON`, `DRM_NOUVEAU`, `DRM_GMA500` |
| Embedded devices | `IIO`, `W1`, `GNSS`, `COMEDI`, `GREYBUS`, `MOST`, `SLIMBUS`, `SIOX`, `IPACK_BUS`, `RAPIDIO` |
| Laptop and desktop | `CHROME_PLATFORMS`, `SURFACE_PLATFORMS`, `MACINTOSH_DRIVERS`, `INPUT_JOYSTICK`, `INPUT_TOUCHSCREEN`, `INPUT_TABLET`, `GAMEPORT`, `ACCESSIBILITY`, `AUXDISPLAY`, `MEMSTICK` |
| Legacy buses | `PCCARD`, `PARPORT`, `FIREWIRE`, `ATM`, `CAN`, `WAN`, `PATA_LEGACY` |
| Other | `STAGING`, `USB_GADGET`, `ANDROID_BINDER_IPC` |
| Filesystems | `JFS_FS`, `GFS2_FS`, `OCFS2_FS`, `NILFS2_FS`, `F2FS_FS`, `HFS_FS`, `HFSPLUS_FS`, `MINIX_FS`, `UFS_FS`, `AFFS_FS`, `ADFS_FS`, `BEFS_FS`, `QNX4FS_FS`, `OMFS_FS` |

Left on, on purpose, until the hardware list says that they can go:

- `DRM_I915`, `DRM_XE`, `DRM_AMDGPU`: a server can have one of these GPUs.
- `MTD` (the firmware flash of a server) and `FPGA` (accelerator cards).
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

1. Run `scripts/config-surface.py` before and after. Look at each module that
   the "keep" group loses: it must depend on an area that you turned off.
2. Build, then run `scripts/install-test.sh`. It boots the kernel and loads
   the storage, network, DOCA and NVIDIA modules.
3. Collect the list of loaded modules (`lsmod`) from one machine of each kind
   that we run: each cloud's instance types, and each server model. Every
   module in those lists must be in the new build. This is the real test, and
   we do not have these lists yet.
4. Compare the two `sbom.used-files.txt` files. They show which source files
   left the build.

## Recommendation

Step 1 is done. Do steps 2 and 3 when we have the module lists from real
machines and an arm64 build. Before a release with the reduced config, do
check 3 above: compare the `lsmod` lists of real machines with the new build.
