import contextlib
import io
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from toaster import cli, devices, guimodel, media
from toaster.guimodel import ERASE_PHRASE, Job, Pipeline, ProcessRunner
from toaster.options import Customizations
from toaster.tools import ToolError

from .fakemedia import make_windows_tree
from .test_media_devices_cli import IMG_TOOLS


class FakeUI:
    def __init__(self, answers=True):
        self.answers = answers if isinstance(answers, list) else None
        self.default = answers if isinstance(answers, bool) else True
        self.stages, self.logs, self.progress_events, self.confirms = [], [], [], []

    def stage(self, name): self.stages.append(name)
    def log(self, line): self.logs.append(line)
    def progress(self, phase, done, total): self.progress_events.append((phase, done, total))

    def confirm(self, title, lines, require_text):
        self.confirms.append((title, require_text))
        return self.answers.pop(0) if self.answers else self.default


class FakeRunner:
    """Stands in for ProcessRunner: records argv, emits canned lines, returns a canned exit code."""
    instances = []

    def __init__(self, lines=(), rc=0, block=None):
        self.lines, self.rc, self.block = lines, rc, block
        self.argvs, self.cancelled = [], False
        FakeRunner.instances.append(self)

    def run(self, argv, on_line):
        self.argvs.append(argv)
        for line in self.lines:
            on_line(line)
        if self.block:
            self.block.wait(5)
        return self.rc

    def cancel(self):
        self.cancelled = True
        if self.block:
            self.block.set()


def factory_of(*runners):
    it = iter(runners)
    return lambda: next(it)


class CommandTests(unittest.TestCase):
    def test_elevation_is_skipped_for_root_and_uses_pkexec_otherwise(self):
        argv = ["python3", "-m", "toaster", "flash"]
        self.assertEqual(guimodel.elevated(argv, is_root=True), argv)
        wrapped = guimodel.elevated(argv, is_root=False, which=lambda n: "/usr/bin/pkexec")
        self.assertEqual(wrapped[:2], ["pkexec", "env"])
        self.assertIn("PYTHONUNBUFFERED=1", wrapped)
        self.assertTrue(any(a.startswith("PYTHONPATH=") for a in wrapped))
        self.assertEqual(wrapped[-4:], argv)

    def test_missing_pkexec_explains_what_to_do(self):
        with self.assertRaisesRegex(ToolError, "polkit"):
            guimodel.elevated(["x"], is_root=False, which=lambda n: None)

    def test_pythonpath_given_to_pkexec_can_import_toaster(self):
        path = [a for a in guimodel.elevated(["x"], is_root=False, which=lambda n: "p") if a.startswith("PYTHONPATH=")][0]
        self.assertTrue((Path(path.split("=", 1)[1]) / "toaster" / "__init__.py").exists())

    def test_progress_parsing(self):
        self.assertEqual(guimodel.parse_progress("PROGRESS\tcopy\t10\t200"), ("copy", 10, 200))
        for bad in ("copy 10 200", "PROGRESS\tcopy\tx\t200", "PROGRESS\tcopy", "hello"):
            self.assertIsNone(guimodel.parse_progress(bad))

    def test_flash_argv_for_a_real_drive_is_elevated_and_for_an_image_is_not(self):
        job = Job("a.iso", "/dev/sdb", boot="bios", label="MYSTICK", verify=True, allow_fixed=True)
        argv = guimodel.flash_argv(job, Path("/w/tree"), is_root=True)
        self.assertEqual(argv[1:5], ["-m", "toaster", "flash", "/w/tree"])
        for expected in ("/dev/sdb", "bios", "MYSTICK", "--yes", "--machine-progress", "--verify", "--allow-fixed"):
            self.assertIn(expected, argv)
        img = Job("a.iso", "/x/win.img", target_is_image=True, image_size="16G", verify=False)
        iargv = guimodel.flash_argv(img, Path("/w/tree"), is_root=False)
        self.assertEqual(iargv[0], sys.executable)                   # no pkexec for images
        self.assertEqual(iargv[iargv.index("--image-size") + 1], "16G")
        self.assertNotIn("--verify", iargv)

    def test_prepare_argv_passes_the_profile_not_individual_flags(self):
        with tempfile.TemporaryDirectory() as t:
            iso = Path(t) / "w.iso"
            iso.write_bytes(b"x")
            argv = guimodel.prepare_argv(Job(str(iso), "/dev/sdb"), Path(t) / "tree", Path(t) / "p.json")
        self.assertEqual(argv[3], "prepare")
        self.assertEqual(argv[argv.index("--profile") + 1], str(Path(t) / "p.json"))

    def test_job_validation(self):
        with tempfile.TemporaryDirectory() as t:
            iso = Path(t) / "w.iso"
            iso.write_bytes(b"x")
            with self.assertRaisesRegex(ToolError, "choose a Windows ISO"):
                Job("", "/dev/sdb").validate()
            with self.assertRaisesRegex(ToolError, "choose a Windows ISO"):
                Job(str(Path(t) / "missing.iso"), "/dev/sdb").validate()
            with self.assertRaisesRegex(ToolError, "target"):
                Job(str(iso), "").validate()
            with self.assertRaisesRegex(ToolError, "size"):
                Job(str(iso), str(Path(t) / "new.img"), target_is_image=True).validate()
            with self.assertRaisesRegex(ToolError, "UEFI"):
                Job(str(iso), "/dev/sdb", boot="bios",
                    opts=Customizations(silent_install=True, auto_partition=True)).validate()
            with self.assertRaisesRegex(ToolError, "auto_partition requires"):
                Job(str(iso), "/dev/sdb", opts=Customizations(auto_partition=True)).validate()
            job = Job(str(iso), "/dev/sdb", label="mystick")
            job.validate()
            self.assertEqual(job.opts.media_label, "MYSTICK")        # reaches the auto-partition guard

    def test_summary_lists_target_and_options(self):
        text = "\n".join(guimodel.summary(Job("a.iso", "/dev/sdb", opts=Customizations(username="sam"))))
        self.assertIn("/dev/sdb", text)
        self.assertIn("UEFI (GPT)", text)
        self.assertIn("'sam'", text)

    def test_drive_choices_flag_unsafe_disks(self):
        drives = [devices.Drive("/dev/sda", 2**38, mountpoints=["/"], transport="sata"),
                  devices.Drive("/dev/sdb", 16 * 2**30, model="Stick", transport="usb", read_only=True),
                  devices.Drive("/dev/sdc", 8 * 2**30, model="OK", transport="usb")]
        with mock.patch.object(devices, "list_drives", return_value=drives):
            c = {x.path: x for x in guimodel.drive_choices()}
        self.assertEqual(c["/dev/sda"].blocked, "holds the running system")
        self.assertEqual(c["/dev/sdb"].blocked, "read-only")
        self.assertEqual(c["/dev/sdc"].blocked, "")
        self.assertIn("8.0 GiB", c["/dev/sdc"].text)


class ProcessRunnerTests(unittest.TestCase):
    def test_streams_lines_and_returns_exit_code(self):
        lines = []
        rc = ProcessRunner().run([sys.executable, "-c", "print('a'); print('b'); import sys; sys.exit(3)"], lines.append)
        self.assertEqual((lines, rc), (["a", "b"], 3))

    def test_stderr_is_merged_into_the_stream(self):
        lines = []
        ProcessRunner().run([sys.executable, "-c", "import sys; sys.stderr.write('oops\\n')"], lines.append)
        self.assertEqual(lines, ["oops"])

    def test_output_is_delivered_as_it_happens_not_at_exit(self):
        seen = []
        code = "import time; print('first', flush=True); time.sleep(1.5); print('second', flush=True)"
        start = time.monotonic()
        stamps = []
        ProcessRunner().run([sys.executable, "-c", code], lambda l: stamps.append((l, time.monotonic() - start)))
        self.assertLess(stamps[0][1], 1.2)
        self.assertGreater(stamps[1][1], 1.2)

    def test_cancel_terminates_the_whole_process_group(self):
        runner = ProcessRunner()
        code = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "print('started', flush=True); time.sleep(60)")
        result = {}
        t = threading.Thread(target=lambda: result.update(rc=runner.run([sys.executable, "-c", code], lambda l: None)))
        t.start()
        time.sleep(1)
        runner.cancel()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["rc"], -signal.SIGTERM)
        self.assertTrue(runner.cancelled)

    def test_cancel_before_start_never_launches(self):
        runner = ProcessRunner()
        runner.cancel()
        self.assertEqual(runner.run([sys.executable, "-c", "open('/tmp/should-not-exist-toaster','w')"], print), -signal.SIGTERM)
        self.assertFalse(os.path.exists("/tmp/should-not-exist-toaster"))


class PipelineLogicTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.iso = self.tmp / "w.iso"
        self.iso.write_bytes(b"x")
        self.work = self.tmp / "work"

    def job(self, **kw):
        kw.setdefault("workdir", self.work)
        return Job(str(self.iso), "/dev/sdz", **kw)

    def test_drive_target_asks_for_the_device_path_then_proceeds(self):
        ui, prep, flash = FakeUI(), FakeRunner(["Extracting w.iso..."]), FakeRunner(["PROGRESS\tcopy\t5\t10", "note"])
        res = Pipeline(self.job(), ui, factory_of(prep, flash), is_root=True).run()
        self.assertEqual(res.status, "done")
        self.assertEqual(ui.confirms, [("Erase this drive?", "/dev/sdz")])
        self.assertEqual(ui.stages[0], "Preparing Windows files")
        self.assertEqual(ui.progress_events, [("copy", 5, 10)])
        self.assertEqual(ui.logs, ["Extracting w.iso...", "note"])          # progress lines are not logged
        self.assertEqual(prep.argvs[0][3], "prepare")
        self.assertEqual(flash.argvs[0][3], "flash")

    def test_declining_the_confirmation_runs_nothing(self):
        ui, prep = FakeUI(False), FakeRunner()
        res = Pipeline(self.job(), ui, factory_of(prep), is_root=True).run()
        self.assertEqual(res.status, "cancelled")
        self.assertEqual(prep.argvs, [])
        self.assertFalse(self.work.exists())

    def test_auto_partition_needs_a_second_explicit_confirmation(self):
        opts = Customizations(silent_install=True, auto_partition=True)
        ui = FakeUI([True, False])
        res = Pipeline(self.job(opts=opts), ui, factory_of(FakeRunner()), is_root=True).run()
        self.assertEqual(res.status, "cancelled")
        self.assertEqual([c[1] for c in ui.confirms], ["/dev/sdz", ERASE_PHRASE])

    def test_new_image_needs_no_confirmation_but_an_existing_one_does(self):
        new = Job(str(self.iso), str(self.tmp / "new.img"), target_is_image=True, image_size="1G", workdir=self.work)
        ui = FakeUI()
        Pipeline(new, ui, factory_of(FakeRunner(), FakeRunner())).run()
        self.assertEqual(ui.confirms, [])
        (self.tmp / "old.img").write_bytes(b"x")
        old = Job(str(self.iso), str(self.tmp / "old.img"), target_is_image=True, workdir=self.work)
        ui = FakeUI()
        Pipeline(old, ui, factory_of(FakeRunner(), FakeRunner())).run()
        self.assertEqual(ui.confirms[0][0], "Overwrite this disk image?")

    def test_prepare_failure_reports_the_error_line_and_skips_writing(self):
        prep = FakeRunner(["Extracting...", "toaster: error: not enough free space", "trailing"], rc=2)
        flash = FakeRunner()
        res = Pipeline(self.job(), FakeUI(), factory_of(prep, flash), is_root=True).run()
        self.assertEqual(res, guimodel.Result("failed", "toaster: error: not enough free space"))
        self.assertEqual(flash.argvs, [])

    def test_flash_failure_is_reported(self):
        res = Pipeline(self.job(), FakeUI(), factory_of(FakeRunner(), FakeRunner(["toaster: error: boom"], rc=2)),
                       is_root=True).run()
        self.assertEqual((res.status, res.message), ("failed", "toaster: error: boom"))

    def test_missing_polkit_fails_cleanly_after_preparing(self):
        with mock.patch("shutil.which", return_value=None):
            res = Pipeline(self.job(), FakeUI(), factory_of(FakeRunner(), FakeRunner()), is_root=False).run()
        self.assertEqual(res.status, "failed")
        self.assertIn("polkit", res.message)

    def test_workdir_is_always_cleaned_up(self):
        for rc in (0, 2):
            Pipeline(self.job(), FakeUI(), factory_of(FakeRunner(rc=rc), FakeRunner()), is_root=True).run()
            self.assertEqual(list(self.work.iterdir()), [])

    def test_profile_handed_to_prepare_contains_the_options(self):
        seen = {}

        class Peek(FakeRunner):
            def run(self, argv, on_line):
                seen["profile"] = Path(argv[argv.index("--profile") + 1]).read_text()
                return 0
        Pipeline(self.job(label="mystick", opts=Customizations(username="sam", tweaks=["no-copilot"])),
                 FakeUI(), factory_of(Peek(), FakeRunner()), is_root=True).run()
        self.assertIn('"username": "sam"', seen["profile"])
        self.assertIn('"media_label": "MYSTICK"', seen["profile"])

    def test_cancel_during_preparation(self):
        block = threading.Event()
        prep = FakeRunner(block=block)
        pipe = Pipeline(self.job(), FakeUI(), factory_of(prep, FakeRunner()), is_root=True)
        out = {}
        t = threading.Thread(target=lambda: out.update(res=pipe.run()))
        t.start()
        time.sleep(0.5)
        self.assertTrue(pipe.cancel())
        t.join(5)
        prep.cancelled = True            # the real runner reports this after cancel(); mimic it
        self.assertFalse(t.is_alive())

    def test_cancel_is_refused_once_writing_has_started(self):
        block, started = threading.Event(), threading.Event()

        class Flash(FakeRunner):
            def run(self, argv, on_line):
                started.set()
                block.wait(5)
                return 0
        pipe = Pipeline(self.job(), FakeUI(), factory_of(FakeRunner(), Flash()), is_root=True)
        t = threading.Thread(target=pipe.run)
        t.start()
        self.assertTrue(started.wait(5))
        self.assertFalse(pipe.cancel())
        block.set()
        t.join(5)


@unittest.skipUnless(IMG_TOOLS, "needs wimlib, hivex, sgdisk, dosfstools, mtools, syslinux")
class PipelineEndToEndTests(unittest.TestCase):
    """The GUI's pipeline against the real CLI, writing a disk image."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.media_dir = make_windows_tree(self.tmp / "media")
        env = {"PYTHONPATH": guimodel._package_parent()}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prepare_and_flash_into_a_disk_image(self):
        img = self.tmp / "gui.img"
        job = Job(str(self.media_dir), str(img), target_is_image=True, image_size="300M", boot="bios",
                  label="guitest", workdir=self.tmp / "work",
                  opts=Customizations(username="sam", tweaks=["no-copilot"]))
        ui = FakeUI()
        res = Pipeline(job, ui).run()
        self.assertEqual(res.status, "done", res.message + "\n".join(ui.logs))
        phases = {p for p, _, _ in ui.progress_events}
        self.assertTrue({"copy", "verify", "done"} <= phases)
        self.assertEqual(ui.progress_events[-1][1], ui.progress_events[-1][2])
        self.assertTrue(any("Wrote HKLM" in l for l in ui.logs))                # prepare output reached the log
        self.assertEqual(img.read_bytes()[510:512], b"\x55\xaa")
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_a_failing_prepare_surfaces_the_cli_error(self):
        job = Job(str(self.tmp / "empty"), str(self.tmp / "x.img"), target_is_image=True, image_size="300M",
                  workdir=self.tmp / "work")
        (self.tmp / "empty").mkdir()
        res = Pipeline(job, FakeUI()).run()
        self.assertEqual(res.status, "failed")
        self.assertFalse((self.tmp / "x.img").exists())


@unittest.skipUnless(IMG_TOOLS, "needs wimlib, hivex, sgdisk, dosfstools, mtools, syslinux")
class FlashCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.tree = make_windows_tree(self.tmp / "media")

    def run_cli(self, *argv, inputs=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("builtins.input", side_effect=list(inputs)):
            rc = cli.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def test_machine_progress_lines_are_parseable_and_monotonic(self):
        rc, out, err = self.run_cli("flash", self.tree, self.tmp / "m.img", "--image-size", "300M", "--machine-progress",
                                    "--verify")
        self.assertEqual(rc, 0, err)
        events = [guimodel.parse_progress(l) for l in out.splitlines() if l.startswith("PROGRESS")]
        self.assertTrue(events and all(events))
        copies = [d for p, d, _ in events if p == "copy"]
        self.assertEqual(copies, sorted(copies))
        self.assertEqual(events[-1][0], "done")

    def test_human_progress_goes_to_stderr_without_the_flag(self):
        rc, out, err = self.run_cli("flash", self.tree, self.tmp / "h.img", "--image-size", "300M")
        self.assertEqual(rc, 0)
        self.assertNotIn("PROGRESS", out)
        self.assertIn("copy", err)

    def test_flash_rejects_a_non_folder(self):
        rc, _, err = self.run_cli("flash", self.tmp / "nope", self.tmp / "x.img", "--image-size", "300M")
        self.assertEqual(rc, 2)
        self.assertIn("toaster prepare", err)

    def test_flash_confirms_before_overwriting_an_existing_image(self):
        img = self.tmp / "e.img"
        img.write_bytes(bytes(300 * 2**20))
        rc, out, _ = self.run_cli("flash", self.tree, img, inputs=["wrong"])
        self.assertEqual(rc, 1)
        self.assertEqual(img.read_bytes()[:512], bytes(512))


if __name__ == "__main__":
    unittest.main()
