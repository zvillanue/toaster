import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from toaster import cli, devices, media
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
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        t = Path(self._tmp.name)
        self.src, self.dst = t / "src", t / "dst"
        (self.src / "sources" / "$OEM$").mkdir(parents=True)
        (self.src / "a.bin").write_bytes(b"x" * 10_000_000)
        (self.src / "sources" / "$OEM$" / "u.xml").write_text("<x/>")
        self.dst.mkdir()

    def test_copy_then_verify_clean(self):
        events = []
        total = media.tree_size(self.src)
        media._copy_tree(self.src, self.dst, total, lambda *a: events.append(a))
        self.assertEqual(events[-1], ("copy", total, total))
        self.assertEqual(media._verify_tree(self.src, self.dst, lambda *a: None), [])

    def test_verify_detects_corruption_missing_and_truncation(self):
        media._copy_tree(self.src, self.dst, 1, lambda *a: None)
        with open(self.dst / "a.bin", "r+b") as f:
            f.seek(5_000_000)
            f.write(b"Y")                                   # same size, flipped byte
        (self.dst / "sources" / "$OEM$" / "u.xml").unlink()
        bad = media._verify_tree(self.src, self.dst, lambda *a: None)
        self.assertEqual(sorted(bad), ["a.bin", str(Path("sources/$OEM$/u.xml"))])


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
