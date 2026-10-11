import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from toaster import bootcode, cli, devices, media, regpatch, wim
from toaster.options import Customizations
from toaster.tools import ToolError

from .fakemedia import make_windows_tree
from .test_customize import HAVE_TOOLS

LSBLK = json.dumps({"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 512110190592, "rm": False, "hotplug": False, "tran": "nvme",
     "type": "disk", "model": "Samsung", "vendor": None, "ro": False, "mountpoints": [None],
     "children": [{"name": "nvme0n1p1", "type": "part", "mountpoints": ["/boot/efi"]},
                  {"name": "nvme0n1p2", "type": "part", "mountpoints": ["/"]}]},
    {"name": "sdb", "path": "/dev/sdb", "size": 16008609792, "rm": True, "hotplug": True, "tran": "usb",
     "type": "disk", "model": "Ultra", "vendor": "SanDisk ", "ro": False, "mountpoints": [None],
     "children": [{"name": "sdb1", "type": "part", "mountpoints": ["/run/media/me/USB"]}]},
    {"name": "sdc", "path": "/dev/sdc", "size": 8000000000, "rm": True, "hotplug": True, "tran": "usb",
     "type": "disk", "model": "Locked", "vendor": "", "ro": True, "mountpoints": [None]},
    {"name": "sr0", "path": "/dev/sr0", "size": 1, "type": "rom"},
]})


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.drives = {d.path: d for d in devices.parse_lsblk(LSBLK)}

    def test_parse_skips_non_disks_and_collects_child_mounts(self):
        self.assertEqual(set(self.drives), {"/dev/nvme0n1", "/dev/sdb", "/dev/sdc"})
        self.assertEqual(self.drives["/dev/sdb"].mountpoints, ["/run/media/me/USB"])
        self.assertEqual(self.drives["/dev/sdb"].label, "SanDisk Ultra")

    def test_system_disk_always_refused_even_with_allow_fixed(self):
        with self.assertRaisesRegex(ToolError, "running system"):
            devices.check_target(self.drives["/dev/nvme0n1"], allow_fixed=True)

    def test_usb_stick_is_accepted(self):
        devices.check_target(self.drives["/dev/sdb"], needed_bytes=8 * 2**30)

    def test_too_small_and_readonly_refused(self):
        with self.assertRaisesRegex(ToolError, "needs"):
            devices.check_target(self.drives["/dev/sdb"], needed_bytes=32 * 2**30)
        with self.assertRaisesRegex(ToolError, "read-only"):
            devices.check_target(self.drives["/dev/sdc"])

    def test_fixed_disk_needs_explicit_override(self):
        d = devices.Drive("/dev/sdd", 2**40, transport="sata")
        with self.assertRaisesRegex(ToolError, "allow-fixed"):
            devices.check_target(d)
        devices.check_target(d, allow_fixed=True)

    def test_partition_names(self):
        self.assertEqual(media.partition_path("/dev/sdb"), "/dev/sdb1")
        self.assertEqual(media.partition_path("/dev/nvme0n1"), "/dev/nvme0n1p1")
        self.assertEqual(media.partition_path("/dev/mmcblk0"), "/dev/mmcblk0p1")
        self.assertEqual(media.partition_path("/dev/loop0"), "/dev/loop0p1")


class CopyVerifyTests(unittest.TestCase):
    """The mount backend is plain directory I/O, so it can be tested without root."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        t = Path(self._tmp.name)
        self.src, self.dst = t / "src", t / "dst"
        (self.src / "sources" / "$OEM$").mkdir(parents=True)
        (self.src / "a.bin").write_bytes(b"x" * 10_000_000)
        (self.src / "sources" / "$OEM$" / "u.xml").write_text("<x/>")
        self.dst.mkdir()
        self.vol = media.MountedVolume(self.dst)

    def test_copy_then_verify_clean(self):
        events = []
        total = media.tree_size(self.src)
        media.copy_tree(self.src, self.vol, lambda *a: events.append(a))
        self.assertEqual(events[-1], ("copy", total, total))
        self.assertEqual(media.verify_tree(self.src, self.vol), [])

    def test_extra_files_are_copied_and_verified(self):
        media.copy_tree(self.src, self.vol, extra={"syslinux.cfg": b"cfg"})
        self.assertEqual((self.dst / "syslinux.cfg").read_bytes(), b"cfg")
        self.assertEqual(media.verify_tree(self.src, self.vol, extra={"syslinux.cfg": b"cfg"}), [])

    def test_verify_detects_corruption_missing_and_truncation(self):
        media.copy_tree(self.src, self.vol)
        with open(self.dst / "a.bin", "r+b") as f:
            f.seek(5_000_000)
            f.write(b"Y")                                   # same size, flipped byte
        (self.dst / "sources" / "$OEM$" / "u.xml").unlink()
        bad = media.verify_tree(self.src, self.vol)
        self.assertEqual(sorted(bad), ["a.bin", "sources/$OEM$/u.xml"])


class SizeParsingTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(cli.parse_size("8G"), 8 * 2**30)
        self.assertEqual(cli.parse_size("512m"), 512 * 2**20)
        self.assertEqual(cli.parse_size("1.5GiB"), int(1.5 * 2**30))
        self.assertEqual(cli.parse_size("100"), 100)
        with self.assertRaises(ToolError):
            cli.parse_size("lots")


@unittest.skipUnless(HAVE_TOOLS, "needs wimlib-imagex and hivex")
class SplitAndCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = make_windows_tree(self.tmp / "media")

    def test_small_install_wim_is_left_alone(self):
        self.assertIsNone(media.split_oversized_install_wim(self.root))

    def test_oversized_install_wim_is_split_and_original_removed(self):
        with mock.patch.object(media, "FAT32_MAX_FILE", 10):
            swm = media.split_oversized_install_wim(self.root, part_mib=1)
        self.assertEqual(swm.name, "install.swm")
        self.assertTrue(swm.exists())
        self.assertFalse((self.root / "Sources" / "install.wim").exists())

    def test_oversized_esd_gives_actionable_error(self):
        (self.root / "Sources" / "install.wim").rename(self.root / "Sources" / "install.esd")
        with mock.patch.object(media, "FAT32_MAX_FILE", 10), self.assertRaisesRegex(ToolError, "export"):
            media.split_oversized_install_wim(self.root)

    def test_inspect_reports_editions(self):
        info = media.inspect_windows_media(self.root)
        self.assertEqual(info["editions"][0]["name"], "Windows 11 Pro")
        self.assertEqual(info["setup_image_index"], 2)
        self.assertFalse(info["needs_split_for_fat32"])

    def test_cli_prepare_end_to_end(self):
        out = self.tmp / "out"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["prepare", str(self.root), "--out", str(out), "--preset", "debloat"])
        self.assertEqual(rc, 0)
        self.assertTrue((out / "Sources" / "$OEM$" / "$$" / "Panther" / "unattend.xml").exists())
        # the source folder must not be modified
        self.assertEqual((self.root / "Sources" / "appraiserres.dll").stat().st_size, 21)

    def test_cli_unattend_and_profile_roundtrip(self):
        prof = self.tmp / "p.json"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["unattend", "--preset", "debloat", "--save-profile", str(prof),
                                       "-o", str(self.tmp / "u.xml")]), 0)
            self.assertEqual(cli.main(["unattend", "--profile", str(prof), "-o", str(self.tmp / "u2.xml")]), 0)
        self.assertEqual((self.tmp / "u.xml").read_text(), (self.tmp / "u2.xml").read_text())

    def test_cli_bad_input_gives_clean_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["unattend", "--username", "Administrator"])
        self.assertEqual(rc, 2)
        self.assertIn("reserved", err.getvalue())

    def test_cli_write_refuses_without_confirmation_match(self):
        d = devices.Drive("/dev/sdz", 32 * 2**30, transport="usb", removable=True)
        with mock.patch.object(devices, "find", return_value=d), mock.patch("builtins.input", return_value="nope"), \
             mock.patch.object(media, "write_tree_to_device") as w, contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["write", str(self.root), "/dev/sdz", "--preset", "minimal"])
        self.assertEqual(rc, 1)
        w.assert_not_called()


if __name__ == "__main__":
    unittest.main()


IMG_TOOLS = HAVE_TOOLS and all(shutil.which(t) for t in ("sgdisk", "mkfs.vfat", "mcopy", "mmd", "syslinux"))


@unittest.skipUnless(IMG_TOOLS, "needs wimlib, hivex, sgdisk, dosfstools, mtools, syslinux")
class CliWriteImageTests(unittest.TestCase):
    """Drive the real CLI end to end into a disk image and inspect the result."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = make_windows_tree(self.tmp / "media", rich_install=True)
        pack = self.tmp / "vmd"
        pack.mkdir()
        (pack / "iaStorVD.inf").write_text("[Version]")
        (pack / "iaStorVD.cat").write_bytes(b"c")
        self.pack = pack

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def pull(self, img, name):
        dest = self.tmp / ("pulled-" + name.replace("/", "_"))
        with open(dest, "wb") as f:
            f.write(subprocess.run(["mcopy", "-i", f"{img}@@1048576", f"::{name}", "-"], capture_output=True,
                                   env={"MTOOLS_SKIP_CHECK": "1", "PATH": "/usr/bin:/bin"}, check=True).stdout)
        return dest

    def test_everything_at_once_to_a_bios_image(self):
        img = self.tmp / "win.img"
        rc, out, err = self.run_cli("write", self.root, img, "--image-size", "512M", "--boot", "bios", "--verify",
                                    "--preset", "debloat", "--offline-debloat", "--edition", "2",
                                    "--drivers", self.pack, "--secureboot-2023", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertIn("legacy BIOS and UEFI", out)
        self.assertIn("Keep only install image(s): 2", out)
        # editions trimmed, tweaks applied offline, drivers + 2023 loaders present on the stick
        install = self.pull(img, "sources/install.wim")
        self.assertEqual([i.name for i in wim.images(install)], ["Windows 11 Home"])
        with regpatch.Hive(wim.extract(install, 1, "/Windows/System32/config/SOFTWARE", self.tmp)) as h:
            self.assertEqual(h.get_value("Policies\\Microsoft\\Windows\\WindowsCopilot", "TurnOffWindowsCopilot"), 1)
        self.assertEqual(self.pull(img, "efi/boot/bootx64.efi").read_bytes(), b"2023-bootmgfw")
        self.assertEqual(self.pull(img, "sources/$OEM$/$1/Drivers/1-vmd/iaStorVD.inf").read_bytes(), b"[Version]")
        boot = self.pull(img, "sources/boot.wim")
        xml = wim.extract(boot, 2, "/Autounattend.xml", self.tmp).read_text()
        self.assertIn("X:\\Drivers\\1-vmd", xml)
        self.assertEqual(self.pull(img, "syslinux.cfg").read_bytes(), bootcode.SYSLINUX_CFG.encode())

    def test_refuses_to_overwrite_existing_image_without_matching_confirmation(self):
        img = self.tmp / "exists.img"
        img.write_bytes(bytes(300 * 2**20))
        with mock.patch("builtins.input", return_value="no"):
            rc, out, err = self.run_cli("write", self.root, img, "--preset", "minimal")
        self.assertEqual(rc, 1)
        self.assertEqual(img.read_bytes()[:512], bytes(512))          # untouched

    def test_new_image_needs_no_confirmation(self):
        img = self.tmp / "new.img"
        rc, out, err = self.run_cli("write", self.root, img, "--image-size", "300M", "--preset", "minimal")
        self.assertEqual(rc, 0, err)
        self.assertTrue(img.exists())

    def test_unattend_command_includes_driver_sections(self):
        out_file = self.tmp / "u.xml"
        rc, _, err = self.run_cli("unattend", "--drivers", self.pack, "-o", out_file)
        self.assertEqual(rc, 0, err)
        xml = out_file.read_text()
        self.assertIn("PnpCustomizationsWinPE", xml)
        self.assertIn("pnputil", xml)

    def test_profile_roundtrip_with_new_options(self):
        prof = self.tmp / "p.json"
        rc, _, err = self.run_cli("unattend", "--drivers", self.pack, "--edition", "2", "--offline-debloat",
                                  "--secureboot-2023", "--tweak", "no-copilot", "--save-profile", prof,
                                  "-o", self.tmp / "a.xml")
        self.assertEqual(rc, 0, err)
        loaded = Customizations.load(prof)
        self.assertEqual((loaded.editions, loaded.offline_debloat, loaded.secureboot_2023), ([2], True, True))
        self.assertEqual(loaded.drivers, [str(self.pack.resolve())])
