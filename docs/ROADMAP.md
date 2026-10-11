# Roadmap

Ordered roughly by value. `[Rufus]` = a feature Rufus has; `[new]` = beyond Rufus.

## Next: close the gaps with Rufus
- [ ] Legacy BIOS boot (MBR + active FAT32 partition with a Windows boot sector) [Rufus]
- [ ] UEFI:NTFS style booting for NTFS/exFAT media (no 4 GiB split needed) [Rufus]
- [ ] Secure Boot "Windows UEFI CA 2023" bootloaders: swap in `EFI_EX` files from `boot.wim` [Rufus]
- [ ] Silent/unattended install option (auto-partition, with the disk-selection safeguard Rufus uses) [Rufus]
- [ ] Windows 11 24H2+ in-place upgrade bypass (Rufus ships a `setup.exe` wrapper; a Linux-side
      alternative is documenting `setup.exe /product server`) [Rufus]
- [ ] Windows To Go [Rufus]
- [ ] Raw image mode (`dd`-style) for Linux ISOs, with progress and verify [Rufus]
- [ ] Copy host locale / time zone automatically (needs Linux tz -> Windows tz map) [Rufus]

## New: things Rufus doesn't do
- [ ] **Offline debloat of `install.wim`**: delete provisioned AppX packages and edit the
      *Default User* hive (`Users/Default/NTUSER.DAT`) so per-user tweaks apply to every new
      account, not just the first one that logs on. [new]
- [ ] Driver injection into `boot.wim` / `install.wim` (Intel RST/VMD, NVMe, USB) so Setup
      can see disks on newer hardware [new]
- [ ] Wi-Fi profile injection so Setup/OOBE has network without prompting [new]
- [ ] User-supplied scripts run at first logon (`--first-logon script.ps1`) [new]
- [ ] Edition control: remove `ei.cfg`/pick Home vs Pro, trim the image to one edition [new]
- [ ] Emit a customized *ISO* (xorriso) for VMs instead of a USB [new]
- [ ] Download official ISOs from Microsoft with hash verification [Rufus has this via Fido]
- [ ] `toaster explain`: describe in plain English what a profile will change [new]
- [ ] Profile sharing: signed/versioned JSON profiles, `toaster diff a.json b.json` [new]
- [ ] GTK/Qt front end on top of the same library [Rufus is GUI-first]
- [ ] Multi-boot (several ISOs on one stick) [new]
