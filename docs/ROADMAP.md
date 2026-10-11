# Roadmap

Ordered roughly by value. `[Rufus]` = a feature Rufus has; `[new]` = beyond Rufus.

## Next: close the gaps with Rufus
- [x] Legacy BIOS boot (MBR + syslinux `chain.c32` -> `bootmgr`) [Rufus]  (still to confirm with a real `bootmgr`)
- [ ] UEFI:NTFS style booting for NTFS/exFAT media (no 4 GiB split needed) [Rufus]
- [x] Secure Boot "Windows UEFI CA 2023" bootloaders: swap in `EFI_EX` files from `boot.wim` [Rufus]
- [x] Silent/unattended install: `--silent` (disk page kept) and opt-in `--auto-partition` with a disk-1 safeguard [Rufus]
- [ ] Stronger auto-partition safeguard: pick the target by size/bus type rather than disk number [new]
- [ ] Windows 11 24H2+ in-place upgrade bypass (Rufus ships a `setup.exe` wrapper; a Linux-side
      alternative is documenting `setup.exe /product server`) [Rufus]
- [ ] Windows To Go [Rufus]
- [ ] Raw image mode (`dd`-style) for Linux ISOs, with progress and verify [Rufus]
- [ ] Copy host locale / time zone automatically (needs Linux tz -> Windows tz map) [Rufus]

## New: things Rufus doesn't do
- [x] **Offline tweaks in `install.wim`**: registry values written to the SOFTWARE/SYSTEM/Default User
      hives so they apply to every new account [new]
- [ ] Offline removal of provisioned AppX packages (needs a safe way to keep the AppRepository
      database consistent from Linux; today this is done at install time) [new]
- [x] Driver injection: drivers in `boot.wim` for Setup, plus `pnputil` install into the new system [new]
- [ ] Real DriverStore injection into `install.wim` (needs the DRIVERS hive format; DISM's behaviour) [new]
- [ ] Wi-Fi profile injection so Setup/OOBE has network without prompting [new]
- [ ] User-supplied scripts run at first logon (`--first-logon script.ps1`) [new]
- [x] Edition control: trim the image to chosen editions [new]
- [ ] Edition prompt control: `ei.cfg`/`pid.txt` handling [new]
- [x] Write a bootable disk *image* for VMs instead of a USB [new]
- [ ] Emit a customized *ISO* (xorriso) [new]
- [ ] Download official ISOs from Microsoft with hash verification [Rufus has this via Fido]
- [ ] `toaster explain`: describe in plain English what a profile will change [new]
- [ ] Profile sharing: signed/versioned JSON profiles, `toaster diff a.json b.json` [new]
- [ ] GTK/Qt front end on top of the same library [Rufus is GUI-first]
- [ ] Multi-boot (several ISOs on one stick) [new]
