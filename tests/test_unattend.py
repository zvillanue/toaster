import unittest
import xml.etree.ElementTree as ET

from toaster import tweaks
from toaster.options import Customizations, OptionsError, PRESETS, sanitize_username
from toaster.unattend import NS, WCM, build_unattend, empty_password_blob

N = {"u": NS}


def parse(u):
    return ET.fromstring(u.xml)


def passes(root):
    return {s.get("pass") for s in root.findall("u:settings", N)}


class UnattendTests(unittest.TestCase):
    def test_nothing_requested_yields_no_file(self):
        self.assertIsNone(build_unattend(Customizations(bypass_checks=[], local_account=False)))

    def test_default_pe_fallback_contains_labconfig(self):
        u = build_unattend(Customizations(local_account=False))
        self.assertTrue(u.needs_pe_pass)
        root = parse(u)
        paths = [p.text for p in root.findall(".//u:RunSynchronousCommand/u:Path", N)]
        self.assertEqual(len(paths), 3)
        self.assertTrue(all("LabConfig" in p for p in paths))
        self.assertIn("BypassTPMCheck", paths[0])

    def test_registry_bypass_drops_pe_pass(self):
        u = build_unattend(Customizations(), pe_bypass=False)
        self.assertFalse(u.needs_pe_pass)
        self.assertNotIn("windowsPE", passes(parse(u)))

    def test_local_account_sets_bypassnro_in_specialize(self):
        root = parse(build_unattend(Customizations(bypass_checks=[]), pe_bypass=False))
        self.assertEqual(passes(root), {"specialize"})
        self.assertIn("BypassNRO", ET.tostring(root, encoding="unicode"))

    def test_username_creates_account_and_forces_password_change(self):
        o = Customizations(bypass_checks=[], local_account=False, username="alex")
        root = parse(build_unattend(o))
        self.assertEqual(root.find(".//u:LocalAccount/u:Name", N).text, "alex")
        self.assertEqual(root.find(".//u:LocalAccount/u:Password/u:Value", N).text, empty_password_blob())
        cmds = [c.text for c in root.findall(".//u:SynchronousCommand/u:CommandLine", N)]
        self.assertIn('net user "alex" /logonpasswordchg:yes', cmds)
        action = root.find(".//u:LocalAccount", N).get(f"{{{WCM}}}action")
        self.assertEqual(action, "add")

    def test_blank_password_blob_matches_microsoft_constant(self):
        self.assertEqual(empty_password_blob(), "UABhAHMAcwB3AG8AcgBkAA==")

    def test_machine_tweaks_go_to_specialize_user_tweaks_to_first_logon(self):
        o = Customizations(bypass_checks=[], local_account=False, tweaks=["no-copilot"])
        root = parse(build_unattend(o))
        spec = [p.text for p in root.findall(".//u:settings[@pass='specialize']//u:Path", N)]
        logon = [c.text for c in root.findall(".//u:FirstLogonCommands//u:CommandLine", N)]
        self.assertTrue(any("TurnOffWindowsCopilot" in c for c in spec))
        self.assertTrue(any("ShowCopilotButton" in c for c in logon))
        self.assertFalse(any("HKCU" in c for c in spec))

    def test_special_characters_are_escaped_and_roundtrip(self):
        o = Customizations(bypass_checks=[], local_account=False, tweaks=["no-onedrive"])
        u = build_unattend(o)
        root = ET.fromstring(u.xml)       # would raise on bad escaping
        texts = [p.text for p in root.findall(".//u:Path", N)]
        self.assertTrue(any("'" in t or '"' in t for t in texts))

    def test_every_tweak_generates_wellformed_xml(self):
        o = Customizations(tweaks=[t.id for t in tweaks.TWEAKS], username="sam", locale="en-GB",
                           timezone="GMT Standard Time", no_data_collection=True,
                           disable_bitlocker=True, offline_internal_drives=True)
        root = parse(build_unattend(o, "arm64"))
        self.assertEqual(passes(root), {"windowsPE", "specialize", "oobeSystem", "offlineServicing"})
        arches = {c.get("processorArchitecture") for c in root.iter(f"{{{NS}}}component")}
        self.assertEqual(arches, {"arm64"})

    def test_commands_are_ordered_from_one(self):
        o = Customizations(bypass_checks=[], local_account=True, tweaks=["no-onedrive", "no-copilot"])
        root = parse(build_unattend(o))
        orders = [int(e.text) for e in root.findall(".//u:settings[@pass='specialize']//u:Order", N)]
        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_duplicate_machine_commands_are_collapsed(self):
        o = Customizations(bypass_checks=[], local_account=False, tweaks=["no-consumer-content", "no-consumer-content"])
        root = parse(build_unattend(o))
        paths = [p.text for p in root.findall(".//u:settings[@pass='specialize']//u:Path", N)]
        self.assertEqual(len(paths), len(set(paths)))

    def test_default_tweak_is_registry_command(self):
        cmd = tweaks.get("classic-context-menu").values[0].command()
        self.assertIn("/ve", cmd)
        self.assertIn('/d ""', cmd)


class OptionsTests(unittest.TestCase):
    def test_reserved_and_invalid_usernames(self):
        with self.assertRaises(OptionsError):
            sanitize_username("Administrator")
        with self.assertRaises(OptionsError):
            sanitize_username("a" * 21)
        self.assertEqual(sanitize_username("a/b:c"), "a_b_c")

    def test_unknown_options_rejected(self):
        with self.assertRaises(OptionsError):
            Customizations(bypass_checks=["nope"]).validate()
        with self.assertRaises(OptionsError):
            Customizations(tweaks=["nope"]).validate()
        with self.assertRaises(OptionsError):
            Customizations.from_dict({"bogus": 1})

    def test_profile_roundtrip(self):
        import json
        o = PRESETS["debloat"]()
        self.assertEqual(Customizations.from_dict(json.loads(o.to_json())), o)

    def test_presets_are_valid(self):
        for make in PRESETS.values():
            make().validate()


if __name__ == "__main__":
    unittest.main()
