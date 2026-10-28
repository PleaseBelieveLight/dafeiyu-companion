"""Dark local configuration wizard; credentials stay on this computer."""

import ctypes
import json
import os
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

from workspace import RUNTIME_ROOT as ROOT, TOOLS, runtime_env

COLORS = {
    "background": "#101319",
    "sidebar": "#141820",
    "card": "#1a1f29",
    "input": "#11161e",
    "border": "#2b3342",
    "text": "#e9edf5",
    "muted": "#929eaf",
    "accent": "#8ba9ff",
    "accent_hover": "#a2baff",
    "selected": "#25324d",
    "success": "#8dd3b0",
    "error": "#f29ca9",
}
FONT = "Microsoft YaHei UI"


class ConfigurationApp:
    def __init__(self, window, root=ROOT):
        self.window = window
        self.root = Path(root)
        self.path = self.root / "config/settings.json"
        self.settings = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.credentials = json.loads(
            (self.root / "config/local-access.json").read_text(encoding="utf-8-sig")
        )
        self.variables, self.entries, self.pages, self.navigation = {}, {}, {}, {}
        self.wrap_labels = []
        self.events = queue.Queue()
        self.busy = False
        self.active_page = None
        self._build_window()
        self._build_sidebar()
        self._build_content()
        self._build_pages()
        self._build_footer()
        self.initial_values = self._values()
        for variable in self.variables.values():
            variable.trace_add("write", self._mark_changed)
        self.select_page("account")
        self.window.bind("<Control-s>", lambda _: self.save())
        self.window.bind("<MouseWheel>", self._scroll)
        self.window.after(100, self._poll)

    def _build_window(self):
        window = self.window
        window.title("大肥鱼 · 配置向导")
        window.configure(bg=COLORS["background"])
        width = min(1060, window.winfo_screenwidth() - 100)
        height = min(820, window.winfo_screenheight() - 110)
        window.geometry(f"{width}x{height}")
        window.minsize(min(800, width), min(620, height))
        window.option_add("*Font", (FONT, 10))
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=COLORS["border"],
            troughcolor=COLORS["background"],
            borderwidth=0,
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["background"],
            lightcolor=COLORS["background"],
            darkcolor=COLORS["background"],
            gripcount=0,
            width=9,
        )
        style.map("Dark.Vertical.TScrollbar", background=[("active", "#49546a")])
        style.layout(
            "Dark.Vertical.TScrollbar",
            [
                (
                    "Vertical.Scrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            (
                                "Vertical.Scrollbar.thumb",
                                {"expand": "1", "sticky": "nswe"},
                            )
                        ],
                    },
                )
            ],
        )
        style.configure(
            "Dark.Horizontal.TProgressbar",
            background=COLORS["accent"],
            troughcolor=COLORS["background"],
            borderwidth=0,
            thickness=2,
        )
        if os.name == "nt":
            window.update_idletasks()
            try:
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                user32.GetParent.argtypes = [wintypes.HWND]
                user32.GetParent.restype = wintypes.HWND
                handle = user32.GetParent(window.winfo_id())
                value = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(handle), 20, ctypes.byref(value), ctypes.sizeof(value)
                )
            except (AttributeError, OSError):
                pass

    def _label(self, parent, text, *, muted=False, size=10, bold=False, **kwargs):
        return tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg=COLORS["muted" if muted else "text"],
            font=(FONT, size, "bold" if bold else "normal"),
            anchor="w",
            justify="left",
            **kwargs,
        )

    def _button(self, parent, text, command, *, primary=False, compact=False):
        color = COLORS["accent"] if primary else "#283140"
        foreground = "#111722" if primary else COLORS["text"]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg=foreground,
            activebackground=COLORS["accent_hover"] if primary else "#354156",
            activeforeground=foreground,
            disabledforeground="#687488",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            takefocus=True,
            highlightthickness=1,
            highlightbackground=color,
            highlightcolor=COLORS["accent"],
            font=(FONT, 10, "bold" if primary else "normal"),
            padx=14 if compact else 20,
            pady=7 if compact else 10,
        )
        button.bind("<Return>", lambda _: (button.invoke(), "break")[-1])
        return button

    def _build_sidebar(self):
        side = tk.Frame(self.window, bg=COLORS["sidebar"], width=190)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=COLORS["sidebar"])
        brand.pack(fill="x", padx=22, pady=(28, 32))
        logo = tk.Canvas(brand, width=36, height=36, bg=COLORS["sidebar"], highlightthickness=0)
        logo.pack(anchor="w", pady=(0, 12))
        logo.create_oval(1, 1, 35, 35, fill=COLORS["selected"], outline="")
        logo.create_oval(8, 12, 27, 25, fill=COLORS["accent"], outline="")
        logo.create_polygon(25, 17, 31, 12, 31, 25, fill=COLORS["accent"])
        logo.create_oval(12, 15, 15, 18, fill=COLORS["sidebar"], outline="")
        self._label(brand, "大肥鱼", size=19, bold=True).pack(anchor="w")
        self._label(brand, "本地助手配置", muted=True, size=9).pack(anchor="w", pady=(5, 0))
        self._label(side, "配置", muted=True, size=9).pack(anchor="w", padx=24, pady=(0, 10))
        for number, key, title in [
            ("01", "account", "QQ 与聊天"),
            ("02", "speech", "看图与语音"),
            ("03", "image", "画图与自拍"),
            ("04", "extras", "主动功能"),
            ("05", "access", "管理入口"),
        ]:
            item = tk.Frame(side, bg=COLORS["sidebar"])
            item.pack(fill="x", padx=14, pady=3)
            indicator = tk.Frame(item, bg=COLORS["sidebar"], width=3)
            indicator.pack(side="left", fill="y")
            button = tk.Button(
                item,
                text=f"{number}   {title}",
                anchor="w",
                relief="flat",
                borderwidth=0,
                bg=COLORS["sidebar"],
                fg=COLORS["muted"],
                activebackground=COLORS["selected"],
                activeforeground=COLORS["text"],
                cursor="hand2",
                padx=12,
                pady=12,
                font=(FONT, 10),
                takefocus=True,
                highlightthickness=0,
                command=lambda page=key: self.select_page(page),
            )
            button.pack(fill="x")
            self.navigation[key] = (item, indicator, button)
        bottom = tk.Frame(side, bg=COLORS["sidebar"])
        bottom.pack(side="bottom", fill="x", padx=24, pady=24)
        tk.Frame(bottom, bg=COLORS["border"], height=1).pack(fill="x", pady=(0, 15))
        self._label(bottom, "配置保存在本机", muted=True, size=9).pack(anchor="w")
        self._label(bottom, str(self.root), muted=True, size=9, wraplength=145).pack(
            anchor="w", pady=(5, 0)
        )

    def _build_content(self):
        self.main = tk.Frame(self.window, bg=COLORS["background"])
        self.main.pack(side="left", fill="both", expand=True)
        header = tk.Frame(self.main, bg=COLORS["background"])
        header.pack(fill="x", padx=30, pady=(23, 18))
        pill = self._label(header, "仅本机访问", size=9, padx=11, pady=5)
        pill.configure(bg=COLORS["selected"], fg=COLORS["accent"])
        pill.pack(side="right", anchor="n", pady=5)
        self.title = self._label(header, "", size=22, bold=True)
        self.title.pack(anchor="w")
        self.description = self._label(header, "", muted=True, wraplength=560)
        self.description.pack(anchor="w", pady=(7, 0))
        self.viewport = tk.Frame(self.main, bg=COLORS["background"])
        self.viewport.pack(fill="both", expand=True, padx=(30, 19))
        self.canvas = tk.Canvas(
            self.viewport, bg=COLORS["background"], highlightthickness=0, borderwidth=0
        )
        self.scrollbar = ttk.Scrollbar(
            self.viewport,
            orient="vertical",
            command=self.canvas.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.scrollbar.pack(side="right", fill="y", padx=(12, 0))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas_item = self.canvas.create_window(0, 0, anchor="nw")
        self.canvas.bind("<Configure>", self._resize_content)

    def _page(self, key, title, description):
        page = tk.Frame(self.canvas, bg=COLORS["background"])
        page.bind(
            "<Configure>",
            lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.pages[key] = (page, title, description)
        return page

    def _card(self, page, title, description=""):
        card = tk.Frame(
            page,
            bg=COLORS["card"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=20,
            pady=15,
        )
        card.pack(fill="x", pady=(0, 16))
        self._label(card, title, size=12, bold=True).pack(anchor="w")
        if description:
            label = self._label(card, description, muted=True, size=9, wraplength=620)
            label.pack(anchor="w", pady=(5, 0))
            self.wrap_labels.append(label)
        body = tk.Frame(card, bg=COLORS["card"])
        body.pack(fill="x", pady=(12, 0))
        body.columnconfigure(0, weight=1, uniform="fields")
        body.columnconfigure(1, weight=1, uniform="fields")
        return body

    def _field(
        self,
        parent,
        key,
        label,
        row,
        column=0,
        *,
        span=1,
        secret=False,
        value=None,
        readonly=False,
    ):
        field = tk.Frame(parent, bg=COLORS["card"])
        field.grid(
            row=row,
            column=column,
            columnspan=span,
            sticky="ew",
            padx=(0, 14 if span == 1 and column == 0 else 0),
            pady=(0, 10),
        )
        self._label(field, label, size=9).pack(anchor="w", pady=(0, 7))
        shell = tk.Frame(
            field,
            bg=COLORS["input"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        shell.pack(fill="x")
        variable = tk.StringVar(value=str(self.settings.get(key, "") if value is None else value))
        if not readonly:
            self.variables[key] = variable
        entry = tk.Entry(
            shell,
            textvariable=variable,
            width=10,
            relief="flat",
            borderwidth=0,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["accent"],
            selectbackground=COLORS["selected"],
            selectforeground=COLORS["text"],
            readonlybackground=COLORS["input"],
            font=(FONT, 10),
            show="•" if secret else "",
            state="readonly" if readonly else "normal",
        )
        entry.pack(side="left", fill="x", expand=True, padx=10, pady=7)
        entry.bind("<FocusIn>", lambda _: shell.configure(highlightbackground=COLORS["accent"]))
        entry.bind(
            "<FocusOut>",
            lambda _: shell.configure(highlightbackground=COLORS["border"]),
        )
        entry.bind("<Control-a>", lambda _: (entry.selection_range(0, "end"), "break")[-1])
        if secret:
            toggle = tk.Button(
                shell,
                text="显示",
                width=4,
                bg=COLORS["input"],
                fg=COLORS["muted"],
                activebackground=COLORS["input"],
                activeforeground=COLORS["accent"],
                relief="flat",
                borderwidth=0,
                cursor="hand2",
                font=(FONT, 9),
            )

            def reveal():
                hidden = bool(entry.cget("show"))
                entry.configure(show="" if hidden else "•")
                toggle.configure(text="隐藏" if hidden else "显示")

            toggle.configure(command=reveal)
            toggle.pack(side="right", padx=(0, 7))
        self.entries[key] = entry
        return entry

    def _build_pages(self):
        page = self._page("account", "QQ 与聊天", "连接机器人账号，设置聊天模型和可使用的人。")
        card = self._card(page, "QQ 账号", "群号、好友 QQ 有多个时，用英文逗号分隔。")
        self._field(card, "owner_qq", "你的 QQ 号", 0)
        self._field(card, "bot_qq", "机器人 QQ 号", 0, 1)
        self._field(card, "group_ids", "允许的群号", 1)
        self._field(card, "friend_qqs", "其他允许的好友 QQ", 1, 1)
        card = self._card(page, "聊天模型", "使用 DeepSeek 处理日常对话。API Key 默认隐藏。")
        self._field(card, "deepseek_api_key", "DeepSeek API Key", 0, span=2, secret=True)
        self._field(card, "deepseek_model", "模型 ID", 1, span=2)
        self._button(
            card,
            "打开 DeepSeek 控制台  ↗",
            lambda: webbrowser.open("https://platform.deepseek.com/"),
            compact=True,
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        page = self._page("speech", "看图、视频与语音", "配置百炼的视觉理解、语音识别和语音回复。")
        card = self._card(page, "阿里云百炼", "看图、视频理解与听写共用这里的 API Key。")
        self._field(card, "dashscope_api_key", "百炼 API Key", 0, span=2, secret=True)
        self._field(card, "vision_model", "视觉模型 ID", 1, span=2)
        self._button(
            card,
            "打开百炼控制台  ↗",
            lambda: webbrowser.open("https://bailian.console.aliyun.com/"),
            compact=True,
        ).grid(row=2, column=0, columnspan=2, sticky="w")
        card = self._card(
            page,
            "语音回复",
            "填写与模型匹配的音色 ID 后使用百炼发声；留空时使用 Edge 普通女声。",
        )
        self._field(card, "voice_id", "音色 ID", 0, span=2)
        self._field(card, "tts_model", "语音模型 ID", 1, span=2)
        self._label(
            card,
            "/语音开 允许自动语音，/语音关 关闭普通聊天语音。\n主动消息保持独立自动语音。",
            muted=True,
            size=9,
            wraplength=430,
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        page = self._page("image", "画图与自拍", "设置生图接口，画图与自拍可以使用同一个服务。")
        card = self._card(page, "生图服务", "填写完整生图接口地址，以及平台提供的模型 ID。")
        self._field(card, "image_api_key", "生图 API Key", 0, span=2, secret=True)
        self._field(card, "image_api_url", "接口地址", 1, span=2)
        self._field(card, "image_model", "画图模型 ID", 2)
        self._field(card, "selfie_model", "自拍模型 ID", 2, 1)
        self._button(
            card,
            "火山方舟控制台  ↗",
            lambda: webbrowser.open("https://console.volcengine.com/ark"),
            compact=True,
        ).grid(row=3, column=0, sticky="w")
        self._button(
            card,
            "硅基流动控制台  ↗",
            lambda: webbrowser.open("https://cloud.siliconflow.cn/"),
            compact=True,
        ).grid(row=3, column=1, sticky="w")
        card = self._card(page, "快速验证", "保存配置并等待服务启动后，在 QQ 私聊机器人发送：")
        self._label(card, "/画 一只橘猫坐在窗边", size=10).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        self._label(card, "/自拍 在公园里看着镜头微笑", size=10).grid(
            row=1, column=0, columnspan=2, sticky="w"
        )

        page = self._page("extras", "主动功能", "让大肥鱼按你的节奏，主动聊天、问候和汇报。")
        self._switch_card(
            page,
            "proactive_enabled",
            "主动聊天",
            "每 2～4 小时主动联系一次，23:00～08:00 免打扰。",
        )
        self._switch_card(
            page,
            "scheduled_enabled",
            "每日安排",
            "北京时间 07:00 早安、20:30 逛 B 站汇报、23:30 催睡觉。",
        )
        card = self._card(
            page,
            "虚拟手机",
            "逛 B 站汇报需要专用模拟器已启动。主动消息发送到“你的 QQ 号”。",
        )
        self._button(card, "启动虚拟手机", lambda: self._control("phone-start"), compact=True).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )

        page = self._page("access", "管理入口", "快速打开面板，查看账号和连接状态。")
        card = self._card(page, "AstrBot", "聊天、模型、插件和日志管理。")
        self._field(
            card,
            "dashboard_url",
            "面板地址",
            0,
            span=2,
            value="http://127.0.0.1:6185",
            readonly=True,
        )
        self._field(
            card,
            "dashboard_username",
            "用户名",
            1,
            value=self.credentials["username"],
            readonly=True,
        )
        self._field(
            card,
            "dashboard_password",
            "面板密码",
            1,
            1,
            value=self.credentials["password"],
            secret=True,
            readonly=True,
        )
        self._button(
            card,
            "打开管理面板  ↗",
            lambda: webbrowser.open("http://127.0.0.1:6185"),
            compact=True,
        ).grid(row=2, column=0, sticky="w")
        self._button(card, "复制面板密码", self._copy_password, compact=True).grid(
            row=2, column=1, sticky="w"
        )
        card = self._card(page, "NapCat", "管理 QQ 登录与消息连接，快捷入口会自动填入登录密钥。")
        self._button(
            card, "打开 NapCat  ↗", lambda: self._control("open-napcat"), compact=True
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        note = self._label(
            card,
            "保存配置会保留 QQ 登录。首次登录或登录凭证失效时，需要手机 QQ 授权。",
            muted=True,
            size=9,
            wraplength=620,
        )
        note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(15, 0))
        self.wrap_labels.append(note)

    def _switch_card(self, page, key, title, description):
        card = self._card(page, title, description)
        variable = tk.BooleanVar(value=bool(self.settings.get(key, False)))
        self.variables[key] = variable
        switch = self._button(card, "", lambda: variable.set(not variable.get()), compact=True)
        switch.grid(row=0, column=1, sticky="e")
        self._label(card, "功能状态", muted=True, size=9).grid(row=0, column=0, sticky="w")

        def refresh(*_):
            on = variable.get()
            color = COLORS["selected"] if on else "#283140"
            switch.configure(
                text="已开启  ●" if on else "已关闭  ○",
                bg=color,
                fg=COLORS["accent"] if on else COLORS["muted"],
                highlightbackground=color,
            )

        variable.trace_add("write", refresh)
        refresh()

    def _build_footer(self):
        tk.Frame(self.main, height=1, bg=COLORS["border"]).pack(fill="x", pady=(12, 0))
        self.progress_track = tk.Frame(self.main, bg=COLORS["background"], height=3)
        self.progress_track.pack(fill="x")
        self.progress_track.pack_propagate(False)
        self.progress = ttk.Progressbar(
            self.progress_track,
            style="Dark.Horizontal.TProgressbar",
            mode="indeterminate",
        )
        footer = tk.Frame(self.main, bg=COLORS["background"])
        footer.pack(fill="x", padx=30, pady=(17, 22))
        self.save_button = self._button(footer, "保存配置并启动", self.save, primary=True)
        self.save_button.pack(side="right")
        self.status = self._label(footer, "配置已载入", size=10)
        self.status.pack(anchor="w", pady=(0, 5))
        self.status_detail = self._label(
            footer, "修改后保存即可生效  ·  Ctrl+S", muted=True, size=9, wraplength=350
        )
        self.status_detail.pack(anchor="w")

    def select_page(self, key):
        self.active_page = key
        page, title, description = self.pages[key]
        self.title.configure(text=title)
        self.description.configure(text=description)
        self.canvas.itemconfigure(
            self.canvas_item, window=page, width=max(1, self.canvas.winfo_width())
        )
        for name, (item, indicator, button) in self.navigation.items():
            active = name == key
            color = COLORS["selected"] if active else COLORS["sidebar"]
            item.configure(bg=color)
            indicator.configure(bg=COLORS["accent"] if active else COLORS["sidebar"])
            button.configure(bg=color, fg=COLORS["text"] if active else COLORS["muted"])
        self.window.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0)

    def _resize_content(self, event):
        self.canvas.itemconfigure(self.canvas_item, width=event.width)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        for label in self.wrap_labels:
            label.configure(wraplength=max(260, event.width - 44))
        self.description.configure(wraplength=max(240, event.width - 150))
        if hasattr(self, "status_detail"):
            self.status_detail.configure(wraplength=max(200, event.width - 240))

    def _scroll(self, event):
        if (
            self.active_page
            and self.pages[self.active_page][0].winfo_reqheight() > self.canvas.winfo_height()
        ):
            self.canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")

    def _values(self):
        return {key: variable.get() for key, variable in self.variables.items()}

    def _mark_changed(self, *_):
        if not self.busy:
            changed = self._values() != self.initial_values
            self.status.configure(
                text="有未保存的修改" if changed else "配置已载入",
                fg=COLORS["accent"] if changed else COLORS["text"],
            )
            self.status_detail.configure(
                text="保存后应用到机器人。" if changed else "修改后保存即可生效  ·  Ctrl+S"
            )

    def _control(self, action):
        subprocess.Popen(
            [
                str(self.root / "runtime/astrbot/Scripts/pythonw.exe"),
                str(TOOLS / "control.py"),
                action,
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=runtime_env(self.root),
        )

    def _copy_password(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.credentials["password"])
        if not self.busy:
            self.status.configure(text="面板密码已复制", fg=COLORS["success"])

    def save(self):
        if self.busy:
            return
        values = self._values()
        for key in ("owner_qq", "bot_qq"):
            value = values[key].strip()
            if value and not value.isdigit():
                self.select_page("account")
                self.entries[key].focus_set()
                self.status.configure(text="QQ 号只能填写数字", fg=COLORS["error"])
                self.status_detail.configure(text="请检查你的 QQ 号和机器人 QQ 号。")
                return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8-sig"))
            updated = {**current, **values}
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)
        except (OSError, ValueError):
            self.status.configure(text="配置保存失败", fg=COLORS["error"])
            self.status_detail.configure(text="请确认配置文件可读取、写入。")
            return
        self.settings = updated
        self.initial_values = values
        self.busy = True
        self.save_button.configure(state="disabled", text="正在应用…")
        self.status.configure(text="正在应用配置，QQ 保持在线", fg=COLORS["accent"])
        self.status_detail.configure(text="聊天服务约需 30～60 秒恢复，请稍候。")
        self.progress.pack(fill="both", expand=True)
        self.progress.start(12)
        threading.Thread(target=self._apply, daemon=True).start()

    def _apply(self):
        try:
            with (self.root / "logs/apply-config.log").open("ab") as log:
                result = subprocess.run(
                    [
                        str(self.root / "runtime/astrbot/Scripts/python.exe"),
                        str(TOOLS / "control.py"),
                        "apply",
                    ],
                    stdout=log,
                    stderr=log,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env=runtime_env(self.root),
                )
            if result.returncode:
                self.events.put(
                    ("error", "配置应用失败", "请查看 logs/apply-config.log 中的错误。")
                )
                return
            self.events.put(
                (
                    "progress",
                    "配置已应用，正在等待聊天服务",
                    "QQ 登录保持不变，服务就绪后即可聊天。",
                )
            )
            for _ in range(60):
                try:
                    for port in (6185, 6199):
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            pass
                    self.events.put(
                        (
                            "success",
                            "配置已生效，聊天服务已就绪",
                            "QQ 登录已保留，可以继续聊天。",
                        )
                    )
                    return
                except OSError:
                    time.sleep(1)
            self.events.put(
                (
                    "pending",
                    "配置已保存，服务仍在启动",
                    "请稍后到管理面板查看服务状态。",
                )
            )
        except OSError:
            self.events.put(
                (
                    "error",
                    "配置应用失败",
                    "请检查本地运行环境和 logs/apply-config.log。",
                )
            )

    def _poll(self):
        try:
            while True:
                kind, text, detail = self.events.get_nowait()
                color = (
                    COLORS["error"]
                    if kind == "error"
                    else COLORS["success"]
                    if kind == "success"
                    else COLORS["accent"]
                )
                self.status.configure(text=text, fg=color)
                self.status_detail.configure(text=detail)
                if kind != "progress":
                    self.busy = False
                    self.progress.stop()
                    self.progress.configure(value=0)
                    self.progress.pack_forget()
                    self.save_button.configure(state="normal", text="保存配置并启动")
        except queue.Empty:
            pass
        self.window.after(100, self._poll)


def main():
    window = tk.Tk()
    ConfigurationApp(window)
    window.mainloop()


if __name__ == "__main__":
    main()
