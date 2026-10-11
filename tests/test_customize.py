import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from toaster import regpatch, wim
from toaster.customize import SYSTEM_HIVE_IN_WIM, customize_tree, find_ci
from toaster.options import Customizations
from toaster.unattend import NS

from .fakemedia import make_windows_tree

HAVE_TOOLS = all(shutil.which(t) for t in ("wimlib-imagex", "hivexsh", "hivexget"))


@unittest.skipUnless(HAVE_TOOLS, "needs wimlib-imagex and hivex")
class CustomizeTreeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_windows_tree(Path(self._tmp.name) / "media")
        self.boot = find_ci(self.root, "sources/boot.wim")

    def labconfig(self, name):
        with tempfile.TemporaryDirectory() as t:
            hive = wim.extract(self.boot, 2, SYSTEM_HIVE_IN_WIM, Path(t))
            return regpatch.get_value(hive, "Setup\\LabConfig", name)

    def test_setup_image_found_by_name(self):
        self.assertEqual(wim.find_setup_image(self.boot), 2)

    def test_bypass_written_to_boot_wim_registry_not_answer_file(self):
        report = customize_tree(self.root, Customizations(local_account=False))
        for name in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck"):
            self.assertEqual(self.labconfig(name), "1")
        self.assertIsNone(report.answer_file)       # nothing left that needs an answer file
        self.assertEqual(report.warnings, [])

    def test_only_setup_image_is_patched(self):
        customize_tree(self.root, Customizations(local_account=False))
        with tempfile.TemporaryDirectory() as t:
            hive = wim.extract(self.boot, 1, SYSTEM_HIVE_IN_WIM, Path(t))
            with regpatch.Hive(hive) as h:
                self.assertEqual(h.find("Setup"), 0)

    def test_appraiserres_replaced_with_empty_file(self):
        customize_tree(self.root, Customizations(local_account=False))
        dll = find_ci(self.root, "sources/appraiserres.dll")
        self.assertEqual(dll.stat().st_size, 0)
        self.assertEqual(find_ci(self.root, "sources/appraiserres.bak").read_bytes(), b"MZ-original-appraiser")

    def test_answer_file_goes_to_oem_panther_when_no_pe_pass(self):
        report = customize_tree(self.root, Customizations(tweaks=["no-copilot"]))
        path = self.root / "Sources" / "$OEM$" / "$$" / "Panther" / "unattend.xml"
        self.assertTrue(path.exists())
        self.assertEqual(report.answer_file, "sources/$OEM$/$$/Panther/unattend.xml")
        ET.fromstring(path.read_text())

    def test_pe_fallback_ships_autounattend_inside_boot_wim(self):
        # Break the registry route by removing the hive from the setup image.
        with tempfile.TemporaryDirectory() as t:
            dummy = Path(t) / "x"
            dummy.write_text("not a hive")
            wim.update(self.boot, 2, {SYSTEM_HIVE_IN_WIM: dummy})
        report = customize_tree(self.root, Customizations(local_account=False))
        self.assertTrue(report.warnings)
        self.assertEqual(report.answer_file, "boot.wim:/Autounattend.xml")
        with tempfile.TemporaryDirectory() as t:
            xml = wim.extract(self.boot, 2, "/Autounattend.xml", Path(t)).read_text()
        self.assertIn("BypassTPMCheck", xml)
        self.assertIn(NS, xml)

    def test_bypass_disabled_leaves_media_untouched(self):
        before = self.boot.read_bytes()
        report = customize_tree(self.root, Customizations(bypass_checks=[], local_account=False))
        self.assertEqual(self.boot.read_bytes(), before)
        self.assertEqual(find_ci(self.root, "sources/appraiserres.dll").stat().st_size, 21)
        self.assertIsNone(report.answer_file)

    def test_extra_bypass_checks(self):
        customize_tree(self.root, Customizations(bypass_checks=["tpm", "cpu"], local_account=False))
        self.assertEqual(self.labconfig("BypassCPUCheck"), "1")

    def test_missing_boot_wim_warns_instead_of_crashing(self):
        self.boot.unlink()
        report = customize_tree(self.root, Customizations())
        self.assertTrue(any("boot.wim" in w for w in report.warnings))


class FindCiTests(unittest.TestCase):
    def test_case_insensitive(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "Sources").mkdir()
            (Path(t) / "Sources" / "Boot.WIM").write_text("")
            self.assertEqual(find_ci(Path(t), "sources/boot.wim").name, "Boot.WIM")
            self.assertIsNone(find_ci(Path(t), "sources/nope"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_TOOLS, "needs hivex")
class RegpatchTests(unittest.TestCase):
    def setUp(self):
        from .hivefixture import build_minimal_hive
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hive = Path(self._tmp.name) / "SYSTEM"
        self.hive.write_bytes(build_minimal_hive())

    def hivexget(self, key, name):
        # Independent reader (the hivex CLI) so we are not just trusting our own bindings.
        from toaster.tools import run
        return run(["hivexget", self.hive, "\\" + key, name]).stdout.strip()

    def test_multiple_values_all_survive(self):
        regpatch.set_dwords(self.hive, "Setup\\LabConfig", {"A": 1, "B": 1, "C": 1})
        for n in "ABC":
            self.assertEqual(self.hivexget("Setup\\LabConfig", n), "1")

    def test_second_call_merges_instead_of_clobbering(self):
        regpatch.set_dwords(self.hive, "Setup\\LabConfig", {"A": 1})
        regpatch.set_dwords(self.hive, "Setup\\LabConfig", {"B": 1, "A": 0})
        self.assertEqual(self.hivexget("Setup\\LabConfig", "A"), "0")
        self.assertEqual(self.hivexget("Setup\\LabConfig", "B"), "1")

    def test_other_value_types_in_the_key_are_preserved(self):
        with regpatch.Hive(self.hive, write=True) as h:
            h.set_value("K", "S", "REG_SZ", "keep me")
            h.set_value("K", "E", "REG_EXPAND_SZ", "%SystemRoot%\\x")
        regpatch.set_dwords(self.hive, "K", {"A": 1})
        self.assertEqual(self.hivexget("K", "S"), "keep me")
        self.assertEqual(self.hivexget("K", "E"), "%SystemRoot%\\x")
        self.assertEqual(self.hivexget("K", "A"), "1")

    def test_default_value_and_string_roundtrip(self):
        with regpatch.Hive(self.hive, write=True) as h:
            h.set_value("Software\\Classes\\CLSID\\{x}\\InprocServer32", "", "REG_SZ", "")
        with regpatch.Hive(self.hive) as h:
            self.assertEqual(h.get_value("Software\\Classes\\CLSID\\{x}\\InprocServer32", ""), "")
            self.assertIsNone(h.get_value("Software\\Classes\\CLSID\\{x}\\InprocServer32", "nope"))

    def test_key_lookup_is_case_insensitive(self):
        regpatch.set_dwords(self.hive, "Software\\Policies\\Microsoft", {"V": 7})
        with regpatch.Hive(self.hive, write=True) as h:
            h.set_value("SOFTWARE\\policies\\MICROSOFT", "W", "REG_DWORD", 8)
        self.assertEqual(self.hivexget("Software\\Policies\\Microsoft", "V"), "7")
        self.assertEqual(self.hivexget("Software\\Policies\\Microsoft", "W"), "8")

    def test_exception_inside_context_does_not_commit(self):
        with self.assertRaises(RuntimeError), regpatch.Hive(self.hive, write=True) as h:
            h.set_value("Gone", "X", "REG_DWORD", 1)
            raise RuntimeError("abort")
        with regpatch.Hive(self.hive) as h:
            self.assertEqual(h.find("Gone"), 0)

    def test_missing_hive_gives_clean_error(self):
        from toaster.tools import ToolError
        with self.assertRaises(ToolError):
            regpatch.Hive(Path(self._tmp.name) / "nope")
