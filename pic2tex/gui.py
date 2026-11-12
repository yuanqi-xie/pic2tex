"""Tkinter GUI for Pic2TeX."""

from __future__ import annotations

import base64
import io
import logging
import queue
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import __version__
from .api import DeepSeekError, DeepSeekVisionClient, VisionResult
from .clipboard import ClipboardWatcher, read_clipboard_image
from .config import Config
from .hotkey import HotkeyManager
from .imageio import ImageError, load_image_file
from .latex import apply_wrapper, brace_balance_problems, preview_text
from .models import ImagePayload
from .prompts import build_prompt
from .winutil import (
    copy_text,
    enable_high_dpi,
    get_work_area,
    launch_screen_clip,
    resource_path,
)

log = logging.getLogger(__name__)

APP_TITLE = "Pic2TeX · 图片转 LaTeX"
WELCOME_TEXT = """欢迎使用 Pic2TeX —— 截图 / 图片 → LaTeX

第一次使用：
  1. 点击右上角「⚙ 设置」，填入 DeepSeek API Key
     （也可以设置环境变量 DEEPSEEK_API_KEY）
  2. 点击「▶ 开始监听」
  3. 按 Win+Shift+S 截图，结果会自动出现在这里并复制到剪贴板

也可以点击「上传图片」，或按 Ctrl+V 直接粘贴图片。
"""

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

COLORS = {
    "bg": "#eef2f9",
    "header": "#0f172a",
    "header_soft": "#1e293b",
    "card": "#ffffff",
    "border": "#dbe3ef",
    "text": "#0f172a",
    "muted": "#64748b",
    "accent": "#2563eb",
    "accent_dark": "#1d4ed8",
    "accent_soft": "#dbeafe",
    "green": "#16a34a",
    "green_soft": "#dcfce7",
    "red": "#dc2626",
    "red_soft": "#fee2e2",
    "amber": "#b45309",
    "amber_soft": "#fef3c7",
    "code_bg": "#0f172a",
    "code_fg": "#e2e8f0",
    "code_muted": "#94a3b8",
    "canvas": "#f8fafc",
}

STYLE_CHOICES = [
    ("公式（单个/多个数学表达式）", "math"),
    ("整页文档（生成完整 .tex）", "document"),
]
WRAPPER_CHOICES = [
    ("行间公式  \\[ … \\]", "display"),
    ("行内公式  $ … $", "inline"),
    ("不包裹（纯代码）", "none"),
]
DETAIL_CHOICES = [
    ("原图 original（推荐）", "original"),
    ("高清 high", "high"),
    ("低清 low（更快更省）", "low"),
    ("自动 auto", "auto"),
]
SOURCE_LABELS = {"clipboard": "剪贴板截图", "file": "本地文件", "screenshot": "截图"}

try:  # Pillow is optional: nicer previews for JPEG/WebP and smooth scaling.
    from PIL import Image as _PILImage
    from PIL import ImageTk as _PILImageTk

    _HAS_PIL = True
except Exception:  # pragma: no cover - depends on environment
    _PILImage = None
    _PILImageTk = None
    _HAS_PIL = False


class PreviewUnavailable(Exception):
    """Raised when the current Tk/Tkinter build cannot decode the preview."""


@dataclass
class HistoryEntry:
    when: datetime
    source: str
    image: ImagePayload
    raw_latex: str
    model: str


class Pic2TeXApp:
    def __init__(
        self,
        root: tk.Tk,
        config: Config,
        *,
        demo: bool = False,
        initial_images: Iterable[str] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self.demo = demo
        self.initial_images = list(initial_images or [])

        self.client: DeepSeekVisionClient | None = None
        self.watcher: ClipboardWatcher | None = None
        self.hotkey: HotkeyManager | None = None
        self.monitor_on = False
        self.busy = False
        self.closing = False

        self.queue: queue.Queue = queue.Queue()
        self.current_image: ImagePayload | None = None
        self.raw_latex = ""
        self.current_latex = ""
        self.history: list[HistoryEntry] = []
        self._conversion_token = 0
        self._spin_job: str | None = None
        self._spin_index = 0
        self._spin_start = 0.0
        self._notice_job: str | None = None
        self._preview_job: str | None = None
        self._poll_job: str | None = None
        self._photo: object | None = None
        self._preview_placeholder = True
        self._settings_window: tk.Toplevel | None = None
        self._last_dir = self.config.resolved_save_dir()

        self.ui_font = self._pick_font(
            ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", "TkDefaultFont"]
        )
        self.mono_font = self._pick_font(["Cascadia Mono", "Consolas", "Courier New", "TkFixedFont"])

        self._build_ui()
        self._bind_events()
        self._apply_config_to_controls()
        self._update_api_state()
        self._update_monitor_ui()
        self._update_footer()

        self.root.after(80, self._poll_queue)
        if demo:
            self.root.after(120, self._load_demo)
        elif not self.config.effective_api_key:
            self.root.after(500, lambda: self._open_settings(first_run=True))
        if self.initial_images:
            self.root.after(200, self._load_initial_images)
        if self.config.auto_monitor and not demo:
            self.root.after(700, self.start_monitor)

    # ------------------------------------------------------------------ setup
    def _pick_font(self, candidates: list[str]) -> str:
        try:
            available = set(tkfont.families(self.root))
        except Exception:
            return candidates[-1]
        for name in candidates:
            if name in available:
                return name
        return candidates[-1]

    def _center_window(self, width: int, height: int) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        try:
            scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            scale = 1.0
        area_x, area_y, area_w, area_h = get_work_area() or (0, 0, screen_w, screen_h)
        width = min(int(width * scale), max(900, area_w - 40))
        height = min(int(height * scale), max(600, area_h - 72))
        x = area_x + max(0, (area_w - width) // 2)
        y = area_y + max(0, (area_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self._window_scale = scale

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        *,
        primary: bool = False,
        dark: bool = False,
        danger: bool = False,
        small: bool = False,
    ) -> tk.Button:
        if danger:
            bg, hover, fg = COLORS["red"], "#b91c1c", "#ffffff"
        elif primary:
            bg, hover, fg = COLORS["accent"], COLORS["accent_dark"], "#ffffff"
        elif dark:
            bg, hover, fg = COLORS["header_soft"], "#334155", "#ffffff"
        else:
            bg, hover, fg = "#f1f5f9", "#e2e8f0", COLORS["text"]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            disabledforeground="#94a3b8",
            relief="flat",
            bd=0,
            padx=10 if small else 14,
            pady=5 if small else 8,
            cursor="hand2",
            font=(self.ui_font, 9 if small else 10, "bold" if (primary or dark or danger) else "normal"),
            highlightthickness=0,
            takefocus=False,
        )
        button.bind("<Enter>", lambda _e: button.configure(bg=hover))
        button.bind("<Leave>", lambda _e: button.configure(bg=bg))
        return button

    def _card(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=COLORS["card"],
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"],
            highlightthickness=1,
            bd=0,
        )

    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.configure(bg=COLORS["bg"])
        self._center_window(1180, 800)
        try:
            margin = self._window_scale if hasattr(self, "_window_scale") else 1.0
            self.root.minsize(int(980 * margin), int(620 * margin))
        except Exception:
            self.root.minsize(980, 620)
        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:  # pragma: no cover
            pass
        style.configure("TCombobox", padding=4)
        style.configure("TCheckbutton", background=COLORS["card"], font=(self.ui_font, 9))
        style.configure("TSpinbox", padding=3)

        self._build_header()
        self._build_toolbar()
        self._build_notice()
        self._build_body()
        self._build_statusbar()

        try:
            icon = resource_path("assets/icon.ico")
            if icon.exists():
                self.root.iconbitmap(default=str(icon))
        except Exception:
            pass

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLORS["header"], height=76)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.columnconfigure(1, weight=1)

        logo = tk.Canvas(header, width=40, height=40, bg=COLORS["header"], highlightthickness=0)
        logo.create_rectangle(0, 0, 40, 40, fill=COLORS["accent"], outline="")
        logo.create_text(20, 21, text="{ }", fill="#ffffff", font=(self.mono_font, 15, "bold"))
        logo.grid(row=0, column=0, rowspan=2, padx=(20, 12), pady=18)

        title = tk.Label(
            header, text="Pic2TeX", bg=COLORS["header"], fg="#ffffff",
            font=(self.ui_font, 17, "bold"),
        )
        title.grid(row=0, column=1, sticky="sw", pady=(15, 0))
        subtitle = tk.Label(
            header,
            text="截图 / 图片  →  LaTeX  ·  DeepSeek 视觉识别  ·  本地运行",
            bg=COLORS["header"],
            fg="#94a3b8",
            font=(self.ui_font, 9),
        )
        subtitle.grid(row=1, column=1, sticky="nw", pady=(0, 15))

        right = tk.Frame(header, bg=COLORS["header"])
        right.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, 20))

        state_box = tk.Frame(right, bg=COLORS["header_soft"], padx=10, pady=6)
        state_box.pack(side="left", padx=(0, 12))
        self.state_dot = tk.Canvas(state_box, width=9, height=9, bg=COLORS["header_soft"], highlightthickness=0)
        self.state_dot.pack(side="left", padx=(0, 7))
        self._state_dot_id = self.state_dot.create_oval(0, 0, 9, 9, fill="#64748b", outline="")
        self.state_label = tk.Label(
            state_box, text="监听已停止", bg=COLORS["header_soft"], fg="#cbd5e1",
            font=(self.ui_font, 9),
        )
        self.state_label.pack(side="left")

        self.monitor_btn = self._button(right, "▶ 开始监听", self.toggle_monitor, primary=True)
        self.monitor_btn.pack(side="left", padx=(0, 8))
        self.settings_btn = self._button(right, "⚙ 设置", self._open_settings, dark=True)
        self.settings_btn.pack(side="left")

    def _build_toolbar(self) -> None:
        outer = tk.Frame(self.root, bg=COLORS["bg"])
        outer.grid(row=1, column=0, sticky="ew", padx=16, pady=(14, 0))

        bar = tk.Frame(outer, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1)
        bar.pack(fill="x")

        left = tk.Frame(bar, bg=COLORS["card"])
        left.pack(side="left", padx=12, pady=10)
        self.snip_btn = self._button(left, "📷 截图", self.trigger_screen_clip, primary=True)
        self.snip_btn.pack(side="left", padx=(0, 8))
        self.upload_btn = self._button(left, "📂 上传图片", self.upload_image)
        self.upload_btn.pack(side="left", padx=(0, 8))
        self.paste_btn = self._button(left, "📋 读取剪贴板", self.read_clipboard_now)
        self.paste_btn.pack(side="left")

        right = tk.Frame(bar, bg=COLORS["card"])
        right.pack(side="right", padx=12, pady=10)

        self.style_var = tk.StringVar()
        self.style_combo = self._labeled_combo(
            right, "排版风格", self.style_var, [label for label, _ in STYLE_CHOICES], width=24,
            command=self._on_style_changed,
        )
        self.style_combo.pack(side="left", padx=(0, 12))

        self.wrapper_var = tk.StringVar()
        self.wrapper_combo = self._labeled_combo(
            right, "公式包裹", self.wrapper_var, [label for label, _ in WRAPPER_CHOICES], width=18,
            command=self._on_wrapper_changed,
        )
        self.wrapper_combo.pack(side="left")

    def _labeled_combo(
        self,
        parent: tk.Misc,
        label: str,
        variable: tk.StringVar,
        values: list[str],
        *,
        width: int = 20,
        command=None,
    ) -> ttk.Combobox:
        box = tk.Frame(parent, bg=COLORS["card"])
        box.pack(side="left", padx=(0, 12))
        tk.Label(
            box, text=label, bg=COLORS["card"], fg=COLORS["muted"],
            font=(self.ui_font, 8),
        ).pack(anchor="w")
        combo = ttk.Combobox(
            box, textvariable=variable, values=values, width=width,
            state="readonly", font=(self.ui_font, 9),
        )
        if command is not None:
            combo.bind("<<ComboboxSelected>>", command)
        combo.pack()
        return combo

    def _build_notice(self) -> None:
        self.notice = tk.Label(
            self.root, text="", bg=COLORS["accent_soft"], fg=COLORS["accent_dark"],
            font=(self.ui_font, 9), anchor="w", padx=12, pady=7,
        )

    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=COLORS["bg"])
        body.grid(row=3, column=0, sticky="nsew", padx=16, pady=(12, 8))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.paned = tk.PanedWindow(
            body, orient="horizontal", sashwidth=8, bd=0,
            bg=COLORS["bg"], sashrelief="flat", showhandle=False,
        )
        self.paned.grid(row=0, column=0, sticky="nsew")

        left = self._card(self.paned)
        right = self._card(self.paned)
        self.paned.add(left, minsize=360, stretch="always")
        self.paned.add(right, minsize=470, stretch="always")

        self._build_preview(left)
        self._build_output(right)
        self.root.after(120, self._position_sash)

    def _position_sash(self) -> None:
        try:
            width = self.paned.winfo_width()
            if width > 100:
                self.paned.sash_place(0, int(width * 0.44), 0)
        except tk.TclError:  # pragma: no cover
            pass

    def _build_preview(self, card: tk.Frame) -> None:
        head = tk.Frame(card, bg=COLORS["card"])
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(
            head, text="图片预览", bg=COLORS["card"], fg=COLORS["text"],
            font=(self.ui_font, 11, "bold"),
        ).pack(side="left")
        self.image_info = tk.Label(
            head, text="尚未载入图片", bg=COLORS["card"], fg=COLORS["muted"],
            font=(self.ui_font, 8),
        )
        self.image_info.pack(side="right")

        self.canvas = tk.Canvas(
            card, bg=COLORS["canvas"], highlightthickness=0, bd=0, width=360, height=240
        )
        self.canvas.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", lambda _e: self.upload_image())

        hist_head = tk.Frame(card, bg=COLORS["card"])
        hist_head.pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(
            hist_head, text="最近记录", bg=COLORS["card"], fg=COLORS["text"],
            font=(self.ui_font, 9, "bold"),
        ).pack(side="left")
        self.history_hint = tk.Label(
            hist_head, text="点击可回看", bg=COLORS["card"], fg=COLORS["muted"],
            font=(self.ui_font, 8),
        )
        self.history_hint.pack(side="right")

        list_frame = tk.Frame(card, bg=COLORS["card"])
        list_frame.pack(fill="x", padx=16, pady=(0, 14))
        self.history_list = tk.Listbox(
            list_frame,
            height=5,
            width=24,
            activestyle="none",
            bg=COLORS["canvas"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent_soft"],
            selectforeground=COLORS["text"],
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            font=(self.ui_font, 8),
            exportselection=False,
        )
        self.history_list.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.history_list.yview)
        scroll.pack(side="right", fill="y")
        self.history_list.configure(yscrollcommand=scroll.set)
        self.history_list.bind("<<ListboxSelect>>", self._on_history_selected)

    def _build_output(self, card: tk.Frame) -> None:
        head = tk.Frame(card, bg=COLORS["card"])
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(
            head, text="LaTeX 代码", bg=COLORS["card"], fg=COLORS["text"],
            font=(self.ui_font, 11, "bold"),
        ).pack(side="left")
        tk.Label(
            head, text="可直接编辑 · 自动复制", bg=COLORS["card"], fg=COLORS["muted"],
            font=(self.ui_font, 8),
        ).pack(side="left", padx=(10, 0))
        self.output_meta = tk.Label(
            head, text="等待识别", bg=COLORS["card"], fg=COLORS["muted"],
            font=(self.ui_font, 8),
        )
        self.output_meta.pack(side="right")

        text_frame = tk.Frame(card, bg=COLORS["code_bg"])
        text_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        self.output = tk.Text(
            text_frame,
            bg=COLORS["code_bg"],
            fg=COLORS["code_fg"],
            insertbackground="#ffffff",
            selectbackground=COLORS["accent_dark"],
            selectforeground="#ffffff",
            bd=0,
            highlightthickness=0,
            wrap="none",
            width=36,
            height=14,
            font=(self.mono_font, 11),
            padx=14,
            pady=12,
            undo=True,
            maxundo=50,
            spacing1=2,
            spacing3=2,
        )
        self.output.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.output.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.output.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.output.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self._set_output(WELCOME_TEXT)

        footer = tk.Frame(card, bg=COLORS["card"])
        footer.pack(fill="x", padx=16, pady=(0, 14))
        self.copy_btn = self._button(footer, "📋 复制 LaTeX", self.copy_latex, primary=True)
        self.copy_btn.pack(side="left", padx=(0, 8))
        self.save_btn = self._button(footer, "💾 保存 .tex", self.save_latex)
        self.save_btn.pack(side="left", padx=(0, 8))
        self.reconvert_btn = self._button(footer, "🔄 重新转换", self.reconvert)
        self.reconvert_btn.pack(side="right", padx=(8, 0))
        self.clear_btn = self._button(footer, "清空", self.clear_all)
        self.clear_btn.pack(side="right")

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=COLORS["bg"])
        bar.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 10))
        bar.columnconfigure(0, weight=1)
        self.status_label = tk.Label(
            bar, text="就绪", bg=COLORS["bg"], fg=COLORS["muted"],
            font=(self.ui_font, 9), anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w")
        self.footer_info = tk.Label(
            bar, text="", bg=COLORS["bg"], fg=COLORS["muted"],
            font=(self.ui_font, 8), anchor="e",
        )
        self.footer_info.grid(row=0, column=1, sticky="e")

    # --------------------------------------------------------------- bindings
    def _bind_events(self) -> None:
        self.root.bind("<Control-v>", self._on_ctrl_v)
        self.root.bind("<Control-V>", self._on_ctrl_v)
        self.root.bind("<Control-o>", lambda _e: self.upload_image())
        self.root.bind("<Control-s>", lambda _e: self.save_latex())
        self.root.bind("<Control-Return>", lambda _e: self.reconvert())
        self.root.bind("<F5>", lambda _e: self.reconvert())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.report_callback_exception = self._report_callback_exception

    def _on_ctrl_v(self, _event=None):
        widget = self.root.focus_get()
        if isinstance(widget, (tk.Entry, tk.Text)):
            try:
                if str(widget.cget("state")) != "disabled":
                    return None  # let the widget paste text normally
            except tk.TclError:
                pass
        self.read_clipboard_now()
        return "break"

    # ------------------------------------------------------------- config sync
    def _apply_config_to_controls(self) -> None:
        self.style_var.set(self._label_for(STYLE_CHOICES, self.config.prompt_style))
        self.wrapper_var.set(self._label_for(WRAPPER_CHOICES, self.config.wrapper))
        self._update_footer()

    @staticmethod
    def _label_for(choices: list[tuple[str, str]], value: str) -> str:
        for label, candidate in choices:
            if candidate == value:
                return label
        return choices[0][0]

    @staticmethod
    def _value_for(choices: list[tuple[str, str]], label: str) -> str:
        for candidate, value in choices:
            if candidate == label:
                return value
        return choices[0][1]

    def _update_footer(self) -> None:
        self.footer_info.configure(
            text=f"模型 {self.config.effective_model}  ·  快捷键 {self.config.hotkey}  ·  v{__version__}"
        )

    def _update_api_state(self) -> None:
        self.client = None
        if not self.config.effective_api_key:
            return
        try:
            self.client = DeepSeekVisionClient(
                self.config.effective_api_key,
                base_url=self.config.effective_base_url,
                model=self.config.effective_model,
                timeout=float(self.config.timeout),
                max_retries=2,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("无法初始化 API 客户端: %s", exc)
            self.client = None

    # ------------------------------------------------------------------ notice
    def _notice(self, text: str, level: str = "info", timeout: float | None = None) -> None:
        palette = {
            "info": (COLORS["accent_soft"], COLORS["accent_dark"]),
            "success": (COLORS["green_soft"], COLORS["green"]),
            "warn": (COLORS["amber_soft"], COLORS["amber"]),
            "error": (COLORS["red_soft"], COLORS["red"]),
        }
        bg, fg = palette.get(level, palette["info"])
        if self._notice_job:
            try:
                self.root.after_cancel(self._notice_job)
            except Exception:
                pass
            self._notice_job = None
        self.notice.configure(text=text, bg=bg, fg=fg)
        self.notice.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 0))
        if timeout is None and level in ("info", "success"):
            timeout = 8.0
        if timeout:
            self._notice_job = self.root.after(int(timeout * 1000), self._hide_notice)

    def _hide_notice(self) -> None:
        self._notice_job = None
        self.notice.grid_remove()

    def _set_status(self, text: str, level: str = "info") -> None:
        colors = {
            "info": COLORS["muted"],
            "success": COLORS["green"],
            "warn": COLORS["amber"],
            "error": COLORS["red"],
        }
        self.status_label.configure(text=text, fg=colors.get(level, COLORS["muted"]))

    # ------------------------------------------------------------------ monitor
    def toggle_monitor(self) -> None:
        if self.monitor_on:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self) -> None:
        if self.monitor_on:
            return
        try:
            self.watcher = ClipboardWatcher(
                self._on_clipboard_image,
                self._on_clipboard_error,
                poll_interval=0.35,
                start_with_current=bool(self.config.start_with_current),
            )
            self.watcher.start()
        except Exception as exc:
            self.watcher = None
            log.exception("启动剪贴板监听失败")
            self._notice(f"启动监听失败：{exc}", "error")
            self._set_status("监听启动失败", "error")
            return
        self.monitor_on = True
        self._start_hotkey()
        self._update_monitor_ui()
        self._set_status("监听已开启：按 Win+Shift+S 截图，或点击「📷 截图」", "success")
        if not self.config.effective_api_key:
            self._notice("监听已开启，但还没有配置 DeepSeek API Key；识别前请先在「⚙ 设置」中填写。", "warn")

    def stop_monitor(self) -> None:
        if self.watcher is not None:
            try:
                self.watcher.stop()
            except Exception:
                log.exception("停止剪贴板监听失败")
            self.watcher = None
        self._stop_hotkey()
        self.monitor_on = False
        self._update_monitor_ui()
        self._set_status("监听已停止", "info")

    def _update_monitor_ui(self) -> None:
        if self.monitor_on:
            self.monitor_btn.configure(text="■ 停止监听", bg=COLORS["red"], activebackground="#b91c1c")
            self.state_dot.itemconfigure(self._state_dot_id, fill=COLORS["green"])
            self.state_label.configure(text="监听中")
        else:
            self.monitor_btn.configure(text="▶ 开始监听", bg=COLORS["accent"], activebackground=COLORS["accent_dark"])
            self.state_dot.itemconfigure(self._state_dot_id, fill="#64748b")
            self.state_label.configure(text="监听已停止")

    def _start_hotkey(self) -> None:
        if not self.config.hotkey_enabled:
            return
        self._stop_hotkey()
        self.hotkey = HotkeyManager(
            callback=lambda: self.queue.put(("hotkey",)), hotkey=self.config.hotkey
        )
        if not self.hotkey.start():
            error = self.hotkey.last_error or "未知原因"
            self._notice(error, "warn", timeout=10)
            self.hotkey = None

    def _stop_hotkey(self) -> None:
        if self.hotkey is not None:
            try:
                self.hotkey.stop()
            except Exception:
                log.exception("停止全局快捷键失败")
            self.hotkey = None

    # ------------------------------------------------------------- clipboard
    def _on_clipboard_image(self, payload: ImagePayload) -> None:
        self.queue.put(("clipboard_image", payload))

    def _on_clipboard_error(self, exc: Exception) -> None:
        self.queue.put(("clipboard_error", str(exc)))

    def read_clipboard_now(self) -> None:
        try:
            payload = read_clipboard_image()
        except Exception as exc:
            log.exception("读取剪贴板失败")
            self._notice(f"读取剪贴板失败：{exc}", "error")
            return
        if payload is None:
            self._notice("剪贴板中没有图片。请先截图（Win+Shift+S）或复制一张图片。", "warn")
            self._set_status("剪贴板中没有图片", "warn")
            return
        self._load_image(payload, auto_convert=True, label="剪贴板")

    def trigger_screen_clip(self) -> None:
        if not self.monitor_on:
            self.start_monitor()
        if launch_screen_clip():
            self._set_status("已打开系统截图工具：框选区域后会自动识别", "info")
        else:
            self._notice("无法自动打开截图工具，请手动按 Win+Shift+S。", "warn")

    # ------------------------------------------------------------------ images
    def upload_image(self) -> None:
        path = filedialog.askopenfilename(
            title="选择图片",
            initialdir=str(self._last_dir),
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.gif *.webp *.bmp"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        try:
            payload = load_image_file(path, source="file")
        except ImageError as exc:
            self._notice(f"无法读取图片：{exc}", "error")
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("读取图片失败")
            self._notice(f"无法读取图片：{exc}", "error")
            return
        self._last_dir = Path(path).parent
        self._load_image(payload, auto_convert=True, label=Path(path).name)

    def _load_initial_images(self) -> None:
        for raw in self.initial_images:
            try:
                payload = load_image_file(raw, source="file")
            except Exception as exc:
                self._notice(f"无法读取 {raw}：{exc}", "error")
                continue
            self._load_image(payload, auto_convert=True, label=Path(raw).name)
            break

    def _load_image(self, payload: ImagePayload, *, auto_convert: bool, label: str = "") -> None:
        self.current_image = payload
        source = SOURCE_LABELS.get(payload.source, payload.source)
        name = f" · {payload.name}" if payload.name else ""
        self.image_info.configure(
            text=f"{source}{name} · {payload.width}×{payload.height} · {payload.size_human}"
        )
        self._render_preview()
        if auto_convert:
            self._start_conversion(payload)

    def _render_preview(self) -> None:
        self._preview_job = None
        payload = self.current_image
        self.canvas.delete("all")
        if payload is None:
            self._preview_placeholder = True
            self._show_placeholder(
                "截图或上传图片后显示预览\n\n"
                "Win+Shift+S 截图 → 自动识别\n"
                "也可以点击此处上传图片"
            )
            return
        width = max(120, self.canvas.winfo_width())
        height = max(120, self.canvas.winfo_height())
        try:
            photo = self._make_photo(payload, width - 24, height - 24)
        except PreviewUnavailable:
            self._preview_placeholder = True
            self._show_placeholder(
                "该格式暂不支持内置预览\n"
                f"（{payload.mime}）\n\n图片仍可正常识别与转换"
            )
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("生成预览失败")
            self._preview_placeholder = True
            self._show_placeholder(f"预览失败：{exc}")
            return
        self._photo = photo
        self._preview_placeholder = False
        self.canvas.create_image(width // 2, height // 2, image=photo, anchor="center")

    def _show_placeholder(self, text: str) -> None:
        width = max(120, self.canvas.winfo_width())
        height = max(120, self.canvas.winfo_height())
        self.canvas.create_text(
            width // 2,
            height // 2,
            text=text,
            fill=COLORS["muted"],
            font=(self.ui_font, 9),
            justify="center",
            width=max(200, width - 40),
        )

    def _make_photo(self, payload: ImagePayload, max_w: int, max_h: int):
        max_w, max_h = max(80, max_w), max(80, max_h)
        if _HAS_PIL:
            image = _PILImage.open(io.BytesIO(payload.data))
            image = image.convert("RGBA")
            image.thumbnail((max_w, max_h), _PILImage.LANCZOS)
            return _PILImageTk.PhotoImage(image)
        if payload.mime not in ("image/png", "image/gif"):
            raise PreviewUnavailable(payload.mime)
        photo = tk.PhotoImage(data=base64.b64encode(payload.data).decode("ascii"))
        factor = max(1, -(-photo.width() // max_w), -(-photo.height() // max_h))
        if factor > 1:
            photo = photo.subsample(factor, factor)
        return photo

    def _on_canvas_resize(self, _event) -> None:
        if self._preview_job:
            try:
                self.root.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.root.after(120, self._render_preview)

    # ------------------------------------------------------------------- queue
    def _poll_queue(self) -> None:
        if self.closing:
            return
        try:
            while True:
                event = self.queue.get_nowait()
                try:
                    self._handle_event(event)
                except Exception:  # pragma: no cover - defensive
                    log.exception("处理界面事件失败: %r", event)
        except queue.Empty:
            pass
        if not self.closing:
            try:
                self._poll_job = self.root.after(80, self._poll_queue)
            except tk.TclError:  # pragma: no cover - window already destroyed
                self._poll_job = None

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "clipboard_image":
            payload: ImagePayload = event[1]
            if self.busy:
                self._set_status("正在识别上一张图片，本次截图已忽略", "warn")
                return
            self._load_image(payload, auto_convert=True, label="剪贴板")
        elif kind == "clipboard_error":
            self._set_status(f"读取剪贴板出错：{event[1]}", "error")
        elif kind == "hotkey":
            self.trigger_screen_clip()
        elif kind == "api_started":
            if event[1] == self._conversion_token:
                self._set_busy(True)
                self._start_spinner()
        elif kind == "api_delta":
            if event[1] == self._conversion_token and not self.closing:
                self.output.insert("end", event[2])
                self.output.see("end")
        elif kind == "api_done":
            if event[1] != self._conversion_token:
                return
            self._finish_conversion(event[2], event[3])
        elif kind == "api_error":
            if event[1] != self._conversion_token:
                return
            self._fail_conversion(event[2], event[3])
        elif kind == "check_result":
            _kind, ok, message, label, button = event
            try:
                if label.winfo_exists():
                    label.configure(
                        text=message, fg=COLORS["green"] if ok else COLORS["red"]
                    )
                if button.winfo_exists():
                    button.configure(state="normal")
            except tk.TclError:  # settings window already closed
                pass

    # -------------------------------------------------------------- conversion
    def _start_conversion(self, payload: ImagePayload) -> None:
        if self.busy:
            self._notice("正在识别中，请稍候…", "warn")
            return
        if self.demo:
            self._notice("演示模式不会调用 API。请在正式启动后使用。", "info")
            return
        if self.client is None:
            self._update_api_state()
        if self.client is None:
            self._notice("还没有配置 DeepSeek API Key。点击右上角「⚙ 设置」填写后即可识别。", "warn", timeout=10)
            self._set_status("缺少 API Key", "warn")
            self.root.after(200, lambda: self._open_settings(first_run=not self.config.effective_api_key))
            return

        self._conversion_token += 1
        token = self._conversion_token
        client = self.client
        prompt = build_prompt(self.config.prompt_style)
        max_tokens = int(self.config.max_tokens)
        temperature = float(self.config.temperature)
        detail = self.config.detail
        use_stream = bool(self.config.stream)
        self._set_busy(True)
        self._start_spinner()
        self.output_meta.configure(text="正在识别…")
        self._notice("正在调用 DeepSeek 视觉模型识别图片…", "info", timeout=15)
        thread = threading.Thread(
            target=self._worker,
            args=(token, client, payload, prompt, max_tokens, temperature, detail, use_stream),
            name="pic2tex-api",
            daemon=True,
        )
        thread.start()

    def _worker(
        self,
        token: int,
        client: DeepSeekVisionClient,
        payload: ImagePayload,
        prompt: str,
        max_tokens: int,
        temperature: float,
        detail: str,
        use_stream: bool,
    ) -> None:
        started = time.monotonic()
        self.queue.put(("api_started", token))
        try:
            result: VisionResult
            if use_stream:
                try:
                    result = client.convert(
                        payload,
                        prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        detail=detail,
                        stream=True,
                        on_delta=lambda chunk: self.queue.put(("api_delta", token, chunk)),
                    )
                except DeepSeekError as exc:
                    if exc.kind != "invalid_response":
                        raise
                    # Some OpenAI-compatible gateways answer non-SSE even when
                    # streaming was requested; retry once without streaming.
                    result = client.convert(
                        payload, prompt, max_tokens=max_tokens,
                        temperature=temperature, detail=detail, stream=False,
                    )
            else:
                result = client.convert(
                    payload, prompt, max_tokens=max_tokens,
                    temperature=temperature, detail=detail, stream=False,
                )
            self.queue.put(("api_done", token, result, time.monotonic() - started))
        except DeepSeekError as exc:
            self.queue.put(("api_error", token, str(exc), exc.kind))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("识别过程发生未预期错误")
            self.queue.put(("api_error", token, f"发生未预期的错误：{exc}", "unknown"))

    def _finish_conversion(self, result: VisionResult, elapsed: float) -> None:
        self._set_busy(False)
        self._stop_spinner()
        raw = result.text or ""
        self.raw_latex = raw
        final = apply_wrapper(raw, self.config.wrapper)
        self.current_latex = final
        self._set_output(final)

        usage = result.usage or {}
        total_tokens = usage.get("total_tokens") or usage.get("totalTokens")
        meta = f"{result.model or self.config.effective_model} · {elapsed:.1f}s"
        if total_tokens:
            meta += f" · {total_tokens} tokens"
        self.output_meta.configure(text=meta)

        problems = brace_balance_problems(raw)
        if problems:
            self._notice("已生成，但检测到可能的语法问题：" + "；".join(problems[:3]), "warn")
            self._set_status("识别完成（有警告）", "warn")
        else:
            self._notice("识别完成，LaTeX 已生成。", "success")
            self._set_status(f"识别完成，用时 {elapsed:.1f} 秒", "success")

        if self.config.auto_copy and final:
            if self._copy_to_clipboard(final):
                self._set_status(f"识别完成，已自动复制到剪贴板（{elapsed:.1f}s）", "success")
            else:
                self._notice("LaTeX 已生成，但自动复制到剪贴板失败。", "warn")

        if self.current_image is not None:
            entry = HistoryEntry(
                when=datetime.now(),
                source=SOURCE_LABELS.get(self.current_image.source, self.current_image.source),
                image=self.current_image,
                raw_latex=raw,
                model=result.model or self.config.effective_model,
            )
            self._add_history(entry)

    def _fail_conversion(self, message: str, kind: str) -> None:
        self._set_busy(False)
        self._stop_spinner()
        self.output_meta.configure(text="识别失败")
        self._notice(message, "error")
        self._set_status(f"识别失败（{kind}）", "error")
        log.warning("识别失败 [%s]: %s", kind, message)
        if kind in ("auth", "quota", "config"):
            self._set_status(message, "error")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.snip_btn,
            self.upload_btn,
            self.paste_btn,
            self.reconvert_btn,
            self.save_btn,
        ):
            try:
                button.configure(state=state)
            except tk.TclError:
                pass
        if busy:
            self.copy_btn.configure(state="disabled")
        else:
            self.copy_btn.configure(state="normal")

    # ----------------------------------------------------------------- spinner
    def _start_spinner(self) -> None:
        self._spin_start = time.monotonic()
        self._spin_index = 0
        self._spin_tick()

    def _spin_tick(self) -> None:
        if not self.busy or self.closing:
            return
        elapsed = time.monotonic() - self._spin_start
        frame = SPINNER[self._spin_index % len(SPINNER)]
        self._spin_index += 1
        self._set_status(f"{frame} 正在识别… {elapsed:.1f}s", "info")
        self._spin_job = self.root.after(120, self._spin_tick)

    def _stop_spinner(self) -> None:
        if self._spin_job:
            try:
                self.root.after_cancel(self._spin_job)
            except Exception:
                pass
            self._spin_job = None

    # ------------------------------------------------------------------- output
    def _set_output(self, text: str) -> None:
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)

    def _get_output(self) -> str:
        return self.output.get("1.0", "end-1c").strip()

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            if copy_text(text):
                return True
        except Exception:
            log.exception("原生剪贴板写入失败")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            return True
        except Exception:
            log.exception("Tk 剪贴板写入失败")
            return False

    def copy_latex(self) -> None:
        text = self._get_output()
        if not text:
            self._notice("还没有可复制的 LaTeX 代码。", "warn")
            return
        if self._copy_to_clipboard(text):
            self._set_status("LaTeX 已复制到剪贴板", "success")
            self._notice("已复制到剪贴板。", "success", timeout=3)
        else:
            self._notice("复制失败，请手动选择代码后按 Ctrl+C。", "error")

    def save_latex(self) -> None:
        text = self._get_output()
        if not text:
            self._notice("还没有可保存的 LaTeX 代码。", "warn")
            return
        directory = self.config.resolved_save_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            directory = Path.home()
        path = filedialog.asksaveasfilename(
            title="保存 LaTeX 文件",
            initialdir=str(directory),
            initialfile=f"pic2tex_{datetime.now():%Y%m%d_%H%M%S}.tex",
            defaultextension=".tex",
            filetypes=[("LaTeX 文件", "*.tex"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            self._notice(f"保存失败：{exc}", "error")
            return
        self.config.save_dir = str(Path(path).parent)
        try:
            self.config.save()
        except OSError:
            log.exception("保存配置失败")
        self._notice(f"已保存到 {path}", "success")
        self._set_status("文件已保存", "success")

    def reconvert(self) -> None:
        if self.busy:
            self._notice("正在识别中，请稍候…", "warn")
            return
        if self.current_image is None:
            self._notice("请先截图或上传一张图片。", "warn")
            return
        self._start_conversion(self.current_image)

    def clear_all(self) -> None:
        self.current_image = None
        self.raw_latex = ""
        self.current_latex = ""
        self.image_info.configure(text="尚未载入图片")
        self.output_meta.configure(text="等待识别")
        self._set_output(WELCOME_TEXT)
        self._render_preview()
        self._set_status("已清空", "info")

    # ------------------------------------------------------------------ history
    def _add_history(self, entry: HistoryEntry) -> None:
        self.history.insert(0, entry)
        limit = max(1, int(self.config.history_limit))
        self.history = self.history[:limit]
        label = (
            f"{entry.when:%H:%M:%S}  [{entry.source}]  "
            f"{preview_text(entry.raw_latex, 32)}"
        )
        self.history_list.insert(0, label)
        while self.history_list.size() > len(self.history):
            self.history_list.delete("end")
        self.history_hint.configure(text=f"共 {len(self.history)} 条 · 点击可回看")

    def _on_history_selected(self, _event=None) -> None:
        selection = self.history_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if not 0 <= index < len(self.history):
            return
        entry = self.history[index]
        self.current_image = entry.image
        self.raw_latex = entry.raw_latex
        self.current_latex = apply_wrapper(entry.raw_latex, self.config.wrapper)
        self._set_output(self.current_latex)
        self.output_meta.configure(text=f"{entry.model} · {entry.when:%H:%M:%S}")
        source = SOURCE_LABELS.get(entry.image.source, entry.image.source)
        self.image_info.configure(
            text=f"{source} · {entry.image.width}×{entry.image.height} · {entry.image.size_human}"
        )
        self._render_preview()
        self._set_status("已载入历史记录", "info")

    # ---------------------------------------------------------------- combobox
    def _on_style_changed(self, _event=None) -> None:
        value = self._value_for(STYLE_CHOICES, self.style_var.get())
        if value == self.config.prompt_style:
            return
        self.config.prompt_style = value
        self._persist_config()
        if self.current_image is not None and not self.demo and not self.busy:
            self._notice("排版风格已切换，正在重新识别…", "info", timeout=4)
            self._start_conversion(self.current_image)

    def _on_wrapper_changed(self, _event=None) -> None:
        value = self._value_for(WRAPPER_CHOICES, self.wrapper_var.get())
        if value == self.config.wrapper:
            return
        self.config.wrapper = value
        self._persist_config()
        if self.raw_latex:
            self.current_latex = apply_wrapper(self.raw_latex, self.config.wrapper)
            self._set_output(self.current_latex)
            self._set_status("已更新包裹方式", "info")

    def _persist_config(self) -> None:
        try:
            self.config.save()
        except OSError:
            log.exception("保存配置失败")

    # ----------------------------------------------------------------- settings
    def _open_settings(self, first_run: bool = False) -> None:
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.lift()
            self._settings_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self._settings_window = window
        window.title("Pic2TeX 设置")
        window.configure(bg=COLORS["card"])
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()
        self._center_child(window, 780, 690)

        body = tk.Frame(window, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=22, pady=(18, 8))
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=1)

        row = 0
        tk.Label(
            body, text="设置", bg=COLORS["card"], fg=COLORS["text"],
            font=(self.ui_font, 15, "bold"),
        ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 2))
        row += 1
        tk.Label(
            body,
            text="API Key 只保存在本机配置文件中；也可以用环境变量 DEEPSEEK_API_KEY 提供（优先生效）。",
            bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8),
            anchor="w", justify="left", wraplength=700,
        ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 12))
        row += 1

        self._section(body, "DeepSeek 接口", row)
        row += 1

        key_var = tk.StringVar(value=self.config.effective_api_key)
        tk.Label(body, text="API Key", bg=COLORS["card"], fg=COLORS["text"], font=(self.ui_font, 9)).grid(
            row=row, column=0, sticky="w", pady=4
        )
        key_entry = ttk.Entry(body, textvariable=key_var, show="•", font=(self.mono_font, 10))
        key_entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 8), pady=4)
        show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            body, text="显示", variable=show_var, bg=COLORS["card"],
            activebackground=COLORS["card"], font=(self.ui_font, 8),
            command=lambda: key_entry.configure(show="" if show_var.get() else "•"),
        ).grid(row=row, column=3, sticky="w", pady=4)
        row += 1

        link = tk.Label(
            body, text="获取 API Key：https://platform.deepseek.com/api_keys",
            bg=COLORS["card"], fg=COLORS["accent"], cursor="hand2", font=(self.ui_font, 8),
        )
        link.grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 8))
        link.bind("<Button-1>", lambda _e: webbrowser.open("https://platform.deepseek.com/api_keys"))
        row += 1

        url_var = tk.StringVar(value=self.config.effective_base_url)
        model_var = tk.StringVar(value=self.config.effective_model)
        row = self._field_row(body, row, "API 地址", url_var, hint="默认 https://api.deepseek.com")
        row = self._field_row(body, row, "模型", model_var, hint="视觉模型 deepseek-flash")

        extra = tk.Frame(body, bg=COLORS["card"])
        extra.grid(row=row, column=0, columnspan=4, sticky="ew", pady=4)
        row += 1
        timeout_var = tk.StringVar(value=str(self.config.timeout))
        tokens_var = tk.StringVar(value=str(self.config.max_tokens))
        temp_var = tk.StringVar(value=str(self.config.temperature))
        for label, var, width in (
            ("超时（秒）", timeout_var, 8),
            ("最大输出 token", tokens_var, 10),
            ("temperature", temp_var, 8),
        ):
            box = tk.Frame(extra, bg=COLORS["card"])
            box.pack(side="left", padx=(0, 18))
            tk.Label(box, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8)).pack(anchor="w")
            ttk.Entry(box, textvariable=var, width=width, font=(self.mono_font, 9)).pack(anchor="w")

        detail_var = tk.StringVar(value=self._label_for(DETAIL_CHOICES, self.config.detail))
        detail_box = tk.Frame(extra, bg=COLORS["card"])
        detail_box.pack(side="left")
        tk.Label(detail_box, text="图片精度", bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8)).pack(anchor="w")
        ttk.Combobox(
            detail_box, textvariable=detail_var, values=[label for label, _ in DETAIL_CHOICES],
            width=16, state="readonly", font=(self.ui_font, 9),
        ).pack(anchor="w")

        self._section(body, "转换与界面", row)
        row += 1

        style_var = tk.StringVar(value=self._label_for(STYLE_CHOICES, self.config.prompt_style))
        wrapper_var = tk.StringVar(value=self._label_for(WRAPPER_CHOICES, self.config.wrapper))
        combo_row = tk.Frame(body, bg=COLORS["card"])
        combo_row.grid(row=row, column=0, columnspan=4, sticky="ew", pady=4)
        row += 1
        for label, var, values, width in (
            ("默认风格", style_var, [label for label, _ in STYLE_CHOICES], 28),
            ("默认包裹", wrapper_var, [label for label, _ in WRAPPER_CHOICES], 20),
        ):
            box = tk.Frame(combo_row, bg=COLORS["card"])
            box.pack(side="left", padx=(0, 20))
            tk.Label(box, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8)).pack(anchor="w")
            ttk.Combobox(box, textvariable=var, values=values, width=width, state="readonly", font=(self.ui_font, 9)).pack(anchor="w")

        auto_copy_var = tk.BooleanVar(value=self.config.auto_copy)
        auto_monitor_var = tk.BooleanVar(value=self.config.auto_monitor)
        start_current_var = tk.BooleanVar(value=self.config.start_with_current)
        checks = tk.Frame(body, bg=COLORS["card"])
        checks.grid(row=row, column=0, columnspan=4, sticky="w", pady=4)
        row += 1
        for text, var in (
            ("识别后自动复制到剪贴板", auto_copy_var),
            ("启动时自动开始监听", auto_monitor_var),
            ("监听开始时转换剪贴板里已有的截图", start_current_var),
        ):
            tk.Checkbutton(
                checks, text=text, variable=var, bg=COLORS["card"],
                activebackground=COLORS["card"], font=(self.ui_font, 9),
            ).pack(side="left", padx=(0, 16))

        hotkey_enabled_var = tk.BooleanVar(value=self.config.hotkey_enabled)
        hotkey_var = tk.StringVar(value=self.config.hotkey)
        hotkey_row = tk.Frame(body, bg=COLORS["card"])
        hotkey_row.grid(row=row, column=0, columnspan=4, sticky="ew", pady=4)
        row += 1
        tk.Checkbutton(
            hotkey_row, text="启用全局快捷键", variable=hotkey_enabled_var,
            bg=COLORS["card"], activebackground=COLORS["card"], font=(self.ui_font, 9),
        ).pack(side="left")
        ttk.Entry(hotkey_row, textvariable=hotkey_var, width=18, font=(self.mono_font, 9)).pack(side="left", padx=(10, 8))
        tk.Label(
            hotkey_row, text="格式：ctrl+alt+s（监听开启时生效，用于调出截图）",
            bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8),
        ).pack(side="left")

        save_dir_var = tk.StringVar(value=str(self.config.resolved_save_dir()))
        dir_row = tk.Frame(body, bg=COLORS["card"])
        dir_row.grid(row=row, column=0, columnspan=4, sticky="ew", pady=4)
        row += 1
        tk.Label(dir_row, text="默认保存目录", bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8)).pack(anchor="w")
        dir_inner = tk.Frame(dir_row, bg=COLORS["card"])
        dir_inner.pack(fill="x")
        ttk.Entry(dir_inner, textvariable=save_dir_var, font=(self.ui_font, 9)).pack(side="left", fill="x", expand=True)
        tk.Button(
            dir_inner, text="浏览…", command=lambda: self._choose_dir(save_dir_var),
            bg="#f1f5f9", fg=COLORS["text"], relief="flat", bd=0, padx=12, pady=4,
            font=(self.ui_font, 8), cursor="hand2",
        ).pack(side="left", padx=(8, 0))

        status_row = tk.Frame(body, bg=COLORS["card"])
        status_row.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        row += 1
        test_btn = tk.Button(
            status_row, text="🔌 测试连接", command=lambda: self._test_connection(
                key_var.get().strip() or self.config.effective_api_key,
                url_var.get().strip(),
                model_var.get().strip(),
                timeout_var.get(),
                result_label,
                test_btn,
            ),
            bg="#f1f5f9", fg=COLORS["text"], relief="flat", bd=0, padx=12, pady=5,
            font=(self.ui_font, 9), cursor="hand2",
        )
        test_btn.pack(side="left")
        result_label = tk.Label(
            status_row, text="", bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8),
        )
        result_label.pack(side="left", padx=(12, 0))

        buttons = tk.Frame(window, bg=COLORS["card"])
        buttons.pack(fill="x", padx=22, pady=(4, 16))
        tk.Button(
            buttons, text="取消", command=window.destroy, bg="#f1f5f9", fg=COLORS["text"],
            relief="flat", bd=0, padx=18, pady=8, font=(self.ui_font, 9), cursor="hand2",
        ).pack(side="right")
        tk.Button(
            buttons,
            text="保存设置",
            command=lambda: self._save_settings(
                window, key_var, url_var, model_var, timeout_var, tokens_var, temp_var,
                detail_var, style_var, wrapper_var, auto_copy_var, auto_monitor_var,
                start_current_var, hotkey_enabled_var, hotkey_var, save_dir_var,
            ),
            bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
            activeforeground="#ffffff", relief="flat", bd=0, padx=22, pady=8,
            font=(self.ui_font, 9, "bold"), cursor="hand2",
        ).pack(side="right", padx=(0, 10))

        if first_run:
            tk.Label(
                window,
                text="首次使用：请先填写 DeepSeek API Key，然后点击「保存设置」。",
                bg=COLORS["amber_soft"], fg=COLORS["amber"], font=(self.ui_font, 9),
                anchor="w", padx=12, pady=7,
            ).pack(fill="x", side="bottom")
        key_entry.focus_set()

    def _section(self, parent: tk.Frame, text: str, row: int) -> None:
        frame = tk.Frame(parent, bg=COLORS["card"])
        frame.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        tk.Label(
            frame, text=text, bg=COLORS["card"], fg=COLORS["accent_dark"],
            font=(self.ui_font, 10, "bold"),
        ).pack(side="left")
        tk.Frame(frame, bg=COLORS["border"], height=1).pack(side="left", fill="x", expand=True, padx=(10, 0))

    def _field_row(
        self,
        parent: tk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        hint: str = "",
    ) -> int:
        tk.Label(parent, text=label, bg=COLORS["card"], fg=COLORS["text"], font=(self.ui_font, 9)).grid(
            row=row, column=0, sticky="w", pady=4
        )
        ttk.Entry(parent, textvariable=variable, font=(self.mono_font, 9)).grid(
            row=row, column=1, columnspan=2, sticky="ew", padx=(10, 8), pady=4
        )
        if hint:
            tk.Label(
                parent, text=hint, bg=COLORS["card"], fg=COLORS["muted"], font=(self.ui_font, 8),
            ).grid(row=row, column=3, sticky="w", pady=4)
        return row + 1

    def _choose_dir(self, variable: tk.StringVar) -> None:
        directory = filedialog.askdirectory(initialdir=variable.get() or str(Path.home()))
        if directory:
            variable.set(directory)

    def _center_child(self, window: tk.Toplevel, width: int, height: int) -> None:
        self.root.update_idletasks()
        x = self.root.winfo_x() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_y() + max(0, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _test_connection(
        self,
        key: str,
        url: str,
        model: str,
        timeout_raw: str,
        result_label: tk.Label,
        button: tk.Button,
    ) -> None:
        if not key:
            result_label.configure(text="请先填写 API Key", fg=COLORS["red"])
            return
        try:
            timeout = max(5.0, min(60.0, float(timeout_raw)))
        except ValueError:
            timeout = 30.0
        result_label.configure(text="正在连接…", fg=COLORS["muted"])
        button.configure(state="disabled")

        def run() -> None:
            try:
                client = DeepSeekVisionClient(
                    key, base_url=url or "https://api.deepseek.com",
                    model=model or "deepseek-flash", timeout=timeout, max_retries=1,
                )
                ok, message = client.check_connection()
            except Exception as exc:  # pragma: no cover - defensive
                ok, message = False, f"初始化失败：{exc}"
            # Never touch Tk from this thread: hand the result to the UI queue.
            self.queue.put(("check_result", ok, message, result_label, button))

        threading.Thread(target=run, daemon=True, name="pic2tex-check").start()

    def _save_settings(
        self,
        window: tk.Toplevel,
        key_var,
        url_var,
        model_var,
        timeout_var,
        tokens_var,
        temp_var,
        detail_var,
        style_var,
        wrapper_var,
        auto_copy_var,
        auto_monitor_var,
        start_current_var,
        hotkey_enabled_var,
        hotkey_var,
        save_dir_var,
    ) -> None:
        old = self.config
        try:
            timeout_value = float(timeout_var.get().strip())
        except ValueError:
            messagebox.showerror("设置无效", "请求超时必须是数字（秒）", parent=window)
            return
        try:
            max_tokens_value = int(tokens_var.get().strip())
        except ValueError:
            messagebox.showerror("设置无效", "最大输出 token 必须是整数", parent=window)
            return
        try:
            temperature_value = float(temp_var.get().strip())
        except ValueError:
            messagebox.showerror("设置无效", "temperature 必须是数字", parent=window)
            return
        new = Config.from_dict(
            {
                **old.to_dict(),
                "api_key": key_var.get().strip(),
                "base_url": url_var.get().strip() or "https://api.deepseek.com",
                "model": model_var.get().strip() or "deepseek-flash",
                "timeout": timeout_value,
                "max_tokens": max_tokens_value,
                "temperature": temperature_value,
                "detail": self._value_for(DETAIL_CHOICES, detail_var.get()),
                "prompt_style": self._value_for(STYLE_CHOICES, style_var.get()),
                "wrapper": self._value_for(WRAPPER_CHOICES, wrapper_var.get()),
                "auto_copy": auto_copy_var.get(),
                "auto_monitor": auto_monitor_var.get(),
                "start_with_current": start_current_var.get(),
                "hotkey_enabled": hotkey_enabled_var.get(),
                "hotkey": hotkey_var.get().strip() or "ctrl+alt+s",
                "save_dir": save_dir_var.get().strip(),
            }
        )
        issues = new.validate()
        if issues:
            messagebox.showerror("设置无效", "\n".join(issues), parent=window)
            return
        if new.hotkey_enabled:
            try:
                from .hotkey import parse_hotkey

                parse_hotkey(new.hotkey)
            except ValueError as exc:
                messagebox.showerror("快捷键无效", str(exc), parent=window)
                return

        self.config = new
        self._persist_config()
        self._update_api_state()
        self._apply_config_to_controls()
        self._update_footer()
        if self.monitor_on and (
            old.hotkey != new.hotkey or old.hotkey_enabled != new.hotkey_enabled
        ):
            self._start_hotkey()
        window.destroy()
        self._settings_window = None
        if self.config.effective_api_key:
            self._notice(f"设置已保存，API Key：{self.config.masked_key()}", "success")
            self._set_status("设置已保存", "success")
        else:
            self._notice("设置已保存，但尚未填写 API Key。", "warn")

    def _report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        log.error("Tk 回调异常", exc_info=(exc_type, exc_value, exc_tb))
        try:
            messagebox.showerror("Pic2TeX 出错", f"{exc_type.__name__}: {exc_value}")
        except Exception:
            pass

    # -------------------------------------------------------------------- demo
    def _load_demo(self) -> None:
        self._notice("演示模式：展示界面与示例输出，不会调用 API。", "info", timeout=12)
        self._set_status("演示模式 · 界面示例", "info")
        image_path = resource_path("assets/demo_formula.png")
        if image_path.exists():
            try:
                payload = load_image_file(image_path, source="file")
                self.current_image = payload
                self.image_info.configure(
                    text=f"示例图片 · {payload.width}×{payload.height} · {payload.size_human}"
                )
                self._render_preview()
            except Exception:
                log.exception("载入演示图片失败")
        self.raw_latex = (
            r"\int_{-\infty}^{+\infty} e^{-x^{2}}\,\mathrm{d}x "
            r"= \sqrt{\pi},\qquad "
            r"\sum_{n=0}^{\infty}\frac{1}{n!} = e"
        )
        self.current_latex = apply_wrapper(self.raw_latex, self.config.wrapper)
        self._set_output(self.current_latex)
        self.output_meta.configure(text="deepseek-flash · 2.1s · 286 tokens（示例）")
        if self.current_image is not None:
            samples = [
                r"\frac{\partial \mathcal{L}}{\partial \theta} = \nabla_\theta \mathcal{L}(\theta)",
                r"\begin{aligned} a^2 + b^2 &= c^2 \\ \sin^2 x + \cos^2 x &= 1 \end{aligned}",
                r"\operatorname{softmax}(z_i) = \frac{e^{z_i}}{\sum_j e^{z_j}}",
            ]
            for raw in samples:
                self._add_history(
                    HistoryEntry(
                        when=datetime.now(),
                        source="演示",
                        image=self.current_image,
                        raw_latex=raw,
                        model="deepseek-flash",
                    )
                )
        self._set_busy(False)

    # ------------------------------------------------------------------- close
    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno("退出 Pic2TeX", "正在识别图片，确定要退出吗？"):
            return
        self.closing = True
        for job in (self._poll_job, self._spin_job, self._notice_job, self._preview_job):
            if not job:
                continue
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._poll_job = self._spin_job = self._notice_job = self._preview_job = None
        try:
            if self.watcher is not None:
                self.watcher.stop()
        except Exception:
            log.exception("退出时停止监听失败")
        self._stop_hotkey()
        self._stop_spinner()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def run_gui(config: Config, *, demo: bool = False, initial_images: Iterable[str] | None = None) -> int:
    """Create the Tk root and run the main loop."""
    enable_high_dpi()
    root = tk.Tk()
    try:
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    Pic2TeXApp(root, config, demo=demo, initial_images=initial_images)
    root.mainloop()
    return 0
