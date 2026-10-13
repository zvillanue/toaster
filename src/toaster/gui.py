"""Graphical front end (tkinter). Needs the ``python3-tk`` package on Debian/Ubuntu.

All real work is done by ``toaster.guimodel`` driving the command line tool; this
module only builds the window, turns it into a ``Job`` and shows what happens.
"""
from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Callable

from . import __version__, media, tweaks
from .guimodel import ERASE_PHRASE, Job, Pipeline, Result, default_workdir, drive_choices
from .options import BYPASS_CHECKS, PRESETS, Customizations, OptionsError
from .tools import ToolError

DANGER = "#b00020"
MUTED = "#555555"


# --- small widgets -------------------------------------------------------------------------------

class ScrollFrame(ttk.Frame):
    """A vertically scrollable frame; put children in ``.body``."""
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for widget in (self.canvas, self.body):
            widget.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
            widget.bind("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
            widget.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))


class ConfirmDialog(tk.Toplevel):
    """Modal confirmation, optionally requiring the user to type something. Cancel is the default."""
    def __init__(self, parent: tk.Misc, title: str, lines: list[str], require_text: str | None,
                 danger: bool = False) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.result = False
        self._require = require_text
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, width=78, height=min(18, max(6, len(lines) + 1)), wrap="word", relief="flat",
                       background=self.cget("background"), foreground=DANGER if danger else "black")
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        self.entry_var = tk.StringVar()
        buttons = ttk.Frame(frame)
        if require_text:
            ttk.Label(frame, text=f"Type  {require_text}  to continue:").pack(anchor="w", pady=(10, 2))
            self.entry = ttk.Entry(frame, textvariable=self.entry_var)
            self.entry.pack(fill="x")
            self.entry_var.trace_add("write", lambda *a: self._sync())
        buttons.pack(fill="x", pady=(12, 0))
        self.ok = ttk.Button(buttons, text="Continue", command=self._ok)
        self.cancel = ttk.Button(buttons, text="Cancel", command=self.destroy)
        self.ok.pack(side="right")
        self.cancel.pack(side="right", padx=8)
        self._sync()
        self.bind("<Escape>", lambda e: self.destroy())
        self.cancel.focus_set()
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 3)
        self.geometry(f"+{x}+{y}")
        self.grab_set()

    def matches(self) -> bool:
        if not self._require:
            return True
        typed = self.entry_var.get().strip()
        return typed == self._require or (self._require == ERASE_PHRASE and typed.lower() == ERASE_PHRASE)

    def _sync(self) -> None:
        self.ok.state(["!disabled"] if self.matches() else ["disabled"])

    def _ok(self) -> None:
        if self.matches():
            self.result = True
            self.destroy()


# --- the window --------------------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk, *, confirm: Callable[[str, list[str], str | None], bool] | None = None,
                 runner_factory=None) -> None:
        self.root = root
        self._confirm_override = confirm
        self._runner_factory = runner_factory
        self.queue: queue.Queue = queue.Queue()
        self.pipeline: Pipeline | None = None
        self.drive_text_to_path: dict[str, str] = {}
        self.drivers: list[str] = []
        self.busy = False

        root.title(f"toaster {__version__}")
        screen_h = root.winfo_screenheight()
        root.minsize(820, min(640, screen_h - 80))
        root.geometry(f"940x{min(900, screen_h - 80)}")
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=10)
        style.configure("Heading.TLabel", font=(base.actual("family"), 10, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Danger.TLabel", foreground=DANGER)
        style.configure("Accent.TButton", font=(base.actual("family"), 11, "bold"), padding=(18, 6))

        self._make_vars()
        self._build_menu()
        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)
        self._build_target(outer)
        # The run section is packed before the notebook, anchored to the bottom, so the tabs can never
        # squeeze the progress bar and START button off the window.
        self._build_run(outer)
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True, pady=10)
        self._build_windows_tab()
        self._build_tweaks_tab()
        self._build_advanced_tab()
        self.apply_options(PRESETS["minimal"]())
        self.refresh_drives()
        self.update_states()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_id = root.after(50, self._poll)
        root.bind("<Destroy>", self._on_destroy, add="+")

    # ---- variables ---------------------------------------------------------------------------------------
    def _make_vars(self) -> None:
        self.iso_var = tk.StringVar()
        self.target_kind = tk.StringVar(value="device")
        self.device_var = tk.StringVar()
        self.image_var = tk.StringVar()
        self.image_size_var = tk.StringVar(value="16G")
        self.show_fixed = tk.BooleanVar(value=False)
        self.boot_var = tk.StringVar(value="uefi")
        self.label_var = tk.StringVar(value="WINSTALL")
        self.verify_var = tk.BooleanVar(value=True)
        self.bypass_vars = {k: tk.BooleanVar() for k in BYPASS_CHECKS}
        self.local_account_var = tk.BooleanVar()
        self.username_var = tk.StringVar()
        self.no_data_var = tk.BooleanVar()
        self.bitlocker_var = tk.BooleanVar()
        self.offline_drives_var = tk.BooleanVar()
        self.timezone_var = tk.StringVar()
        self.locale_var = tk.StringVar()
        self.editions_var = tk.StringVar()
        self.tweak_vars = {t.id: tk.BooleanVar() for t in tweaks.TWEAKS}
        self.offline_debloat_var = tk.BooleanVar()
        self.sb2023_var = tk.BooleanVar()
        self.keep_drivers_var = tk.BooleanVar()
        self.silent_var = tk.BooleanVar()
        self.install_edition_var = tk.StringVar()
        self.auto_partition_var = tk.BooleanVar()
        self.workdir_var = tk.StringVar(value=str(default_workdir()))
        self.status_var = tk.StringVar(value="Ready")
        for var in (self.silent_var, self.boot_var, self.target_kind, self.auto_partition_var):
            var.trace_add("write", lambda *a: self.update_states())

    # ---- menu --------------------------------------------------------------------------------------------
    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file = tk.Menu(menu, tearoff=False)
        file.add_command(label="Load profile…", command=self.load_profile)
        file.add_command(label="Save profile…", command=self.save_profile)
        file.add_separator()
        file.add_command(label="Quit", command=self.on_close)
        menu.add_cascade(label="File", menu=file)
        helpm = tk.Menu(menu, tearoff=False)
        helpm.add_command(label="About", command=lambda: messagebox.showinfo(
            "About toaster", f"toaster {__version__}\nWindows 11 install media that respects you.\n\nMIT licensed."))
        menu.add_cascade(label="Help", menu=helpm)
        self.root.config(menu=menu)

    # ---- always-visible top section -------------------------------------------------------------------------
    def _build_target(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="Drive properties", padding=10)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Windows ISO").grid(row=0, column=0, sticky="w")
        self.iso_entry = ttk.Entry(box, textvariable=self.iso_var)
        self.iso_entry.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(box, text="Browse…", command=self.browse_iso).grid(row=0, column=2)
        self.inspect_btn = ttk.Button(box, text="Inspect", command=self.inspect)
        self.inspect_btn.grid(row=0, column=3, padx=(6, 0))
        self.inspect_var = tk.StringVar()
        ttk.Label(box, textvariable=self.inspect_var, style="Muted.TLabel").grid(row=1, column=1, columnspan=3, sticky="w", padx=6)

        kind = ttk.Frame(box)
        kind.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(kind, text="Write to").pack(side="left")
        ttk.Radiobutton(kind, text="USB drive", variable=self.target_kind, value="device").pack(side="left", padx=(8, 0))
        ttk.Radiobutton(kind, text="Disk image file (for a VM)", variable=self.target_kind, value="image").pack(side="left", padx=8)

        self.device_row = ttk.Frame(box)
        self.device_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=4)
        self.device_row.columnconfigure(0, weight=1)
        self.device_combo = ttk.Combobox(self.device_row, textvariable=self.device_var, state="readonly")
        self.device_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(self.device_row, text="Refresh", command=self.refresh_drives).grid(row=0, column=1, padx=(6, 0))
        ttk.Checkbutton(self.device_row, text="Show non-USB disks", variable=self.show_fixed,
                        command=self.refresh_drives).grid(row=0, column=2, padx=(10, 0))

        self.image_row = ttk.Frame(box)
        self.image_row.grid(row=4, column=0, columnspan=4, sticky="ew", pady=4)
        self.image_row.columnconfigure(0, weight=1)
        ttk.Entry(self.image_row, textvariable=self.image_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(self.image_row, text="Choose…", command=self.browse_image).grid(row=0, column=1, padx=6)
        ttk.Label(self.image_row, text="Size").grid(row=0, column=2)
        ttk.Entry(self.image_row, textvariable=self.image_size_var, width=7).grid(row=0, column=3, padx=(4, 0))

        opts = ttk.Frame(box)
        opts.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Boot").pack(side="left")
        ttk.Radiobutton(opts, text="UEFI (GPT)", variable=self.boot_var, value="uefi").pack(side="left", padx=(8, 0))
        ttk.Radiobutton(opts, text="Legacy BIOS + UEFI (MBR)", variable=self.boot_var, value="bios").pack(side="left", padx=8)
        ttk.Label(opts, text="Label").pack(side="left", padx=(16, 4))
        ttk.Entry(opts, textvariable=self.label_var, width=13).pack(side="left")
        ttk.Checkbutton(opts, text="Verify after writing", variable=self.verify_var).pack(side="left", padx=(16, 0))

    def _scroll_tab(self, title: str) -> ttk.Frame:
        """A notebook tab whose content scrolls when the window is too small to show it all."""
        scroll = ScrollFrame(self.tabs)
        self.tabs.add(scroll, text=title)
        inner = ttk.Frame(scroll.body, padding=10)
        inner.pack(fill="both", expand=True)
        return inner

    # ---- Windows tab ---------------------------------------------------------------------------------------------
    def _build_windows_tab(self) -> None:
        tab = self._scroll_tab("Windows")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)

        hw = ttk.LabelFrame(tab, text="Skip hardware requirement checks", padding=8)
        hw.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        labels = {"tpm": "TPM 2.0", "secureboot": "Secure Boot", "ram": "4 GB RAM", "cpu": "Supported CPU",
                  "storage": "64 GB storage", "disk": "Disk size"}
        for i, key in enumerate(BYPASS_CHECKS):
            ttk.Checkbutton(hw, text=labels[key], variable=self.bypass_vars[key]).grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=2)

        acct = ttk.LabelFrame(tab, text="Account, privacy and safety", padding=8)
        acct.grid(row=0, column=1, sticky="nsew")
        acct.columnconfigure(1, weight=1)
        ttk.Checkbutton(acct, text="Skip the Microsoft account requirement", variable=self.local_account_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(acct, text="Create local admin").grid(row=1, column=0, sticky="w")
        ttk.Entry(acct, textvariable=self.username_var).grid(row=1, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(acct, text="blank password; must be changed at first sign-in", style="Muted.TLabel").grid(row=2, column=1, sticky="w", padx=6)
        ttk.Checkbutton(acct, text="Opt out of privacy / data collection pages", variable=self.no_data_var).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(acct, text="Prevent automatic BitLocker device encryption", variable=self.bitlocker_var).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(acct, text="Start with internal drives offline", variable=self.offline_drives_var).grid(row=5, column=0, columnspan=2, sticky="w")

        reg = ttk.LabelFrame(tab, text="Regional settings (optional)", padding=8)
        reg.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 0))
        reg.columnconfigure(1, weight=1)
        ttk.Label(reg, text="Time zone").grid(row=0, column=0, sticky="w")
        ttk.Entry(reg, textvariable=self.timezone_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(reg, text='e.g. "Pacific Standard Time"', style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(reg, text="Language").grid(row=2, column=0, sticky="w")
        ttk.Entry(reg, textvariable=self.locale_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Label(reg, text="e.g. en-GB (blank = keep the ISO's)", style="Muted.TLabel").grid(row=3, column=1, sticky="w", padx=6)

        ed = ttk.LabelFrame(tab, text="Editions", padding=8)
        ed.grid(row=1, column=1, sticky="nsew", pady=(8, 0))
        ed.columnconfigure(0, weight=1)
        self.edition_list = tk.Listbox(ed, selectmode="extended", height=4, exportselection=False)
        self.edition_list.grid(row=0, column=0, sticky="nsew")
        self.edition_list.bind("<<ListboxSelect>>", lambda e: self._editions_from_list())
        ttk.Label(ed, text="Click Inspect to list them. Keep only:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(ed, textvariable=self.editions_var).grid(row=2, column=0, sticky="ew")
        ttk.Label(ed, text="image numbers, comma separated; blank keeps all", style="Muted.TLabel").grid(row=3, column=0, sticky="w")

    # ---- Tweaks tab ---------------------------------------------------------------------------------------------------
    def _build_tweaks_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Tweaks")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="Presets:").pack(side="left")
        for name, text in (("none", "None"), ("minimal", "Minimal"), ("debloat", "Debloat")):
            ttk.Button(bar, text=text, command=lambda n=name: self.apply_preset(n)).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Apply inside install.wim (reaches every new account; slower)",
                        variable=self.offline_debloat_var).pack(side="right")
        scroll = ScrollFrame(tab)
        scroll.pack(fill="both", expand=True, pady=(8, 0))
        groups: dict[str, list[tweaks.Tweak]] = {}
        for t in tweaks.TWEAKS:
            groups.setdefault(t.category, []).append(t)
        for category, members in groups.items():
            ttk.Label(scroll.body, text=category, style="Heading.TLabel").pack(anchor="w", pady=(8, 0))
            for t in members:
                ttk.Checkbutton(scroll.body, text=t.title, variable=self.tweak_vars[t.id]).pack(anchor="w", padx=12)
                if t.edition_note:
                    ttk.Label(scroll.body, text=t.edition_note, style="Muted.TLabel", wraplength=700).pack(anchor="w", padx=36)

    # ---- Advanced tab ------------------------------------------------------------------------------------------------------
    def _build_advanced_tab(self) -> None:
        tab = self._scroll_tab("Advanced")
        tab.columnconfigure(0, weight=1)

        si = ttk.LabelFrame(tab, text="Unattended installation", padding=8)
        si.grid(row=0, column=0, sticky="ew")
        si.columnconfigure(1, weight=1)
        ttk.Checkbutton(si, text="Silent install: skip the language, EULA, edition and account pages",
                        variable=self.silent_var).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(si, text="You still choose the disk. Nothing is erased until you do.", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", padx=22)
        ttk.Label(si, text="Edition to install").grid(row=2, column=0, sticky="w", padx=(22, 6), pady=(4, 0))
        self.install_edition_combo = ttk.Combobox(si, textvariable=self.install_edition_var, width=34)
        self.install_edition_combo.grid(row=2, column=1, sticky="w", pady=(4, 0))
        self.auto_check = ttk.Checkbutton(si, text="Also erase disk 0 of the PC and install there automatically",
                                          variable=self.auto_partition_var)
        self.auto_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.auto_warning = ttk.Label(
            si, style="Danger.TLabel", wraplength=780,
            text="DANGEROUS. Booting this drive will erase disk 0 on that PC without asking further. "
                 "UEFI only. Disconnect disks you care about, and test in a VM first.")
        self.auto_warning.grid(row=4, column=0, columnspan=3, sticky="w", padx=22)

        drv = ttk.LabelFrame(tab, text="Extra drivers (extracted .inf folders)", padding=8)
        drv.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        drv.columnconfigure(0, weight=1)
        self.driver_list = tk.Listbox(drv, height=3, exportselection=False)
        self.driver_list.grid(row=0, column=0, rowspan=2, sticky="ew")
        ttk.Button(drv, text="Add folder…", command=self.add_driver).grid(row=0, column=1, padx=6, sticky="ew")
        ttk.Button(drv, text="Remove", command=self.remove_driver).grid(row=1, column=1, padx=6, sticky="ew")
        ttk.Checkbutton(drv, text="Keep C:\\Drivers after installing", variable=self.keep_drivers_var).grid(row=2, column=0, sticky="w")

        sb = ttk.LabelFrame(tab, text="Boot", padding=8)
        sb.grid(row=2, column=0, sticky="ew", pady=8)
        ttk.Checkbutton(sb, text="Use the 'Windows UEFI CA 2023' signed boot loaders", variable=self.sb2023_var).pack(anchor="w")
        ttk.Label(sb, text="Needs recent media and a PC whose firmware trusts that certificate.", style="Muted.TLabel").pack(anchor="w", padx=22)

        wd = ttk.LabelFrame(tab, text="Work folder (needs about 1.3 x the ISO size free)", padding=8)
        wd.grid(row=3, column=0, sticky="ew", pady=8)
        wd.columnconfigure(0, weight=1)
        ttk.Entry(wd, textvariable=self.workdir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(wd, text="Choose…", command=self.browse_workdir).grid(row=0, column=1, padx=6)

    # ---- progress + log ----------------------------------------------------------------------------------------------------------
    def _build_run(self, parent: ttk.Frame) -> None:
        box = ttk.Frame(parent)
        box.pack(side="bottom", fill="x")
        ttk.Label(box, textvariable=self.status_var, style="Heading.TLabel").pack(anchor="w")
        self.bar = ttk.Progressbar(box, maximum=100, mode="determinate")
        self.bar.pack(fill="x", pady=4)
        logframe = ttk.Frame(box)
        logframe.pack(fill="x")
        self.log_text = tk.Text(logframe, height=7, wrap="word", state="disabled", background="#fafafa")
        sb = ttk.Scrollbar(logframe, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log_text.tag_configure("error", foreground=DANGER)
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(8, 0))
        self.start_btn = ttk.Button(buttons, text="START", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="right")
        self.cancel_btn = ttk.Button(buttons, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=8)

    # ===== options <-> widgets ========================================================================================================
    def collect_options(self) -> Customizations:
        silent = self.silent_var.get()
        return Customizations(
            bypass_checks=[k for k, v in self.bypass_vars.items() if v.get()],
            local_account=self.local_account_var.get(),
            username=self.username_var.get().strip() or None,
            no_data_collection=self.no_data_var.get(),
            disable_bitlocker=self.bitlocker_var.get(),
            offline_internal_drives=self.offline_drives_var.get(),
            timezone=self.timezone_var.get().strip() or None,
            locale=self.locale_var.get().strip() or None,
            tweaks=[i for i, v in self.tweak_vars.items() if v.get()],
            offline_debloat=self.offline_debloat_var.get(),
            editions=_ints(self.editions_var.get(), "editions"),
            secureboot_2023=self.sb2023_var.get(),
            drivers=list(self.drivers),
            keep_drivers=self.keep_drivers_var.get() and bool(self.drivers),
            silent_install=silent,
            auto_partition=silent and self.auto_partition_var.get(),
            install_edition=_leading_int(self.install_edition_var.get(), "edition to install") if silent else None,
        )

    def apply_options(self, o: Customizations) -> None:
        for key, var in self.bypass_vars.items():
            var.set(key in o.bypass_checks)
        self.local_account_var.set(o.local_account)
        self.username_var.set(o.username or "")
        self.no_data_var.set(o.no_data_collection)
        self.bitlocker_var.set(o.disable_bitlocker)
        self.offline_drives_var.set(o.offline_internal_drives)
        self.timezone_var.set(o.timezone or "")
        self.locale_var.set(o.locale or "")
        for tid, var in self.tweak_vars.items():
            var.set(tid in o.tweaks)
        self.offline_debloat_var.set(o.offline_debloat)
        self.editions_var.set(", ".join(map(str, o.editions)))
        self.sb2023_var.set(o.secureboot_2023)
        self.drivers = list(o.drivers)
        self._refresh_driver_list()
        self.keep_drivers_var.set(o.keep_drivers)
        self.silent_var.set(o.silent_install)
        self.auto_partition_var.set(o.auto_partition)
        self.install_edition_var.set("" if o.install_edition is None else str(o.install_edition))
        self.update_states()

    def apply_preset(self, name: str) -> None:
        """Presets set the checks, account/privacy options and tweaks, and leave the rest alone."""
        preset = PRESETS[name]() if name != "none" else Customizations(bypass_checks=[], local_account=False)
        for key, var in self.bypass_vars.items():
            var.set(key in preset.bypass_checks)
        self.local_account_var.set(preset.local_account)
        self.no_data_var.set(preset.no_data_collection)
        self.bitlocker_var.set(preset.disable_bitlocker)
        for tid, var in self.tweak_vars.items():
            var.set(tid in preset.tweaks)

    def update_states(self) -> None:
        silent = self.silent_var.get()
        self.install_edition_combo.configure(state="normal" if silent else "disabled")
        uefi = self.boot_var.get() == "uefi"
        if not (silent and uefi):
            self.auto_partition_var.set(False)
        self.auto_check.state(["!disabled"] if (silent and uefi) else ["disabled"])
        if self.auto_partition_var.get():
            self.auto_warning.grid()
        else:
            self.auto_warning.grid_remove()
        if self.target_kind.get() == "device":
            self.image_row.grid_remove()
            self.device_row.grid()
        else:
            self.device_row.grid_remove()
            self.image_row.grid()

    # ===== actions =======================================================================================================================
    def browse_iso(self) -> None:
        path = filedialog.askopenfilename(title="Choose a Windows ISO", filetypes=[("ISO images", "*.iso"), ("All files", "*")])
        if path:
            self.iso_var.set(path)
            self.inspect()

    def browse_image(self) -> None:
        path = filedialog.asksaveasfilename(title="Disk image file", defaultextension=".img",
                                            filetypes=[("Disk images", "*.img"), ("All files", "*")])
        if path:
            self.image_var.set(path)

    def browse_workdir(self) -> None:
        path = filedialog.askdirectory(title="Work folder")
        if path:
            self.workdir_var.set(path)

    def add_driver(self) -> None:
        path = filedialog.askdirectory(title="Folder containing extracted drivers")
        if path and path not in self.drivers:
            self.drivers.append(path)
            self._refresh_driver_list()

    def remove_driver(self) -> None:
        for i in reversed(self.driver_list.curselection()):
            del self.drivers[i]
        self._refresh_driver_list()

    def _refresh_driver_list(self) -> None:
        self.driver_list.delete(0, "end")
        for d in self.drivers:
            self.driver_list.insert("end", d)

    def refresh_drives(self) -> None:
        try:
            choices = drive_choices(include_fixed=self.show_fixed.get())
        except ToolError as e:
            messagebox.showerror("toaster", str(e))
            return
        self.drive_text_to_path = {c.text: c.path for c in choices if not c.blocked}
        self.device_combo.configure(values=[c.text for c in choices if not c.blocked])
        if self.device_var.get() not in self.drive_text_to_path:
            self.device_var.set(next(iter(self.drive_text_to_path), ""))
        if not choices:
            self.device_var.set("")

    def inspect(self) -> None:
        source = self.iso_var.get().strip()
        if not source:
            return
        self.inspect_var.set("Reading the image…")
        self.inspect_btn.state(["disabled"])

        def work() -> None:
            try:
                self.queue.put(("inspected", media.inspect_windows_media(Path(source).expanduser())))
            except (ToolError, OSError) as e:
                self.queue.put(("inspect_failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _show_inspection(self, info: dict) -> None:
        self.inspect_btn.state(["!disabled"])
        eds = info["editions"]
        self.edition_list.delete(0, "end")
        for e in eds:
            self.edition_list.insert("end", f"{e['index']}  {e['name']}   {e['arch']}  {e['version']}")
        self.install_edition_combo.configure(values=[f"{e['index']}: {e['name']}" for e in eds])
        extra = "  (larger than 4 GiB: will be split for FAT32)" if info["needs_split_for_fat32"] else ""
        self.inspect_var.set(f"{len(eds)} edition(s) in {info['install_image']}{extra}")
        if len(eds) == 1 and not self.install_edition_var.get():
            self.install_edition_var.set(f"{eds[0]['index']}: {eds[0]['name']}")

    def _editions_from_list(self) -> None:
        picked = [int(self.edition_list.get(i).split()[0]) for i in self.edition_list.curselection()]
        self.editions_var.set(", ".join(map(str, picked)))

    def load_profile(self) -> None:
        path = filedialog.askopenfilename(title="Load profile", filetypes=[("JSON", "*.json"), ("All files", "*")])
        if path:
            try:
                self.apply_options(Customizations.load(path))
            except (OptionsError, OSError) as e:
                messagebox.showerror("toaster", str(e))

    def save_profile(self) -> None:
        try:
            opts = self.collect_options()
            opts.validate()
        except (OptionsError, ToolError) as e:
            messagebox.showerror("toaster", str(e))
            return
        path = filedialog.asksaveasfilename(title="Save profile", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            Path(path).write_text(opts.to_json())

    # ===== running =========================================================================================================================
    def make_job(self) -> Job:
        """Turn the form into a Job (raises ToolError with a message fit to show the user)."""
        try:
            opts = self.collect_options()
        except OptionsError as e:
            raise ToolError(str(e)) from None
        is_image = self.target_kind.get() == "image"
        if is_image:
            target = self.image_var.get().strip()
        else:
            target = self.drive_text_to_path.get(self.device_var.get(), "")
            if not target:
                raise ToolError("choose a USB drive (plug it in and press Refresh)")
        return Job(source=self.iso_var.get().strip(), target=target, target_is_image=is_image,
                   image_size=self.image_size_var.get().strip(), boot=self.boot_var.get(),
                   label=self.label_var.get().strip() or "WINSTALL", verify=self.verify_var.get(),
                   allow_fixed=self.show_fixed.get(), workdir=Path(self.workdir_var.get().strip() or default_workdir()),
                   opts=opts)

    def start(self) -> None:
        if self.busy:
            return
        try:
            job = self.make_job()
            job.validate()
        except ToolError as e:
            messagebox.showerror("toaster", str(e))
            return
        self.busy = True
        self.start_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self._clear_log()
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        kwargs = {"runner_factory": self._runner_factory} if self._runner_factory else {}
        self.pipeline = Pipeline(job, _QueueUI(self), **kwargs)

        def work() -> None:
            res = self.pipeline.run()
            self.queue.put(("finished", res))
        threading.Thread(target=work, daemon=True).start()

    def cancel(self) -> None:
        if self.pipeline and not self.pipeline.cancel():
            messagebox.showinfo("toaster", "Writing to the drive can't be cancelled safely; it will finish shortly.")
        else:
            self.status_var.set("Cancelling…")

    def on_close(self) -> None:
        if self.busy:
            if self.pipeline and not self.pipeline.cancel():
                messagebox.showinfo("toaster", "Please wait: the drive is being written.")
                return
        self.root.destroy()

    # ---- queue from the worker thread --------------------------------------------------------------------------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self._poll_id = self.root.after(50, self._poll)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self.root and self._poll_id:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None

    def _handle(self, msg: tuple) -> None:
        kind, *rest = msg
        if kind == "log":
            self._append_log(rest[0])
        elif kind == "stage":
            self.status_var.set(rest[0])
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
        elif kind == "progress":
            phase, done, total = rest
            self.bar.stop()
            self.bar.configure(mode="determinate", value=100 * done / max(total, 1))
            self.status_var.set(f"{ {'copy': 'Copying files', 'verify': 'Verifying', 'sync': 'Flushing to the drive', 'done': 'Finishing'}.get(phase, phase) }"
                                f"  {100 * done // max(total, 1)}%")
        elif kind == "confirm":
            title, lines, require, reply = rest
            reply.put(self._ask(title, lines, require))
        elif kind == "inspected":
            self._show_inspection(rest[0])
        elif kind == "inspect_failed":
            self.inspect_btn.state(["!disabled"])
            self.inspect_var.set("")
            messagebox.showerror("toaster", rest[0])
        elif kind == "finished":
            self._finished(rest[0])

    def _ask(self, title: str, lines: list[str], require: str | None) -> bool:
        if self._confirm_override:
            return self._confirm_override(title, lines, require)
        dlg = ConfirmDialog(self.root, title, lines, require, danger=require == ERASE_PHRASE)
        self.root.wait_window(dlg)
        return dlg.result

    def _finished(self, res: Result) -> None:
        self.busy = False
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100 if res.status == "done" else 0)
        self.start_btn.state(["!disabled"])
        self.cancel_btn.state(["disabled"])
        self.status_var.set({"done": "Finished", "cancelled": "Cancelled", "failed": "Failed"}[res.status])
        if res.status == "failed":
            self._append_log(res.message, "error")
            messagebox.showerror("toaster", res.message)
        elif res.status == "done":
            messagebox.showinfo("toaster", res.message)

    # ---- log -----------------------------------------------------------------------------------------------------------------------------------------
    def _append_log(self, line: str, tag: str | None = None) -> None:
        tag = tag or ("error" if re.search(r"\berror\b|^\s*!", line, re.I) else None)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


class _QueueUI:
    """The Pipeline's view of the window. Called from the worker thread, so everything goes via the queue."""
    def __init__(self, app: App) -> None:
        self.app = app

    def stage(self, name: str) -> None:
        self.app.queue.put(("stage", name))

    def log(self, line: str) -> None:
        self.app.queue.put(("log", line))

    def progress(self, phase: str, done: int, total: int) -> None:
        self.app.queue.put(("progress", phase, done, total))

    def confirm(self, title: str, lines: list[str], require_text: str | None) -> bool:
        reply: queue.Queue = queue.Queue()
        self.app.queue.put(("confirm", title, lines, require_text, reply))
        return reply.get()


def _ints(text: str, what: str) -> list[int]:
    """'2, 3' -> [2, 3]. Anything that is not a number is an error the user can read."""
    out = []
    for part in re.findall(r"[^,\s]+", text):
        if not part.isdigit():
            raise OptionsError(f"{what}: '{part}' is not a number")
        out.append(int(part))
    return sorted(set(out))


def _leading_int(text: str, what: str) -> int | None:
    """'2' or '2: Windows 11 Home' (as the dropdown shows it) -> 2; blank -> None."""
    text = text.strip()
    if not text:
        return None
    m = re.match(r"\d+", text)
    if not m:
        raise OptionsError(f"{what}: choose one from the list or type its number")
    return int(m.group(0))


def main(argv: list[str] | None = None) -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
