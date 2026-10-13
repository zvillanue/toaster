"""GUI tests. They need tkinter and a display, e.g.:

    PYTHONPATH=src xvfb-run -a /usr/bin/python3.12 -m unittest tests.test_gui

and are skipped otherwise.
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk
    from toaster import gui
    HAVE_TK = bool(os.environ.get("DISPLAY"))
except ImportError:                                  # no python3-tk
    HAVE_TK = False

from toaster import devices, guimodel
from toaster.options import PRESETS, Customizations, OptionsError
from toaster.tools import ToolError

from .fakemedia import make_windows_tree
from .test_guimodel import FakeRunner, factory_of
from .test_media_devices_cli import IMG_TOOLS


@unittest.skipUnless(HAVE_TK, "needs tkinter and a display (use xvfb-run)")
class GuiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = tk.Tk()
        self.addCleanup(lambda: self.root.winfo_exists() and self.root.destroy())
        self.dialogs = []
        for name in ("showinfo", "showerror"):
            p = mock.patch.object(gui.messagebox, name, side_effect=lambda *a, n=name, **k: self.dialogs.append((n, a)))
            p.start()
            self.addCleanup(p.stop)

    def make_app(self, **kw):
        self.confirms = []
        kw.setdefault("confirm", lambda t, lines, req: self.confirms.append((t, req)) or True)
        with mock.patch.object(devices, "list_drives", return_value=[]):
            return gui.App(self.root, **kw)

    def pump(self, cond, timeout=60.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if cond():
                return True
            time.sleep(0.02)
        return False


class FormTests(GuiTestCase):
    def test_defaults_match_the_minimal_preset(self):
        app = self.make_app()
        self.assertEqual(app.collect_options(), PRESETS["minimal"]())

    def test_options_roundtrip_through_the_widgets(self):
        app = self.make_app()
        wanted = Customizations(bypass_checks=["tpm", "cpu"], local_account=False, username="sam",
                                no_data_collection=True, disable_bitlocker=True, offline_internal_drives=True,
                                timezone="GMT Standard Time", locale="en-GB", tweaks=["no-copilot", "taskbar-left"],
                                offline_debloat=True, editions=[2, 3], secureboot_2023=True,
                                drivers=["/a", "/b"], keep_drivers=True, silent_install=True,
                                auto_partition=True, install_edition=3)
        app.apply_options(wanted)
        got = app.collect_options()
        wanted.media_label = got.media_label                      # label comes from the job, not the form
        self.assertEqual(got, wanted)

    def test_presets_replace_checks_and_tweaks_but_keep_personal_settings(self):
        app = self.make_app()
        app.username_var.set("sam")
        app.drivers = ["/d"]
        app.apply_preset("debloat")
        o = app.collect_options()
        self.assertEqual(set(o.tweaks), set(PRESETS["debloat"]().tweaks))
        self.assertTrue(o.disable_bitlocker)
        self.assertEqual((o.username, o.drivers), ("sam", ["/d"]))
        app.apply_preset("none")
        o = app.collect_options()
        self.assertEqual((o.bypass_checks, o.tweaks, o.local_account), ([], [], False))
        self.assertEqual(o.username, "sam")

    def test_saved_profile_loads_back_into_the_form(self):
        app = self.make_app()
        app.apply_options(PRESETS["debloat"]())
        path = self.tmp / "p.json"
        path.write_text(app.collect_options().to_json())
        other = self.make_app()
        other.apply_options(Customizations.load(path))
        self.assertEqual(other.collect_options(), app.collect_options())

    def test_tweak_headings_appear_once_each(self):
        app = self.make_app()
        headings = []

        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, gui.ttk.Label) and str(c.cget("style")) == "Heading.TLabel":
                    headings.append(c.cget("text"))
                walk(c)
        walk(app.tabs)
        self.assertEqual(len(headings), len(set(headings)))
        self.assertTrue({"Bloat", "Privacy", "AI", "UI"} <= set(headings))

    def test_every_catalog_tweak_has_a_checkbox(self):
        app = self.make_app()
        from toaster import tweaks
        self.assertEqual(set(app.tweak_vars), {t.id for t in tweaks.TWEAKS})


class StateTests(GuiTestCase):
    def test_auto_partition_needs_silent_and_uefi(self):
        app = self.make_app()
        self.assertTrue(app.auto_check.instate(["disabled"]))
        app.silent_var.set(True)
        self.root.update()
        self.assertTrue(app.auto_check.instate(["!disabled"]))
        app.auto_partition_var.set(True)
        self.root.update()
        self.assertEqual(app.auto_warning.winfo_manager(), "grid")           # the warning is shown
        app.boot_var.set("bios")
        self.root.update()
        self.assertFalse(app.auto_partition_var.get())                       # cleared, not just hidden
        self.assertTrue(app.auto_check.instate(["disabled"]))
        self.assertEqual(app.auto_warning.winfo_manager(), "")

    def test_unticking_silent_clears_auto_partition_and_the_option_is_not_collected(self):
        app = self.make_app()
        app.silent_var.set(True)
        app.auto_partition_var.set(True)
        app.silent_var.set(False)
        self.root.update()
        self.assertFalse(app.auto_partition_var.get())
        o = app.collect_options()
        self.assertEqual((o.silent_install, o.auto_partition, o.install_edition), (False, False, None))

    def test_device_and_image_rows_swap(self):
        app = self.make_app()
        self.root.update()
        self.assertEqual(app.device_row.winfo_manager(), "grid")
        self.assertEqual(app.image_row.winfo_manager(), "")
        app.target_kind.set("image")
        self.root.update()
        self.assertEqual((app.device_row.winfo_manager(), app.image_row.winfo_manager()), ("", "grid"))


class JobTests(GuiTestCase):
    def test_no_drive_selected_gives_a_helpful_message(self):
        app = self.make_app()
        with self.assertRaisesRegex(ToolError, "plug it in"):
            app.make_job()

    def test_selected_drive_text_maps_to_its_path_and_flags_carry_over(self):
        app = self.make_app()
        app.drive_text_to_path = {"/dev/sdb  16.0 GiB  Stick": "/dev/sdb"}
        app.device_var.set("/dev/sdb  16.0 GiB  Stick")
        app.iso_var.set("/x/w.iso")
        app.boot_var.set("bios")
        app.label_var.set("mystick")
        app.verify_var.set(False)
        app.workdir_var.set(str(self.tmp / "w"))
        job = app.make_job()
        self.assertEqual((job.target, job.target_is_image, job.boot, job.label, job.verify),
                         ("/dev/sdb", False, "bios", "mystick", False))
        self.assertEqual(job.workdir, self.tmp / "w")

    def test_image_target(self):
        app = self.make_app()
        app.target_kind.set("image")
        app.image_var.set(str(self.tmp / "a.img"))
        app.image_size_var.set("8G")
        job = app.make_job()
        self.assertEqual((job.target, job.target_is_image, job.image_size), (str(self.tmp / "a.img"), True, "8G"))

    def test_bad_numbers_are_reported_in_plain_language(self):
        app = self.make_app()
        app.target_kind.set("image")
        app.image_var.set("x.img")
        app.editions_var.set("2, two")
        with self.assertRaisesRegex(ToolError, "editions: 'two' is not a number"):
            app.make_job()

    def test_install_edition_accepts_the_dropdown_text(self):
        app = self.make_app()
        app.silent_var.set(True)
        app.install_edition_var.set("2: Windows 11 Home")
        self.assertEqual(app.collect_options().install_edition, 2)
        app.install_edition_var.set("pick one")
        with self.assertRaises(OptionsError):
            app.collect_options()

    def test_start_with_missing_iso_shows_an_error_and_does_not_run(self):
        app = self.make_app()
        app.target_kind.set("image")
        app.image_var.set(str(self.tmp / "a.img"))
        app.start()
        self.assertEqual(self.dialogs[-1][0], "showerror")
        self.assertIn("choose a Windows ISO", self.dialogs[-1][1][1])
        self.assertFalse(app.busy)


class DialogTests(GuiTestCase):
    def dialog(self, require, **kw):
        d = gui.ConfirmDialog(self.root, "t", ["line"], require, **kw)
        self.addCleanup(lambda: d.winfo_exists() and d.destroy())
        return d

    def test_device_path_must_match_exactly(self):
        d = self.dialog("/dev/sdb")
        self.assertTrue(d.ok.instate(["disabled"]))
        for typed, ok in (("/dev/sdc", False), ("/dev/SDB", False), ("/dev/sdb", True), ("  /dev/sdb ", True)):
            d.entry_var.set(typed)
            self.root.update()
            self.assertEqual(d.ok.instate(["!disabled"]), ok, typed)

    def test_erase_phrase_is_case_insensitive(self):
        d = self.dialog(guimodel.ERASE_PHRASE)
        d.entry_var.set("Erase Disk 0")
        self.assertTrue(d.matches())
        d.entry_var.set("erase disk 1")
        self.assertFalse(d.matches())

    def test_result_is_false_unless_confirmed(self):
        d = self.dialog(None)
        self.assertFalse(d.result)
        d.destroy()
        self.assertFalse(d.result)

    def test_ok_sets_result(self):
        d = self.dialog(None)
        d._ok()
        self.assertTrue(d.result)


class InspectTests(GuiTestCase):
    @unittest.skipUnless(IMG_TOOLS, "needs wimlib")
    def test_inspect_fills_in_the_edition_list_and_selection_feeds_the_field(self):
        tree = make_windows_tree(self.tmp / "media", rich_install=True)
        app = self.make_app()
        app.iso_var.set(str(tree))
        app.inspect()
        self.assertTrue(self.pump(lambda: app.edition_list.size() == 3, 20))
        self.assertIn("3 edition(s)", app.inspect_var.get())
        app.edition_list.selection_set(1)
        app.edition_list.selection_set(2)
        app.edition_list.event_generate("<<ListboxSelect>>")
        self.root.update()
        self.assertEqual(app.editions_var.get(), "2, 3")
        self.assertEqual(app.collect_options().editions, [2, 3])
        self.assertEqual(len(app.install_edition_combo.cget("values")), 3)

    def test_inspect_failure_is_reported(self):
        app = self.make_app()
        app.iso_var.set(str(self.tmp / "nothing.iso"))
        app.inspect()
        self.assertTrue(self.pump(lambda: self.dialogs, 20))
        self.assertEqual(self.dialogs[-1][0], "showerror")
        self.assertTrue(app.inspect_btn.instate(["!disabled"]))


class RunTests(GuiTestCase):
    def prepare_form(self, app, tree, img):
        app.iso_var.set(str(tree))
        app.target_kind.set("image")
        app.image_var.set(str(img))
        app.image_size_var.set("300M")
        app.workdir_var.set(str(self.tmp / "work"))
        app.label_var.set("guitest")

    @unittest.skipUnless(IMG_TOOLS, "needs wimlib, hivex, sgdisk, dosfstools, mtools, syslinux")
    def test_full_run_through_the_window_writes_a_disk_image(self):
        tree = make_windows_tree(self.tmp / "media")
        img = self.tmp / "gui.img"
        app = self.make_app()
        self.prepare_form(app, tree, img)
        app.boot_var.set("bios")
        app.apply_preset("debloat")
        app.username_var.set("sam")
        with mock.patch.dict(os.environ, {"PYTHONPATH": guimodel._package_parent()}):
            app.start()
            self.assertTrue(app.busy)
            self.assertTrue(app.cancel_btn.instate(["!disabled"]))
            self.assertTrue(self.pump(lambda: not app.busy, 120), "run did not finish")
        self.assertEqual(app.status_var.get(), "Finished")
        self.assertEqual(self.dialogs[-1][0], "showinfo")
        self.assertEqual(float(app.bar.cget("value")), 100.0)
        log = app.log_text.get("1.0", "end")
        self.assertIn("Wrote HKLM", log)                                     # prepare output streamed in
        self.assertEqual(img.read_bytes()[510:512], b"\x55\xaa")
        self.assertTrue(app.start_btn.instate(["!disabled"]))
        self.assertTrue(app.cancel_btn.instate(["disabled"]))
        self.assertEqual(self.confirms, [])                                   # a brand-new image needs none

    def test_declining_the_confirmation_cancels_without_running_anything(self):
        img = self.tmp / "exists.img"
        img.write_bytes(b"x")
        iso = self.tmp / "w.iso"
        iso.write_bytes(b"x")
        prep = FakeRunner()
        app = self.make_app(confirm=lambda t, lines, req: False, runner_factory=factory_of(prep))
        self.prepare_form(app, iso, img)
        app.start()
        self.assertTrue(self.pump(lambda: not app.busy, 20))
        self.assertEqual(app.status_var.get(), "Cancelled")
        self.assertEqual(prep.argvs, [])

    def test_failure_is_shown_in_red_and_in_a_dialog(self):
        iso = self.tmp / "w.iso"
        iso.write_bytes(b"x")
        prep = FakeRunner(["toaster: error: not enough free space"], rc=2)
        app = self.make_app(runner_factory=factory_of(prep))
        self.prepare_form(app, iso, self.tmp / "n.img")
        app.start()
        self.assertTrue(self.pump(lambda: not app.busy, 20))
        self.assertEqual(app.status_var.get(), "Failed")
        self.assertEqual(self.dialogs[-1], ("showerror", ("toaster", "toaster: error: not enough free space")))
        self.assertTrue(app.log_text.tag_ranges("error"))

    def test_cancel_button_stops_preparation(self):
        iso = self.tmp / "w.iso"
        iso.write_bytes(b"x")
        block = threading.Event()
        prep = FakeRunner(["Extracting..."], block=block)
        app = self.make_app(runner_factory=factory_of(prep, FakeRunner()))
        self.prepare_form(app, iso, self.tmp / "n.img")
        app.start()
        self.assertTrue(self.pump(lambda: "Extracting" in app.log_text.get("1.0", "end"), 20))
        app.cancel()
        self.assertTrue(self.pump(lambda: not app.busy, 20))
        self.assertEqual(app.status_var.get(), "Cancelled")
        self.assertTrue(prep.cancelled)

    def test_progress_messages_drive_the_bar(self):
        iso = self.tmp / "w.iso"
        iso.write_bytes(b"x")
        flash = FakeRunner(["PROGRESS\tcopy\t50\t200"])
        app = self.make_app(runner_factory=factory_of(FakeRunner(), flash))
        self.prepare_form(app, iso, self.tmp / "n.img")
        seen = []
        orig = app._handle
        app._handle = lambda msg: (seen.append((msg[0], app.status_var.get())), orig(msg))[1]
        app.start()
        self.assertTrue(self.pump(lambda: not app.busy, 20))
        self.assertIn("progress", [k for k, _ in seen])
        self.assertTrue(any("Copying files" in s or s == "Finished" for _, s in seen))

    def test_second_start_while_busy_is_ignored(self):
        iso = self.tmp / "w.iso"
        iso.write_bytes(b"x")
        block = threading.Event()
        runner = FakeRunner(block=block)
        app = self.make_app(runner_factory=factory_of(runner, FakeRunner()))
        self.prepare_form(app, iso, self.tmp / "n.img")
        app.start()
        app.start()
        block.set()
        self.assertTrue(self.pump(lambda: not app.busy, 20))
        self.assertEqual(len(runner.argvs), 1)


if __name__ == "__main__":
    unittest.main()
