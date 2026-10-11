import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from toaster import bootcode, devices, media
from toaster.tools import ToolError, run

from . import bootstubs
from .fakemedia import make_windows_tree

NEED = ("sgdisk", "mkfs.vfat", "mcopy", "mmd", "syslinux")
HAVE = all(shutil.which(t) for t in NEED) and shutil.which("wimlib-imagex") is not None


def stub_tree(root: Path) -> Path:
    tree = make_windows_tree(root)
    (tree / "bootmgr").write_bytes(bootstubs.BIOS_BOOTMGR)
    (tree / "efi" / "boot" / "bootx64.efi").write_bytes(bootstubs.efi_stub())
    return tree


class MbrTests(unittest.TestCase):
    def test_table_layout(self):
        t = media.mbr_table(disk_sectors=1_000_000, start=2048, signature=0xDEADBEEF)
        self.assertEqual(len(t), 72)                              # bytes 440..511: sig, 2 reserved, 4 entries, magic
        self.assertEqual(struct.unpack_from("<I", t, 0)[0], 0xDEADBEEF)
        entry = t[6:22]
        self.assertEqual(entry[0], 0x80)                          # active
        self.assertEqual(entry[4], 0x0C)                          # FAT32 LBA
        self.assertEqual(struct.unpack_from("<II", entry, 8), (2048, 1_000_000 - 2048))
        self.assertEqual(t[-2:], b"\x55\xaa")
        self.assertEqual(t[22:-2], bytes(48))                     # remaining three entries empty

    def test_partition_size_is_capped_at_32_bits(self):
        entry = media.mbr_table(2**33, 2048, 1)[6:22]
        self.assertEqual(struct.unpack_from("<I", entry, 12)[0], 0xFFFFFFFF)

    def test_partition_node_names(self):
        self.assertEqual(media.partition_path("/dev/sdb"), "/dev/sdb1")
        self.assertEqual(media.partition_path("/dev/nvme0n1"), "/dev/nvme0n1p1")
        self.assertEqual(media.partition_path("/dev/loop0"), "/dev/loop0p1")


@unittest.skipUnless(HAVE, "needs sgdisk, dosfstools, mtools, syslinux, wimlib")
class ImageWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.tree = stub_tree(self.tmp / "media")

    def write(self, boot, size=256 * 2**20, **kw):
        img = self.tmp / f"{boot}.img"
        drive = devices.image_drive(str(img), size)
        media.write_tree_to_device(self.tree, drive, boot=boot, **kw)
        return img

    def listing(self, img, offset, path="::"):
        out = run(["mdir", "-a", "-/", "-b", "-i", f"{img}@@{offset}", path], env={"MTOOLS_SKIP_CHECK": "1"}).stdout
        return {line.strip() for line in out.splitlines()}

    def test_bios_layout(self):
        img = self.write("bios")
        d = img.read_bytes()[:2 * 2**20]
        self.assertEqual(d[510:512], b"\x55\xaa")
        self.assertEqual(d[446], 0x80)                            # active partition
        self.assertEqual(d[450], 0x0C)
        start = struct.unpack_from("<I", d, 454)[0]
        self.assertEqual(start, 2048)
        self.assertNotEqual(struct.unpack_from("<I", d, 440)[0], 0)   # disk signature set
        self.assertEqual(d[:440], bootcode.mbr_code())
        pbr = d[start * 512:(start + 1) * 512]
        self.assertEqual(pbr[3:11], b"SYSLINUX")
        self.assertEqual(struct.unpack_from("<I", pbr, 0x1C)[0], start)   # hidden sectors point at the partition

    def test_bios_media_has_syslinux_files_and_windows_files(self):
        img = self.write("bios")
        names = {n.lower() for n in self.listing(img, 2048 * 512)}
        for expected in ("::/bootmgr", "::/syslinux.cfg", "::/chain.c32", "::/libcom32.c32", "::/ldlinux.sys",
                         "::/sources/boot.wim", "::/efi/boot/bootx64.efi"):
            self.assertIn(expected.lower(), names, expected)

    def test_uefi_layout_is_valid_gpt_with_data_partition(self):
        img = self.write("uefi")
        info = run(["sgdisk", "-p", img]).stdout
        self.assertIn("0700", info)
        self.assertEqual(run(["sgdisk", "-v", img], check=False).returncode, 0)
        self.assertIn("::/efi/boot/bootx64.efi".lower(), {n.lower() for n in self.listing(img, 2048 * 512)})
        self.assertNotIn("syslinux.cfg", " ".join(self.listing(img, 2048 * 512)).lower())

    def test_label_is_applied_uppercased(self):
        img = self.write("uefi", label="win11")
        out = run(["mdir", "-i", f"{img}@@{2048 * 512}", "::"], env={"MTOOLS_SKIP_CHECK": "1"}).stdout
        self.assertIn("WIN11", out)

    def test_verify_runs_and_reports_progress_phases(self):
        phases = []
        self.write("uefi", verify=True, progress=lambda p, a, b: phases.append(p))
        self.assertTrue({"copy", "verify", "done"} <= set(phases))

    def test_too_small_image_is_refused_before_touching_it(self):
        img = self.tmp / "small.img"
        with open(self.tree / "padding.bin", "wb") as f:
            f.truncate(8 * 2**20)                       # sparse: bigger than the 4 MiB image
        drive = devices.image_drive(str(img), 4 * 2**20)
        with self.assertRaisesRegex(ToolError, "needs"):
            media.write_tree_to_device(self.tree, drive, boot="uefi")
        self.assertEqual(img.read_bytes(), bytes(4 * 2**20))

    def test_bios_requires_bootmgr(self):
        (self.tree / "bootmgr").unlink()
        with self.assertRaisesRegex(ToolError, "bootmgr"):
            self.write("bios")

    def test_bad_arguments(self):
        drive = devices.image_drive(str(self.tmp / "a.img"), 256 * 2**20)
        with self.assertRaisesRegex(ToolError, "label"):
            media.write_tree_to_device(self.tree, drive, label="way too long label!")
        with self.assertRaisesRegex(ToolError, "boot mode"):
            media.write_tree_to_device(self.tree, drive, boot="floppy")

    def test_rewriting_an_image_replaces_previous_contents(self):
        img = self.write("bios")
        (self.tree / "only-new.txt").write_text("x")
        drive = devices.image_drive(str(img))
        media.write_tree_to_device(self.tree, drive, boot="uefi")
        names = {n.lower() for n in self.listing(img, 2048 * 512)}
        self.assertIn("::/only-new.txt", names)
        self.assertNotIn("::/syslinux.cfg", names)


@unittest.skipUnless(HAVE, "needs sgdisk, dosfstools, mtools, syslinux, wimlib")
class MtoolsVolumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.img = self.tmp / "v.img"
        self.img.write_bytes(bytes(64 * 2**20))
        run(["mkfs.vfat", "-F", "32", str(self.img)])
        self.vol = media.MtoolsVolume(str(self.img), 0)
        self.src = self.tmp / "src"
        (self.src / "sources" / "$OEM$" / "$1" / "Drivers").mkdir(parents=True)
        (self.src / "sources" / "$OEM$" / "$1" / "Drivers" / "a long driver name.inf").write_text("inf")
        (self.src / "big.bin").write_bytes(bytes(range(256)) * 40_000)       # ~10 MB, several chunks

    def test_roundtrip_special_and_long_names(self):
        events = []
        media.copy_tree(self.src, self.vol, lambda p, a, b: events.append((a, b)))
        self.assertEqual(events[-1][0], events[-1][1])
        self.assertGreater(len(events), 3)                                   # progress is incremental
        self.assertEqual(media.verify_tree(self.src, self.vol), [])
        data = b"".join(self.vol.read("sources/$OEM$/$1/Drivers/a long driver name.inf"))
        self.assertEqual(data, b"inf")

    def test_verify_reports_changed_and_missing_files(self):
        media.copy_tree(self.src, self.vol)
        (self.src / "big.bin").write_bytes(b"different")
        run(["mdel", "-i", str(self.img), "::sources/$OEM$/$1/Drivers/a long driver name.inf"],
            env={"MTOOLS_SKIP_CHECK": "1"})
        self.assertEqual(sorted(media.verify_tree(self.src, self.vol)),
                         ["big.bin", "sources/$OEM$/$1/Drivers/a long driver name.inf"])

    def test_write_failure_raises(self):
        with self.assertRaises(ToolError):
            self.vol.write(b"x", "no-such-dir/file.txt", lambda n: None)


@unittest.skipUnless(HAVE and bootstubs.have_qemu(), "needs QEMU plus the image tools")
class BootInEmulatorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.tree = stub_tree(self.tmp / "media")

    def image(self, boot):
        img = self.tmp / f"{boot}.img"
        media.write_tree_to_device(self.tree, devices.image_drive(str(img), 256 * 2**20), boot=boot)
        return img

    def test_bios_image_boots_into_bootmgr(self):
        out = bootstubs.boot_serial(self.image("bios"), bootstubs.BIOS_MARKER)
        self.assertIn(bootstubs.BIOS_MARKER, out)

    @unittest.skipUnless(bootstubs.OVMF_CODE.exists(), "OVMF firmware not installed")
    def test_uefi_image_boots_efi_loader(self):
        out = bootstubs.boot_serial(self.image("uefi"), bootstubs.EFI_MARKER, uefi=True)
        self.assertIn(bootstubs.EFI_MARKER, out)

    @unittest.skipUnless(bootstubs.OVMF_CODE.exists(), "OVMF firmware not installed")
    def test_bios_drive_also_boots_on_uefi_firmware(self):
        out = bootstubs.boot_serial(self.image("bios"), bootstubs.EFI_MARKER, uefi=True)
        self.assertIn(bootstubs.EFI_MARKER, out)


if __name__ == "__main__":
    unittest.main()
