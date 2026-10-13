import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from toaster import drivers, regpatch, tweaks, wim
from toaster.customize import SYSTEM_HIVE_IN_WIM, customize_tree, find_ci
from toaster.options import Customizations, OptionsError
from toaster.tools import ToolError
from toaster.unattend import NS, WCM, build_unattend

from .fakemedia import ONEDRIVE, make_windows_tree
from .test_customize import HAVE_TOOLS

N = {"u": NS}


def make_driver_pack(root: Path, name="vmd", cat=True) -> Path:
    d = root / name
    (d / "x64").mkdir(parents=True)
    (d / "x64" / "iaStorVD.inf").write_text("[Version]\n")
    (d / "x64" / "iaStorVD.sys").write_bytes(b"MZ")
    if cat:
        (d / "x64" / "iaStorVD.cat").write_bytes(b"cat")
    return d


class UnattendFeatureTests(unittest.TestCase):
    def test_pe_driver_paths_become_pnp_customizations(self):
        o = Customizations(bypass_checks=[], local_account=False, drivers=["x"])
        u = build_unattend(o, pe_driver_paths=["X:\\Drivers\\1-vmd\\x64", "X:\\Drivers\\2-nic"])
        self.assertTrue(u.needs_pe_pass)
        root = ET.fromstring(u.xml)
        comp = root.find(".//u:component[@name='Microsoft-Windows-PnpCustomizationsWinPE']", N)
        entries = comp.findall("u:DriverPaths/u:PathAndCredentials", N)
        self.assertEqual([e.find("u:Path", N).text for e in entries], ["X:\\Drivers\\1-vmd\\x64", "X:\\Drivers\\2-nic"])
        self.assertEqual([e.get(f"{{{WCM}}}keyValue") for e in entries], ["1", "2"])
        self.assertEqual({e.get(f"{{{WCM}}}action") for e in entries}, {"add"})
        # no bypass fallback requested -> no Microsoft-Windows-Setup component, no blank product key
        self.assertIsNone(root.find(".//u:component[@name='Microsoft-Windows-Setup']", N))

    def test_os_drivers_install_with_pnputil_and_clean_up(self):
        o = Customizations(bypass_checks=[], local_account=False, drivers=["x"])
        root = ET.fromstring(build_unattend(o, os_drivers=True).xml)
        spec = [p.text for p in root.findall(".//u:settings[@pass='specialize']//u:Path", N)]
        self.assertTrue(any("pnputil /add-driver" in c and "/subdirs" in c and "exit /b 0" in c for c in spec))
        logon = [c.text for c in root.findall(".//u:FirstLogonCommands//u:CommandLine", N)]
        self.assertTrue(any("rmdir /s /q" in c for c in logon))

    def test_keep_drivers_skips_cleanup(self):
        o = Customizations(bypass_checks=[], local_account=False, drivers=["x"], keep_drivers=True)
        root = ET.fromstring(build_unattend(o, os_drivers=True).xml)
        self.assertEqual(root.findall(".//u:FirstLogonCommands", N), [])

    def test_offline_debloat_drops_registry_commands_but_keeps_appx_removal(self):
        o = Customizations(bypass_checks=[], local_account=False, offline_debloat=True,
                           tweaks=["no-copilot", "no-onedrive", "no-outlook-teams"])
        root = ET.fromstring(build_unattend(o).xml)
        text = ET.tostring(root, encoding="unicode")
        self.assertNotIn("TurnOffWindowsCopilot", text)
        self.assertNotIn("ShowCopilotButton", text)
        self.assertNotIn("OneDriveSetup", text)               # file deletion happens offline
        self.assertIn("Remove-AppxProvisionedPackage", text)  # no offline equivalent

    def test_options_validation_for_new_fields(self):
        with self.assertRaises(OptionsError):
            Customizations(editions=[0]).validate()
        with self.assertRaises(OptionsError):
            Customizations(keep_drivers=True).validate()

    def test_offline_target_mapping(self):
        t = tweaks.offline_target
        self.assertEqual(t(tweaks.RegValue("HKLM", "Software\\Policies\\X", "a", 1)), ("software", "Policies\\X"))
        self.assertEqual(t(tweaks.RegValue("HKLM", "System\\CurrentControlSet\\Control\\P", "a", 1)),
                         ("system", "ControlSet001\\Control\\P"))
        self.assertEqual(t(tweaks.RegValue("HKCU", "Software\\Microsoft\\X", "a", 1)),
                         ("user", "Software\\Microsoft\\X"))

    def test_every_catalog_tweak_maps_to_an_offline_hive(self):
        for tw in tweaks.TWEAKS:
            for v in tw.values:
                self.assertIn(tweaks.offline_target(v)[0], tweaks.OFFLINE_HIVES)


class DriverStagingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_stage_lists_inf_folders_in_winpe_syntax(self):
        pack = make_driver_pack(self.tmp)
        plan = drivers.stage_drivers([str(pack)], self.tmp / "stage")
        self.assertEqual(plan.pe_paths, ["X:\\Drivers\\1-vmd\\x64"])
        self.assertEqual(plan.inf_count, 1)
        self.assertEqual(plan.warnings, [])
        self.assertTrue((self.tmp / "stage" / "1-vmd" / "x64" / "iaStorVD.sys").exists())

    def test_missing_catalog_warns(self):
        plan = drivers.stage_drivers([str(make_driver_pack(self.tmp, cat=False))], self.tmp / "stage")
        self.assertTrue(any(".cat" in w for w in plan.warnings))

    def test_rejects_folder_without_inf_and_missing_folder(self):
        (self.tmp / "empty").mkdir()
        with self.assertRaisesRegex(ToolError, "no .inf"):
            drivers.stage_drivers([str(self.tmp / "empty")], self.tmp / "s1")
        with self.assertRaisesRegex(ToolError, "not found"):
            drivers.stage_drivers([str(self.tmp / "nope")], self.tmp / "s2")


@unittest.skipUnless(HAVE_TOOLS, "needs wimlib-imagex and hivex")
class PipelineFeatureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tree(self, **kw):
        root = make_windows_tree(self.tmp / "media", **kw)
        return root, find_ci(root, "sources/boot.wim"), find_ci(root, "sources/install.wim")

    def wim_file(self, wimfile, index, path):
        d = Path(tempfile.mkdtemp(dir=self.tmp))
        return wim.extract(wimfile, index, path, d)

    # --- drivers -----------------------------------------------------------
    def test_drivers_reach_boot_wim_media_and_answer_file(self):
        root, boot, _ = self.tree()
        pack = make_driver_pack(self.tmp)
        report = customize_tree(root, Customizations(local_account=False, drivers=[str(pack)]))
        self.assertEqual(report.warnings, [])
        self.assertEqual(self.wim_file(boot, 2, "/Drivers/1-vmd/x64/iaStorVD.sys").read_bytes(), b"MZ")
        self.assertTrue((root / "Sources" / "$OEM$" / "$1" / "Drivers" / "1-vmd" / "x64" / "iaStorVD.inf").exists())
        xml = self.wim_file(boot, 2, "/Autounattend.xml").read_text()
        self.assertIn("PnpCustomizationsWinPE", xml)
        self.assertIn("X:\\Drivers\\1-vmd\\x64", xml)
        self.assertIn("pnputil", xml)

    def test_bypass_and_drivers_share_a_single_boot_wim_update(self):
        root, boot, _ = self.tree()
        customize_tree(root, Customizations(local_account=False, drivers=[str(make_driver_pack(self.tmp))]))
        with regpatch.Hive(self.wim_file(boot, 2, SYSTEM_HIVE_IN_WIM)) as h:
            self.assertEqual(h.get_value("Setup\\LabConfig", "BypassTPMCheck"), 1)
        # ...and the driver files rode along in that same update
        self.assertEqual(self.wim_file(boot, 2, "/Drivers/1-vmd/x64/iaStorVD.cat").read_bytes(), b"cat")

    # --- 2023 boot loaders ----------------------------------------------------
    def test_2023_loaders_replace_boot_files_and_font(self):
        root, _, _ = self.tree()
        report = customize_tree(root, Customizations(bypass_checks=[], local_account=False, secureboot_2023=True))
        self.assertEqual((root / "efi" / "boot" / "bootx64.efi").read_bytes(), b"2023-bootmgfw")
        self.assertEqual((root / "bootmgr.efi").read_bytes(), b"2023-bootmgr")
        self.assertEqual((root / "efi" / "microsoft" / "boot" / "fonts" / "segoen_slboot.ttf").read_bytes(), b"2023-font")
        self.assertFalse((root / "efi" / "microsoft" / "boot" / "fonts" / "segoen_slboot_EX.ttf").exists())
        self.assertTrue(any("2023" in a for a in report.actions))

    def test_2023_loaders_on_older_media_warn_and_change_nothing(self):
        root, _, _ = self.tree(efi_ex=False)
        report = customize_tree(root, Customizations(bypass_checks=[], local_account=False, secureboot_2023=True))
        self.assertEqual((root / "efi" / "boot" / "bootx64.efi").read_bytes(), b"2011-bootmgfw")
        self.assertTrue(any("no 2023-signed" in w for w in report.warnings))

    # --- offline debloat -----------------------------------------------------------
    def test_offline_tweaks_land_in_every_edition(self):
        root, _, inst = self.tree(rich_install=True)
        opts = Customizations(bypass_checks=[], local_account=False, offline_debloat=True,
                              tweaks=["no-copilot", "no-fast-startup", "no-onedrive", "classic-context-menu"])
        report = customize_tree(root, opts)
        self.assertEqual(report.warnings, [])
        for idx in (1, 2, 3):
            with regpatch.Hive(self.wim_file(inst, idx, "/Windows/System32/config/SOFTWARE")) as h:
                self.assertEqual(h.get_value("Policies\\Microsoft\\Windows\\WindowsCopilot", "TurnOffWindowsCopilot"), 1)
            with regpatch.Hive(self.wim_file(inst, idx, "/Windows/System32/config/SYSTEM")) as h:
                self.assertEqual(h.get_value("ControlSet001\\Control\\Session Manager\\Power", "HiberbootEnabled"), 0)
            with regpatch.Hive(self.wim_file(inst, idx, "/Users/Default/NTUSER.DAT")) as h:
                self.assertEqual(h.get_value("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                                             "ShowCopilotButton"), 0)
                self.assertEqual(h.get_value(
                    "Software\\Classes\\CLSID\\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\\InprocServer32", ""), "")
            for rel in ONEDRIVE:
                with self.assertRaises(ToolError):
                    self.wim_file(inst, idx, "/" + rel)
            self.assertEqual(self.wim_file(inst, idx, "/edition.txt").read_text(), wim.images(inst)[idx - 1].name)

    def test_offline_mode_keeps_answer_file_free_of_applied_registry_commands(self):
        root, _, _ = self.tree(rich_install=True)
        customize_tree(root, Customizations(bypass_checks=[], local_account=False, offline_debloat=True,
                                            tweaks=["no-copilot"]))
        self.assertIsNone(find_ci(root, "sources/$OEM$/$$/Panther/unattend.xml"))

    def test_offline_failure_falls_back_to_first_boot_commands(self):
        root, _, _ = self.tree()           # plain install.wim has no SOFTWARE hive to patch
        report = customize_tree(root, Customizations(bypass_checks=[], local_account=False,
                                                     offline_debloat=True, tweaks=["no-copilot"]))
        self.assertTrue(any("Offline tweaks failed" in w for w in report.warnings))
        xml = (root / "Sources" / "$OEM$" / "$$" / "Panther" / "unattend.xml").read_text()
        self.assertIn("TurnOffWindowsCopilot", xml)

    # --- editions -----------------------------------------------------------------
    def test_keep_only_selected_editions(self):
        root, _, inst = self.tree(rich_install=True)
        report = customize_tree(root, Customizations(bypass_checks=[], local_account=False, editions=[2]))
        self.assertEqual([i.name for i in wim.images(inst)], ["Windows 11 Home"])
        self.assertTrue(any("Home" in a for a in report.actions))

    def test_unknown_edition_index_lists_what_exists(self):
        root, _, _ = self.tree(rich_install=True)
        with self.assertRaisesRegex(ToolError, "Windows 11 Home"):
            customize_tree(root, Customizations(bypass_checks=[], local_account=False, editions=[9]))

    def test_esd_is_converted_when_editing(self):
        root, _, inst = self.tree(rich_install=True)
        inst.rename(inst.with_name("install.esd"))
        customize_tree(root, Customizations(bypass_checks=[], local_account=False, editions=[1, 3]))
        self.assertFalse((root / "Sources" / "install.esd").exists())
        self.assertEqual([i.name for i in wim.images(root / "Sources" / "install.wim")],
                         ["Windows 11 Pro", "Windows 11 Education"])


if __name__ == "__main__":
    unittest.main()
