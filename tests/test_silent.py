import contextlib
import io
import itertools
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from toaster import cli, wim
from toaster.customize import customize_tree, find_ci
from toaster.options import Customizations, OptionsError
from toaster.tools import ToolError
from toaster.unattend import NS, WCM, build_unattend

from .fakemedia import make_windows_tree
from .test_customize import HAVE_TOOLS
from .test_media_devices_cli import IMG_TOOLS

N = {"u": NS}


def parse(opts, **kw):
    kw.setdefault("install_index", 1)
    return ET.fromstring(build_unattend(opts, **kw).xml)


def setup_component(root):
    return root.find(".//u:settings[@pass='windowsPE']/u:component[@name='Microsoft-Windows-Setup']", N)


def tags(el):
    return [c.tag.split("}")[1] for c in el]


class SilentAnswerFileTests(unittest.TestCase):
    def test_silent_skips_pages_but_never_names_a_disk(self):
        root = parse(Customizations(bypass_checks=[], local_account=False, silent_install=True))
        comp = setup_component(root)
        self.assertEqual(comp.find("u:UserData/u:AcceptEula", N).text, "true")
        self.assertEqual(comp.find("u:UserData/u:ProductKey/u:Key", N).text, None)
        self.assertIsNone(comp.find("u:DiskConfiguration", N))
        self.assertIsNone(comp.find("u:ImageInstall/u:OSImage/u:InstallTo", N))
        # the disk page stays visible
        self.assertEqual(comp.find("u:ImageInstall/u:OSImage/u:WillShowUI", N).text, "Always")
        meta = comp.find("u:ImageInstall/u:OSImage/u:InstallFrom/u:MetaData", N)
        self.assertEqual(meta.find("u:Key", N).text, "/IMAGE/INDEX")
        self.assertEqual(meta.find("u:Value", N).text, "1")
        self.assertEqual(meta.get(f"{{{WCM}}}action"), "add")

    def test_edition_omitted_when_unknown(self):
        root = parse(Customizations(bypass_checks=[], local_account=False, silent_install=True), install_index=None)
        self.assertIsNone(setup_component(root).find(".//u:MetaData", N))

    def test_language_comes_from_setup_image_unless_overridden(self):
        o = Customizations(bypass_checks=[], local_account=False, silent_install=True)
        root = parse(o, setup_language="de-DE")
        comp = root.find(".//u:component[@name='Microsoft-Windows-International-Core-WinPE']", N)
        self.assertEqual(comp.find("u:SetupUILanguage/u:UILanguage", N).text, "de-DE")
        for tag in ("InputLocale", "SystemLocale", "UILanguage", "UserLocale"):
            self.assertEqual(comp.find(f"u:{tag}", N).text, "de-DE")
        o.locale = "en-GB"
        comp = parse(o, setup_language="de-DE").find(".//u:component[@name='Microsoft-Windows-International-Core-WinPE']", N)
        self.assertEqual(comp.find("u:SetupUILanguage/u:UILanguage", N).text, "en-GB")

    def test_oobe_pages_hidden_only_when_silent(self):
        loud = parse(Customizations(bypass_checks=[], local_account=False, silent_install=True))
        oobe = loud.find(".//u:settings[@pass='oobeSystem']//u:OOBE", N)
        self.assertEqual({c.tag.split("}")[1]: c.text for c in oobe},
                         {"HideEULAPage": "true", "ProtectYourPC": "3",
                          "HideOnlineAccountScreens": "true", "HideWirelessSetupInOOBE": "true"})
        quiet = parse(Customizations(bypass_checks=[], local_account=False, no_data_collection=True))
        self.assertIsNone(quiet.find(".//u:HideOnlineAccountScreens", N))

    def test_bypass_fallback_and_silent_share_one_setup_component(self):
        root = parse(Customizations(silent_install=True), pe_bypass=True)
        comps = root.findall(".//u:component[@name='Microsoft-Windows-Setup']", N)
        self.assertEqual(len(comps), 1)
        self.assertEqual(tags(comps[0]), ["UserData", "ImageInstall", "RunSynchronous"])
        self.assertIn("BypassTPMCheck", comps[0].find(".//u:Path", N).text)

    def test_silent_with_registry_bypass_has_no_labconfig_commands(self):
        root = parse(Customizations(silent_install=True), pe_bypass=False)
        self.assertNotIn("LabConfig", ET.tostring(root, encoding="unicode"))


class AutoPartitionAnswerFileTests(unittest.TestCase):
    def build(self, **kw):
        o = Customizations(bypass_checks=[], local_account=False, silent_install=True, auto_partition=True, **kw)
        return parse(o)

    def test_guard_comes_first_then_the_wipe_of_disk_zero(self):
        dc = setup_component(self.build()).find("u:DiskConfiguration", N)
        disks = dc.findall("u:Disk", N)
        self.assertEqual(len(disks), 2)
        guard, wipe = disks
        self.assertEqual(guard.find("u:DiskID", N).text, "1")
        self.assertEqual(guard.get(f"{{{WCM}}}action"), "modify")
        self.assertIsNone(guard.find("u:WillWipeDisk", N))                  # the guard must never wipe
        self.assertIsNone(guard.find("u:CreatePartitions", N))
        self.assertEqual(wipe.find("u:DiskID", N).text, "0")
        self.assertEqual(wipe.find("u:WillWipeDisk", N).text, "true")
        self.assertEqual(dc.find("u:WillShowUI", N).text, "OnError")        # failure falls back to the UI

    def test_guard_targets_partition_one_of_the_install_drive_and_uses_its_label(self):
        guard = self.build(media_label="mystick").find(".//u:Disk[u:DiskID='1']", N)
        mod = guard.find("u:ModifyPartitions/u:ModifyPartition", N)
        self.assertEqual(mod.find("u:PartitionID", N).text, "1")
        self.assertEqual(mod.find("u:Label", N).text, "MYSTICK")

    def test_gpt_layout_efi_msr_windows(self):
        wipe = self.build().find(".//u:Disk[u:DiskID='0']", N)
        parts = [(p.find("u:Type", N).text, getattr(p.find("u:Size", N), "text", None),
                  getattr(p.find("u:Extend", N), "text", None)) for p in wipe.findall(".//u:CreatePartition", N)]
        self.assertEqual(parts, [("EFI", "260", None), ("MSR", "16", None), ("Primary", None, "true")])
        mods = {m.find("u:PartitionID", N).text: (m.find("u:Format", N).text, m.find("u:Label", N).text)
                for m in wipe.findall("u:ModifyPartitions/u:ModifyPartition", N)}
        self.assertEqual(mods, {"1": ("FAT32", "System"), "3": ("NTFS", "Windows")})
        self.assertEqual(wipe.find(".//u:ModifyPartition[u:PartitionID='3']/u:Letter", N).text, "C")

    def test_install_target_is_the_windows_partition_on_disk_zero(self):
        to = setup_component(self.build()).find("u:ImageInstall/u:OSImage/u:InstallTo", N)
        self.assertEqual((to.find("u:DiskID", N).text, to.find("u:PartitionID", N).text), ("0", "3"))
        self.assertEqual(setup_component(self.build()).find("u:ImageInstall/u:OSImage/u:WillShowUI", N).text, "OnError")

    def test_bitlocker_provisioning_is_disabled_inside_disk_configuration(self):
        dc = setup_component(self.build(disable_bitlocker=True)).find("u:DiskConfiguration", N)
        self.assertEqual(dc.find("u:DisableEncryptedDiskProvisioning", N).text, "true")
        self.assertIsNone(setup_component(self.build()).find(".//u:DisableEncryptedDiskProvisioning", N))

    def test_element_order_matches_microsofts_documented_layout(self):
        comp = setup_component(self.build())
        self.assertEqual(tags(comp), ["UserData", "DiskConfiguration", "ImageInstall"])

    def test_no_wipe_unless_auto_partition_across_every_option_combination(self):
        """The invariant that keeps --silent safe."""
        flags = ["silent_install", "bypass", "local_account", "no_data_collection", "disable_bitlocker",
                 "offline_internal_drives", "drivers", "username"]
        for combo in itertools.product([False, True], repeat=len(flags)):
            picked = dict(zip(flags, combo))
            o = Customizations(
                bypass_checks=["tpm"] if picked["bypass"] else [], local_account=picked["local_account"],
                silent_install=picked["silent_install"], no_data_collection=picked["no_data_collection"],
                disable_bitlocker=picked["disable_bitlocker"], offline_internal_drives=picked["offline_internal_drives"],
                drivers=["x"] if picked["drivers"] else [], username="sam" if picked["username"] else None)
            u = build_unattend(o, pe_driver_paths=["X:\\Drivers\\a"] if picked["drivers"] else [], install_index=1)
            if u is not None:
                self.assertNotIn("WillWipeDisk", u.xml, picked)
                self.assertNotIn("DiskConfiguration", u.xml, picked)
                self.assertNotIn("InstallTo", u.xml, picked)


class OptionValidationTests(unittest.TestCase):
    def test_auto_partition_requires_silent(self):
        with self.assertRaisesRegex(OptionsError, "requires silent_install"):
            Customizations(auto_partition=True).validate()

    def test_install_edition_must_be_among_kept_editions(self):
        with self.assertRaises(OptionsError):
            Customizations(silent_install=True, editions=[2, 3], install_edition=1).validate()
        Customizations(silent_install=True, editions=[2, 3], install_edition=3).validate()

    def test_language_parsed_from_wimlib_info(self):
        sample = ("Index:                  2\nName:                   Microsoft Windows Setup (amd64)\n"
                  "Architecture:           x86_64\nTotal Bytes:            100\n"
                  "Languages:              de-DE\nDefault Language:       de-DE\n")
        self.assertEqual(wim._parse_info(sample)[0].default_language, "de-DE")
        self.assertEqual(wim._parse_info("Index: 1\nName: x\n")[0].default_language, "")


@unittest.skipUnless(HAVE_TOOLS, "needs wimlib-imagex and hivex")
class SilentPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tree(self, **kw):
        root = make_windows_tree(self.tmp / "media", **kw)
        return root, find_ci(root, "sources/boot.wim")

    def autounattend(self, boot):
        return wim.extract(boot, 2, "/Autounattend.xml", Path(tempfile.mkdtemp(dir=self.tmp))).read_text()

    def index_in(self, xml):
        return ET.fromstring(xml).find(".//u:MetaData/u:Value", N).text

    def opts(self, **kw):
        return Customizations(bypass_checks=[], local_account=False, silent_install=True, **kw)

    def test_single_edition_image_needs_no_choice(self):
        root, boot = self.tree()
        report = customize_tree(root, self.opts())
        self.assertEqual(self.index_in(self.autounattend(boot)), "1")
        self.assertEqual(report.answer_file, "boot.wim:/Autounattend.xml")     # silent needs a windowsPE pass

    def test_multiple_editions_without_a_choice_is_an_error_listing_them(self):
        root, _ = self.tree(rich_install=True)
        with self.assertRaisesRegex(ToolError, "--install-edition.*Windows 11 Home"):
            customize_tree(root, self.opts())

    def test_install_edition_picks_that_index(self):
        root, boot = self.tree(rich_install=True)
        customize_tree(root, self.opts(install_edition=2))
        self.assertEqual(self.index_in(self.autounattend(boot)), "2")

    def test_trimming_renumbers_so_the_index_follows_the_edition(self):
        root, boot = self.tree(rich_install=True)
        customize_tree(root, self.opts(editions=[2, 3], install_edition=3))
        self.assertEqual([i.name for i in wim.images(find_ci(root, "sources/install.wim"))],
                         ["Windows 11 Home", "Windows 11 Education"])
        self.assertEqual(self.index_in(self.autounattend(boot)), "2")           # Education is now image 2

    def test_keeping_one_edition_selects_it_automatically(self):
        root, boot = self.tree(rich_install=True)
        customize_tree(root, self.opts(editions=[3]))
        self.assertEqual(self.index_in(self.autounattend(boot)), "1")

    def test_nonexistent_edition_is_rejected(self):
        root, _ = self.tree(rich_install=True)
        with self.assertRaisesRegex(ToolError, "does not exist"):
            customize_tree(root, self.opts(install_edition=9))

    def test_safe_mode_never_writes_a_disk_layout_into_the_media(self):
        root, boot = self.tree()
        report = customize_tree(root, self.opts())
        xml = self.autounattend(boot)
        self.assertNotIn("WillWipeDisk", xml)
        self.assertFalse(any("ERASE" in a for a in report.actions))

    def test_auto_partition_is_flagged_loudly_in_the_report(self):
        root, boot = self.tree()
        report = customize_tree(root, self.opts(auto_partition=True))
        self.assertTrue(any("ERASE disk 0" in a for a in report.actions))
        self.assertIn("WillWipeDisk", self.autounattend(boot))

    def test_silent_with_bypass_registry_still_uses_a_pe_answer_file(self):
        root, boot = self.tree()
        customize_tree(root, Customizations(local_account=False, silent_install=True))
        xml = self.autounattend(boot)
        self.assertNotIn("LabConfig", xml)                                      # bypass went to the registry


@unittest.skipUnless(IMG_TOOLS, "needs wimlib, hivex, sgdisk, dosfstools, mtools, syslinux")
class SilentCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = make_windows_tree(self.tmp / "media")

    def run_cli(self, *argv, inputs=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("builtins.input", side_effect=list(inputs)) as asked:
            rc = cli.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue(), asked

    def test_auto_partition_without_silent_is_an_error(self):
        rc, _, err, _ = self.run_cli("unattend", "--auto-partition")
        self.assertEqual(rc, 2)
        self.assertIn("requires silent_install", err)

    def test_auto_partition_cannot_be_combined_with_bios_boot(self):
        img = self.tmp / "a.img"
        rc, _, err, _ = self.run_cli("write", self.root, img, "--image-size", "300M", "--boot", "bios",
                                     "--silent", "--auto-partition", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("UEFI", err)
        self.assertFalse(img.exists())

    def test_wrong_acknowledgement_writes_nothing(self):
        img = self.tmp / "b.img"
        rc, out, _, asked = self.run_cli("write", self.root, img, "--image-size", "300M", "--silent",
                                         "--auto-partition", inputs=["yes"])
        self.assertEqual(rc, 1)
        self.assertIn("AUTO-PARTITION WARNING", out)
        self.assertEqual(img.read_bytes()[:512], bytes(512))

    def test_correct_acknowledgement_proceeds_and_label_reaches_the_guard(self):
        img = self.tmp / "c.img"
        rc, out, err, asked = self.run_cli("write", self.root, img, "--image-size", "300M", "--silent",
                                           "--auto-partition", "--label", "mystick", inputs=["Erase Disk 0"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(asked.call_count, 1)                    # new image file: no overwrite prompt
        self.assertIn("!! AUTO-PARTITION", out)
        boot = self.tmp / "boot.wim"
        boot.write_bytes(subprocess.run(
            ["mcopy", "-i", f"{img}@@1048576", "::sources/boot.wim", "-"], capture_output=True, check=True,
            env={"MTOOLS_SKIP_CHECK": "1", "PATH": "/usr/bin:/bin"}).stdout)
        xml = wim.extract(boot, 2, "/Autounattend.xml", Path(tempfile.mkdtemp(dir=self.tmp))).read_text()
        self.assertIn("<Label>MYSTICK</Label>", xml)

    def test_yes_skips_prompts_for_explicit_flags(self):
        img = self.tmp / "d.img"
        rc, _, err, asked = self.run_cli("write", self.root, img, "--image-size", "300M", "--silent",
                                         "--auto-partition", "--yes")
        self.assertEqual(rc, 0, err)
        asked.assert_not_called()

    def test_silent_alone_asks_nothing_destructive(self):
        img = self.tmp / "e.img"
        rc, out, err, asked = self.run_cli("write", self.root, img, "--image-size", "300M", "--silent")
        self.assertEqual(rc, 0, err)
        asked.assert_not_called()
        self.assertNotIn("AUTO-PARTITION", out)

    def test_unattend_and_profile_carry_the_new_options(self):
        prof, xml = self.tmp / "p.json", self.tmp / "u.xml"
        rc, _, err, _ = self.run_cli("unattend", "--silent", "--auto-partition", "--install-edition", "2",
                                     "--edition", "2", "--edition", "3", "--save-profile", prof, "-o", xml)
        self.assertEqual(rc, 0, err)
        loaded = Customizations.load(prof)
        self.assertEqual((loaded.silent_install, loaded.auto_partition, loaded.install_edition), (True, True, 2))
        self.assertIn("<Value>2</Value>", xml.read_text())


if __name__ == "__main__":
    unittest.main()
