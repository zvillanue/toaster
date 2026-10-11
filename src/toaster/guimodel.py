"""The GUI's brains, with no toolkit code so it can be tested without a display.

The GUI never duplicates toaster's logic. It drives the command line tool as
subprocesses:

1. ``toaster prepare`` extracts and customizes the ISO as the *normal user*
   (cancellable; no privileges needed), then
2. ``toaster flash`` writes the result. Only this step needs root, so only this
   step is run through ``pkexec`` (the desktop's password prompt). The GUI
   itself never runs as root.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import devices
from .cli import _describe
from .options import Customizations, OptionsError
from .tools import ToolError

PROGRESS_PREFIX = "PROGRESS\t"
ERASE_PHRASE = "erase disk 0"


def default_workdir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "toaster"


# --- commands ------------------------------------------------------------------------

def toaster_argv(*args: str | Path) -> list[str]:
    return [sys.executable, "-m", "toaster", *map(str, args)]


def _package_parent() -> str:
    return str(Path(__file__).resolve().parent.parent)


def elevated(argv: list[str], *, is_root: bool | None = None, which=shutil.which) -> list[str]:
    """Wrap ``argv`` so it runs as root: unchanged if we already are, else via pkexec."""
    if is_root is None:
        is_root = os.geteuid() == 0
    if is_root:
        return argv
    if which("pkexec") is None:
        raise ToolError("writing to a drive needs administrator rights, but pkexec (polkit) was not found.\n"
                        "  Install polkit, or start the GUI with sudo.")
    # pkexec starts programs with a clean environment, so hand over what Python needs to find toaster.
    return ["pkexec", "env", "PYTHONUNBUFFERED=1", f"PYTHONPATH={_package_parent()}", *argv]


def parse_progress(line: str) -> tuple[str, int, int] | None:
    """Parse ``PROGRESS<TAB>phase<TAB>done<TAB>total``; anything else returns None."""
    if not line.startswith(PROGRESS_PREFIX):
        return None
    parts = line.rstrip("\n").split("\t")
    try:
        return parts[1], int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return None


# --- job description ---------------------------------------------------------------------

@dataclass
class Job:
    source: str
    target: str                       # /dev/sdX, or the path of a disk image
    target_is_image: bool = False
    image_size: str = ""              # e.g. "16G"; used to create a new disk image
    boot: str = "uefi"
    label: str = "WINSTALL"
    verify: bool = True
    allow_fixed: bool = False
    workdir: Path = field(default_factory=default_workdir)
    opts: Customizations = field(default_factory=Customizations)

    def validate(self) -> None:
        if not self.source or not Path(self.source).expanduser().exists():
            raise ToolError("choose a Windows ISO (or an extracted folder) first")
        if not self.target:
            raise ToolError("choose a target drive or disk image")
        if self.target_is_image and not Path(self.target).exists() and not self.image_size:
            raise ToolError("enter a size for the new disk image (for example 16G)")
        if self.opts.auto_partition and self.boot == "bios":
            raise ToolError("auto-partition lays the target PC's disk out for UEFI; choose UEFI boot "
                            "or turn auto-partition off")
        self.opts.media_label = self.label.upper()
        try:
            self.opts.validate()
        except OptionsError as e:
            raise ToolError(str(e)) from None


def prepare_argv(job: Job, tree: Path, profile: Path) -> list[str]:
    return toaster_argv("prepare", Path(job.source).expanduser().resolve(), "--out", tree, "--profile", profile)


def flash_argv(job: Job, tree: Path, *, is_root: bool | None = None) -> list[str]:
    argv = toaster_argv("flash", tree, job.target, "--boot", job.boot, "--label", job.label,
                        "--yes", "--machine-progress")
    if job.verify:
        argv.append("--verify")
    if job.allow_fixed:
        argv.append("--allow-fixed")
    if job.image_size and job.target_is_image:
        argv += ["--image-size", job.image_size]
    return argv if job.target_is_image else elevated(argv, is_root=is_root)


def summary(job: Job) -> list[str]:
    where = "disk image" if job.target_is_image else "drive"
    lines = [f"Source: {job.source}",
             f"Target {where}: {job.target}",
             f"Boot mode: {'legacy BIOS + UEFI (MBR)' if job.boot == 'bios' else 'UEFI (GPT)'}",
             f"Label: {job.label.upper()}"]
    return lines + ["", "Windows options:", *(f"  - {x}" for x in _describe(job.opts))]


# --- running subprocesses ----------------------------------------------------------------------

class ProcessRunner:
    """Run a command, stream its output lines, and allow cancelling."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._lock = threading.Lock()

    def run(self, argv: list[str], on_line: Callable[[str], None]) -> int:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        with self._lock:
            if self._cancelled:
                return -signal.SIGTERM
            self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                          bufsize=1, env=env, start_new_session=True)
        proc = self._proc
        assert proc.stdout is not None
        with proc.stdout:
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
        return proc.wait()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancelled


# --- the pipeline -----------------------------------------------------------------------------------

class UI(Protocol):
    def stage(self, name: str) -> None: ...
    def log(self, line: str) -> None: ...
    def progress(self, phase: str, done: int, total: int) -> None: ...
    def confirm(self, title: str, lines: list[str], require_text: str | None) -> bool: ...


@dataclass
class Result:
    status: str                     # "done" | "cancelled" | "failed"
    message: str = ""


class Pipeline:
    """prepare (as the user) -> confirm -> flash (elevated if it is a real drive)."""

    def __init__(self, job: Job, ui: UI, runner_factory: Callable[[], ProcessRunner] = ProcessRunner,
                 is_root: bool | None = None) -> None:
        self.job, self.ui, self.is_root = job, ui, is_root
        self._runner_factory = runner_factory
        self._runner = runner_factory()
        self._flashing = False

    def cancel(self) -> bool:
        """Cancel the preparation. Returns False once writing has begun (interrupting that is unsafe)."""
        if self._flashing:
            return False
        self._runner.cancel()
        return True

    def _confirmations(self) -> bool:
        job = self.job
        lines = summary(job)
        if not job.target_is_image:
            if not self.ui.confirm("Erase this drive?", [f"Everything on {job.target} will be erased.", "", *lines],
                                   job.target):
                return False
        elif Path(job.target).exists():
            if not self.ui.confirm("Overwrite this disk image?", [f"{job.target} will be replaced.", "", *lines],
                                   job.target):
                return False
        if job.opts.auto_partition:
            if not self.ui.confirm(
                    "DANGER: auto-partition",
                    ["Booting this drive will ERASE DISK 0 of the PC you install on and install Windows there",
                     "without asking, if Setup finds this drive as disk 1.", "",
                     "Disconnect any disk you care about, and try it in a virtual machine first.", "",
                     f"Type '{ERASE_PHRASE}' to accept."], ERASE_PHRASE):
                return False
        return True

    def run(self) -> Result:
        job = self.job
        try:
            job.validate()
            if not self._confirmations():
                return Result("cancelled", "Nothing was written.")
            job.workdir.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="job-", dir=job.workdir))
        except (ToolError, OSError) as e:
            return Result("failed", str(e))
        try:
            return self._run_in(work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _run_in(self, work: Path) -> Result:
        job, ui = self.job, self.ui
        tree, profile = work / "tree", work / "profile.json"
        profile.write_text(job.opts.to_json())

        ui.stage("Preparing Windows files")
        tail: list[str] = []

        def on_prepare(line: str) -> None:
            tail.append(line)
            ui.log(line)

        rc = self._runner.run(prepare_argv(job, tree, profile), on_prepare)
        if self._runner.cancelled:
            return Result("cancelled", "Cancelled. Nothing was written.")
        if rc != 0:
            return Result("failed", _error_text(tail, "Preparing the Windows files failed."))

        try:
            argv = flash_argv(job, tree, is_root=self.is_root)
        except ToolError as e:
            return Result("failed", str(e))
        ui.stage("Writing to the drive (this cannot be cancelled)")
        self._flashing = True
        flash_tail: list[str] = []

        def on_flash(line: str) -> None:
            p = parse_progress(line)
            if p:
                ui.progress(*p)
            else:
                flash_tail.append(line)
                ui.log(line)

        rc = self._runner_factory().run(argv, on_flash)
        self._flashing = False
        if rc != 0:
            return Result("failed", _error_text(flash_tail, "Writing to the drive failed."))
        return Result("done", f"{job.target} is ready.")


def _error_text(lines: list[str], fallback: str) -> str:
    errors = [x for x in lines if "error" in x.lower()]
    return (errors[-1] if errors else (lines[-1] if lines else fallback)).strip()


# --- drive list -----------------------------------------------------------------------------------------

@dataclass
class DriveChoice:
    path: str
    text: str
    blocked: str = ""               # non-empty: why it cannot be chosen


def drive_choices(include_fixed: bool = False) -> list[DriveChoice]:
    choices = []
    for d in devices.list_drives(include_fixed=include_fixed):
        size = f"{d.size / 2**30:.1f} GiB" if d.size else "no media"
        blocked = "holds the running system" if d.holds_system else ("read-only" if d.read_only else "")
        choices.append(DriveChoice(d.path, f"{d.path}  {size}  {d.label}"
                                   + (f"  [{blocked}]" if blocked else ""), blocked))
    return choices
