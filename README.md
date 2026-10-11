# toaster

Create Windows 11 install USB drives on Linux, with the intrusive parts taken out.

toaster is inspired by [Rufus](https://rufus.ie)'s "Windows User Experience" options, which
it makes available on Linux: skip the TPM / Secure Boot / RAM checks, skip the
Microsoft-account requirement, opt out of data collection, and strip the bundled extras.
It is a Python (3.10+) command line tool with no Python dependencies. It drives open-source
tools (`wimlib`, `hivex`, `syslinux`, `mtools`) plus standard disk utilities.

## Install

```sh
# Debian/Ubuntu (package names differ elsewhere)
sudo apt install wimtools libhivex0 p7zip-full gdisk dosfstools util-linux mtools syslinux syslinux-common
pip install .            # or run in place:  PYTHONPATH=src python3 -m toaster --help
```

`syslinux` and `mtools` are only needed for `--boot bios` and for writing disk images.

## Use

```sh
toaster drives                              # list USB drives
toaster inspect Win11.iso                   # editions, build, whether install.wim needs splitting
toaster tweaks                              # everything you can switch on

# Write a stick (needs root). Shows what it will do and asks you to retype the device path.
sudo toaster write Win11.iso /dev/sdb --preset debloat --username alex --verify

# Everything at once: legacy BIOS too, one edition only, tweaks baked into the image,
# extra storage drivers, 2023-signed UEFI loaders.
sudo toaster write Win11.iso /dev/sdb --boot bios --edition 6 --preset debloat --offline-debloat \
     --drivers ~/drivers/intel-vmd --secureboot-2023 --verify

# Skip Setup's language / EULA / edition / account pages (you still pick the disk):
sudo toaster write Win11.iso /dev/sdb --silent --install-edition 6 --username alex

# No USB stick? Write a bootable disk image for a VM (no root needed)...
toaster write Win11.iso win11.img --image-size 16G --boot bios --preset debloat
qemu-system-x86_64 -enable-kvm -m 8G -drive file=win11.img,format=raw ...

# ...or just produce the customized file tree, or the answer file:
toaster prepare Win11.iso --out ./win11-tree --preset debloat
toaster unattend --preset debloat -o autounattend.xml
```

Options can be saved and reused: `--save-profile my.json` / `--profile my.json`.

| Preset | What it does |
|---|---|
| `minimal` (default) | Bypass TPM / Secure Boot / RAM checks, skip the Microsoft-account requirement |
| `debloat` | `minimal` + opt out of privacy toggles, no automatic BitLocker, and every tweak marked `*` in `toaster tweaks` |
| `none` | Nothing; add options individually |

| Flag | Effect |
|---|---|
| `--boot uefi` (default) | GPT + FAT32. UEFI only. |
| `--boot bios` | MBR + FAT32 with syslinux boot code. Boots legacy BIOS **and** UEFI. |
| `--bypass tpm,secureboot,ram,cpu,storage,disk` / `--no-bypass` | Which hardware checks Setup skips |
| `--[no-]local-account`, `--username NAME` | Skip the Microsoft-account requirement / pre-create a local admin |
| `--no-data-collection`, `--disable-bitlocker`, `--offline-internal-drives` | Privacy, encryption, disk-safety options |
| `--timezone`, `--locale` | Regional settings |
| `--tweak ID` | Enable one tweak (repeatable) |
| `--offline-debloat` | Apply tweaks inside `install.wim` (see below) |
| `--edition N` | Keep only these install images (repeatable; see `toaster inspect`) |
| `--drivers DIR` | Folder of extracted drivers for Setup and the new system (repeatable) |
| `--secureboot-2023` | Use the "Windows UEFI CA 2023" signed boot loaders |
| `--silent` | Answer the language, EULA, product-key, edition and account pages. **You still choose the disk.** |
| `--install-edition N` | Which install image `--silent` deploys (needed if the image has several; renumbering after `--edition` is handled) |
| `--auto-partition` | **Dangerous.** With `--silent`, also erase disk 0 of the PC you install on. See below. |

## How it works

1. The ISO is extracted to a staging folder (the ISO and your files are never modified).
2. The `LabConfig` bypass values are written into the registry hive inside `boot.wim`
   (the "Microsoft Windows Setup" image); `sources/appraiserres.dll` is emptied so in-place
   upgrades skip the appraisal too. If the hive cannot be patched, the same values go into the
   answer file instead.
3. **Drivers** (`--drivers`): copied into `boot.wim` at `X:\Drivers` and listed in the answer
   file's `PnpCustomizationsWinPE` section, so Setup sees storage/network devices before the
   disk-selection screen. The same files go to `sources\$OEM$\$1\Drivers`, which Setup copies
   to the new system, where `pnputil` installs them during the specialize pass (the folder is
   removed at first logon unless `--keep-drivers`). DISM's private driver database cannot be
   reproduced safely from Linux, so this uses the documented answer-file route.
   Drivers must be extracted (`.inf` + `.sys` + `.cat`) and signed.
4. **Offline debloat** (`--offline-debloat`): instead of running `reg add` at first boot, each
   selected edition's `SOFTWARE`, `SYSTEM` and Default User (`NTUSER.DAT`) hives are edited
   inside `install.wim`. Per-user tweaks therefore apply to *every* account created later, not
   just the first one to log on. It is slower because the large image is rewritten. If
   anything goes wrong it falls back to first-boot commands. `OneDriveSetup.exe` is deleted
   from the image. **AppX packages (Outlook, Teams) are still removed at install time, not
   offline**: their state also lives in a database inside the image that cannot be kept
   consistent from Linux, and getting that wrong risks a broken Start menu.
5. **Editions** (`--edition`): unwanted editions are exported out of `install.wim`
   (`install.esd` is converted to `.wim` first). Smaller image, shorter edition list.
6. An answer file is generated for everything else and placed where Setup reads it
   (`Autounattend.xml` in `boot.wim` when a Windows PE setting is needed, otherwise
   `sources\$OEM$\$$\Panther\unattend.xml`). All `boot.wim` changes happen in one rewrite.
7. **2023 boot loaders** (`--secureboot-2023`): the `EFI_EX` / `Fonts_EX` files carried in
   `boot.wim` replace `efi/boot/boot*.efi`, `bootmgr.efi` and the boot fonts (Microsoft KB5025885).
   Needs reasonably recent media, and a machine whose firmware trusts the 2023 certificate.
8. `install.wim` over 4 GiB is split into `.swm` parts so it fits on FAT32.
9. The drive is wiped, partitioned, formatted FAT32, and the files are copied (and, with
   `--verify`, read back and checksummed). With `--boot bios` the MBR and partition boot sector
   come from syslinux, and syslinux's `chain.c32` starts Windows' `bootmgr`.

### Silent install

`--silent` is safe: it fills in the answers Setup would otherwise ask for, but it never names a
disk, so Setup still shows "Where do you want to install Windows?" and nothing is erased until
you choose. It implies the privacy opt-outs and hides the OOBE network and account pages (add
`--username` to create the local account, or Windows will ask you for one). The language is read
from the Setup image (`--locale` overrides it).

`--auto-partition` goes further: Setup creates an EFI / MSR / Windows (NTFS) layout on **disk 0 of
the PC you boot** and installs there without asking. It is opt-in, needs `--silent`, only works
with `--boot uefi`, and `toaster write` makes you type `erase disk 0` unless you pass `--yes`.
A safeguard stops it when the install drive is not disk 1: the answer file first relabels
partition 1 of disk 1 (the install drive), and if that step fails Setup falls back to its normal
disk page. That protects against the worst case, a missing storage driver leaving the install
drive as the only disk Setup can see. It does **not** protect you if the PC has several internal
disks or if Windows numbers the install drive as disk 0. **Disconnect disks you care about, and try
it in a VM first.**

Safety: toaster refuses any disk holding a mounted system path (`/`, `/boot`, `/home`, ...)
even with `--allow-fixed`, refuses non-USB disks unless `--allow-fixed`, and requires you to
retype the device path before erasing anything.

## Status: what has and hasn't been verified

The automated suite (`PYTHONPATH=src python3 -m unittest discover -s tests -t .`, ~95 tests)
uses the real `wimlib`, `hivex`, `sgdisk`, `mkfs.vfat`, `mtools` and `syslinux`.

Verified:
- Answer-file generation (well-formed, correctly escaped, correct pass placement).
- Registry edits in `boot.wim` and `install.wim` hives, including preservation of existing values.
- Driver, edition, offline-tweak and 2023-loader handling on synthetic Windows media.
- Writing GPT and MBR disk images, with checksum verification.
- **Booting the output in QEMU**: a BIOS-mode image boots through MBR → syslinux → `chain.c32` and
  starts the file named `bootmgr`; a GPT image boots under UEFI firmware (OVMF); a BIOS-mode image
  also boots under UEFI firmware. These use tiny stand-in programs for `bootmgr` and `bootx64.efi`.

**Not verified** (needs hardware or Windows that were not available):
- Writing to a real USB block device (partition nodes + `mount`). The same code is exercised
  through disk images, but the mount path itself could not run in the development sandbox.
- That a *real* Windows `bootmgr` starts under syslinux's `chain.c32 ntldr=/bootmgr` (a widely
  used technique, but only tested here with a stand-in), and that Windows Setup then completes.
- That each registry tweak, `PnpCustomizationsWinPE`/`pnputil` driver loading, and the 2023
  loaders behave as intended on current Windows 11 builds and real firmware. Settings follow
  Microsoft's documentation and Rufus's behaviour. **Please test in a VM or on a spare machine first.**
- Offline edits were tested on synthetic hives; real hives are much larger.
- Silent install: the answer file's structure and the safety invariants are tested (for example,
  nothing that erases a disk is ever written unless `--auto-partition` is given), but Windows Setup
  itself was not run. In particular unverified: that Setup skips each page as intended on current
  Windows 11 builds, how Windows numbers disks in WinPE on your hardware, and that the safeguard
  fails safe on every machine.

Windows changes over time: Microsoft has already removed some bypasses in preview builds
(for example `BypassNRO`). toaster also creates a local account through the answer file,
which is the more durable route to a local-only setup.

## License

toaster is MIT licensed. The code is written independently from Microsoft's published
answer-file schema and registry documentation, not copied from Rufus (GPLv3). `syslinux`, `wimlib`,
`hivex` and `mtools` are separate programs that toaster runs; none of their code is included.
