# toaster

Create Windows 11 install USB drives on Linux, with the intrusive parts taken out.

toaster is inspired by [Rufus](https://rufus.ie)'s "Windows User Experience" options, which
it makes available on Linux: skip the TPM / Secure Boot / RAM checks, skip the
Microsoft-account requirement, opt out of data collection, and strip the bundled extras.
It is a Python (3.10+) command line tool with no Python dependencies. It drives the same
open-source tools Rufus is built on (`wimlib`, `hivex`) plus standard disk utilities.

## Install

```sh
sudo apt install wimtools libhivex-bin p7zip-full gdisk dosfstools util-linux   # Debian/Ubuntu
pip install .            # or run in place:  PYTHONPATH=src python3 -m toaster --help
```

## Use

```sh
toaster drives                              # list USB drives
toaster inspect Win11.iso                   # editions, build, whether install.wim needs splitting
toaster tweaks                              # everything you can switch on

# Write a stick (needs root). Shows what it will do and asks you to retype the device path.
sudo toaster write Win11.iso /dev/sdb --preset debloat --username alex --verify

# No USB handy? Produce the customized file tree, or just the answer file:
toaster prepare Win11.iso --out ./win11-tree --preset debloat
toaster unattend --preset debloat -o autounattend.xml
```

Options can be saved and reused: `--save-profile my.json` / `--profile my.json`.

| Preset | What it does |
|---|---|
| `minimal` (default) | Bypass TPM / Secure Boot / RAM checks, skip the Microsoft-account requirement |
| `debloat` | `minimal` + opt out of privacy toggles, no automatic BitLocker, and every tweak marked `*` in `toaster tweaks` |
| `none` | Nothing; add options individually |

Individual flags: `--bypass tpm,secureboot,ram,cpu,storage,disk`, `--no-bypass`,
`--[no-]local-account`, `--username`, `--no-data-collection`, `--disable-bitlocker`,
`--offline-internal-drives`, `--timezone`, `--locale`, `--tweak ID` (repeatable).

## How it works

1. The ISO is extracted to a staging folder (the ISO and your files are never modified).
2. The `LabConfig` bypass values are written into the registry hive inside `boot.wim`
   (the "Microsoft Windows Setup" image) so Setup never enforces the requirements;
   `sources/appraiserres.dll` is emptied so in-place upgrades skip the appraisal too. If the
   hive cannot be patched, the same values go into the answer file instead.
3. An answer file is generated for everything else and placed where Setup reads it
   (`Autounattend.xml` in `boot.wim` when a Windows PE setting is needed, otherwise
   `sources\$OEM$\$$\Panther\unattend.xml`).
4. `install.wim` over 4 GiB is split into `.swm` parts so it fits on FAT32.
5. The drive is wiped, given one GPT/FAT32 partition, and the files are copied (optionally
   read back and checksummed).

Safety: toaster refuses any disk holding a mounted system path (`/`, `/boot`, `/home`, ...)
even with `--allow-fixed`, refuses non-USB disks unless `--allow-fixed`, and requires you to
retype the device path before erasing anything.

## Status: what has and hasn't been verified

Verified by the automated tests (`PYTHONPATH=src python3 -m unittest discover -s tests -t .`),
using the real `wimlib-imagex` and `hivex` against a synthetic Windows media tree:
answer-file generation (well-formed, correctly escaped, correct pass placement), offline
registry patching of `boot.wim`, fallback to the answer file, `appraiserres.dll` handling,
`install.wim` splitting, device-safety rules, copy + checksum verification, the CLI.

**Not verified**, because it needs hardware or Windows that were not available:

- Partitioning, formatting, mounting and writing a *real* USB stick end to end
  (the development sandbox has no `vfat` driver or loop-partition support).
- That a USB made by toaster actually boots and installs on real hardware, and that each
  registry tweak behaves as intended on current Windows 11 builds. Registry keys and
  answer-file settings follow Microsoft's documentation and Rufus's behaviour, but have not
  been run on a live Windows install. **Please test on a spare machine or VM first.**
- Only UEFI boot is produced. Legacy BIOS boot is not supported yet.

Windows changes over time: Microsoft has already removed some bypasses in preview builds
(for example `BypassNRO`). toaster also creates a local account through the answer file,
which is the more durable route to a local-only setup.

## License

toaster is MIT licensed. The code is written independently from Microsoft's published answer-file
schema and registry documentation, not copied from Rufus. Rufus is GPLv3: copying Rufus source
into this project would require releasing the result under GPLv3.
