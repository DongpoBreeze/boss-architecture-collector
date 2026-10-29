"""Legacy-workbench presentation, with compatible preference re-exports.

The legacy workflow remains in desktop_app; result loading/filtering runs off
the Tk thread and widget changes use its event queue. Both workbenches share
the whitelisted preference helpers in desktop_preferences.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from urllib.parse import urlparse

import desktop_data
import desktop_preferences
from desktop_preferences import PREFERENCE_FIELDS, preference_path
from desktop_routing import AUTO_RUN_CHOICE, NEW_RUN_CHOICE, resolve_daily_root
from desktop_tk_lifecycle import BooleanVar, PhotoImage, StringVar
from screening_policy import AI_DEFAULT_MODEL, AI_DEFAULT_REASONING_EFFORT
from desktop_theme import (
    LIGHT_PALETTE, configure_combobox_popdown, configure_styles,
    normalize_theme, palette_for,
)


# Backwards-compatible daytime constants. Live widgets always use self.colors;
# never mutate a process-global palette when one window switches themes.
COLORS = LIGHT_PALETTE
STATUS_LABELS = {
    "success": "已完成", "completed": "已完成", "partial": "部分完成",
    "running": "运行中", "pending": "待开始", "paused": "已暂停",
    "failed": "失败", "stopped_or_failed": "已停止 / 失败",
    "interrupted_or_failed": "已中断", "not_run": "未运行",
    "configuration_failed": "配置失败", "skipped_no_candidates": "无候选，已跳过",
    "stale": "上游已更新，待重跑",
}
def load_preferences(path: Path | None = None) -> dict:
    """Legacy import/patch compatibility; storage lives in the shared module."""
    return desktop_preferences.load_preferences(path if path is not None else preference_path())


def save_preferences(values: dict, path: Path | None = None) -> None:
    desktop_preferences.save_preferences(values, path if path is not None else preference_path())


def format_count(value) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "—"


def progress_fraction(value: str) -> tuple[int, int] | None:
    import re
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", value or "")
    if not match:
        return None
    done, total = map(int, match.groups())
    return (min(done, total), total) if total else None


class WorkspaceMixin:
    """UI-only methods; the host supplies the existing workflow callbacks."""

    def _build_workspace(self, city_choices, track_choices, stage_choices) -> None:
        self.theme = StringVar(master=self.root, value=normalize_theme(load_preferences().get("theme")))
        self.theme_button_text = StringVar(master=self.root)
        self.colors = palette_for(self.theme.get())
        self._theme_roles: dict[tk.Widget, dict[str, str]] = {}
        configure_styles(self.root, self.colors)
        self._workspace_ready = False
        self._viewed_run: Path | None = None
        self._history_rows: list[dict] = []
        self._result_jobs: list[dict] = []
        self._result_source_root: Path | None = None
        self._filtered_jobs: list[dict] = []
        self._shown_jobs: dict[str, dict] = {}
        self._results_request_id = 0
        self._filter_request_id = 0
        self._history_request_id = 0
        self._snapshot_busy = False
        self._live_after_id = None
        self._search_after_id = None
        self._filter_page = 0
        self._sort_by = "priority"
        self._sort_reverse = False
        self._settings_controls = []
        self.auto_scroll = BooleanVar(master=self.root, value=True)
        self.result_search = StringVar(master=self.root)
        self.result_class = StringVar(master=self.root, value="全部")
        self.result_city = StringVar(master=self.root, value="全部")
        self.result_jd = StringVar(master=self.root, value="全部")
        self.result_model = StringVar(master=self.root, value="全部")
        self.result_count = StringVar(master=self.root, value="选择历史运行，或运行后在这里查看岗位和证据。")
        self.result_location = StringVar(master=self.root, value="尚未载入运行结果")
        self.result_warning = StringVar(master=self.root)
        self.result_page = StringVar(master=self.root)
        self.history_hint = StringVar(master=self.root, value="查看不会改变历史结果；只有“载入续跑设置”后再开始运行，才会复用检查点。")
        self.stage_hint = StringVar(master=self.root)
        self.live_detail = StringVar(master=self.root, value="统计从岗位 JSON 与检查点读取；未生成的数据以 — 显示。")
        self.updated_text = StringVar(master=self.root, value="等待选择任务")
        self.stage_caption = StringVar(master=self.root, value="尚未开始")
        self._metric_values = {key: StringVar(master=self.root, value="—") for key in ("unique", "jd", "llm", "priority", "pool", "excluded")}
        self._metric_notes = {key: StringVar(master=self.root, value="等待数据") for key in self._metric_values}
        if not hasattr(self, "routing_summary"):
            self.routing_summary = StringVar(master=self.root, value="自动识别采集列表、已有 JD 与模型结果；选择运行阶段后查看数据来源。")

        shell = ttk.Frame(self.root, padding=(22, 16, 22, 12))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(2, weight=1)
        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="BOSS 岗位工作台", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="采集 · 整理 · 语义复核 · 证据追溯", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.status_badge = self._theme_widget(tk.Label(header, textvariable=self.status_text, padx=14, pady=9, anchor="e", justify="right", wraplength=420, font=("Microsoft YaHei UI", 10, "bold")), background="accent_soft", foreground="accent")
        self.status_badge.grid(row=0, column=1, rowspan=2, sticky="e")
        self.theme_button = ttk.Button(header, textvariable=self.theme_button_text, style="Theme.TButton", command=self._toggle_theme)
        self.theme_button.grid(row=0, column=2, rowspan=2, sticky="e", padx=(12, 0))

        toolbar = ttk.Frame(shell)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.start_button = ttk.Button(toolbar, text="开始运行", style="Primary.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(toolbar, text="暂停并保留进度", command=self._pause_workflow, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.continue_button = ttk.Button(toolbar, textvariable=self.prompt_button_text, command=self._continue_child, state="disabled")
        self.continue_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(toolbar, text="停止", style="Danger.TButton", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.prepare_browser_button = ttk.Button(toolbar, text="准备浏览器 / 登录", command=self._prepare_browser)
        self.prepare_browser_button.pack(side="right")
        ttk.Button(toolbar, text="打开结果目录", command=self._open_run_directory).pack(side="right", padx=(0, 8))

        self.notebook = ttk.Notebook(shell)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.task_page = ttk.Frame(self.notebook, padding=(12, 15))
        self.results_page = ttk.Frame(self.notebook, padding=(16, 15))
        self.history_page = ttk.Frame(self.notebook, padding=(16, 15))
        self.notebook.add(self.task_page, text="01  任务工作台")
        self.notebook.add(self.results_page, text="02  岗位结果")
        self.notebook.add(self.history_page, text="03  运行历史")
        self._build_task_page(city_choices, track_choices, stage_choices)
        self._build_results_page(city_choices)
        self._build_history_page()
        footer = ttk.Frame(shell)
        footer.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.output_directory, style="Muted.TLabel", wraplength=1060).grid(row=0, column=0, sticky="w")
        ttk.Label(footer, text="只采集与分析 · 不自动投递", style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.stage.trace_add("write", lambda *_: self._update_stage_hint())
        self.result_search.trace_add("write", lambda *_: self._debounce_filters())
        for variable in (self.result_class, self.result_city, self.result_jd, self.result_model):
            variable.trace_add("write", lambda *_: self._apply_result_filters())
        self.root.bind("<Control-f>", self._focus_result_search)
        self.root.bind("<F5>", lambda _: self._refresh_current_view())
        self.root.bind_all("<MouseWheel>", self._scroll_settings, add="+")
        self._update_stage_hint()
        self._set_theme(self.theme.get(), persist=False)
        self._workspace_ready = True
        self._live_after_id = self.root.after(800, self._refresh_live)
        try:
            icon = PhotoImage(master=self.root, width=32, height=32)
            icon.put(self.colors["primary"], to=(0, 0, 32, 32))
            for x, y, width in ((7, 8, 18), (7, 15, 13), (7, 22, 18)):
                icon.put(self.colors["on_primary"], to=(x, y, x + width, y + 3))
            self.root.iconphoto(True, icon)
            self._app_icon = icon  # Retain PhotoImage; Tk does not keep its Python reference alive.
        except tk.TclError:
            pass

    def _theme_widget(self, widget, **roles):
        """Register semantic roles, including a widget's current status tone."""
        assigned = self._theme_roles.setdefault(widget, {})
        assigned.update(roles)
        widget.configure(**{option: self.colors[role] for option, role in assigned.items()})
        return widget

    def _theme_text(self, widget, *, log=False):
        return self._theme_widget(
            widget, background="log" if log else "surface",
            foreground="log_text" if log else "text",
            insertbackground="log_text" if log else "text",
            selectbackground="log_selection" if log else "selection",
            selectforeground="log_selected_text" if log else "selected_text",
            inactiveselectbackground="log_selection" if log else "selection",
            highlightbackground="border", highlightcolor="focus",
        )

    def _set_theme(self, theme: str, *, persist: bool = True) -> None:
        """Live switch API: preserve widgets, content, selection and workflow."""
        selected = normalize_theme(theme)
        self.theme.set(selected)
        self.colors = palette_for(selected)
        configure_styles(self.root, self.colors)
        for widget, roles in tuple(self._theme_roles.items()):
            try:
                widget.configure(**{option: self.colors[role] for option, role in roles.items()})
            except tk.TclError:
                self._theme_roles.pop(widget, None)
        for tag, role in (("warning", "log_warning"), ("error", "log_error"), ("success", "log_success")):
            self.log_view.tag_configure(tag, foreground=self.colors[role])
        for tag, role in (("重点投递", "table_priority"), ("海投池", "table_pool"), ("暂不投", "table_excluded")):
            self.results_table.tag_configure(tag, background=self.colors[role], foreground=self.colors["text"])
        self.routing_summary_text.tag_configure("warning", foreground=self.colors["amber"])
        pending = [self.root]
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, ttk.Combobox):
                try:
                    configure_combobox_popdown(widget, self.colors)
                except tk.TclError:
                    # Some non-Windows Tk builds use a native popdown instead.
                    pass
            elif isinstance(widget, tk.Menu):
                self._theme_widget(widget, background="surface", foreground="text",
                                   activebackground="selection", activeforeground="selected_text",
                                   disabledforeground="disabled_text", selectcolor="accent")
        self.theme_button_text.set("日间模式" if selected == "dark" else "夜间模式")
        if persist:
            self._save_workspace_preferences()

    def _toggle_theme(self) -> None:
        self._set_theme("light" if self.theme.get() == "dark" else "dark")

    def _render_routing_summary(self, *_args) -> None:
        widget = self.routing_summary_text
        position = widget.yview()[0]
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        value = self.routing_summary.get()
        widget.insert("1.0", value)
        for number, line in enumerate(value.splitlines(), 1):
            if any(word in line for word in ("警告", "注意", "缺失", "无法", "未找到", "不会覆盖")):
                widget.tag_add("warning", f"{number}.0", f"{number}.end")
        widget.configure(state="disabled")
        widget.yview_moveto(position)

    def _card(self, parent, title: str):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        ttk.Label(card, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        return card

    def _register_setting(self, widget, state="normal"):
        self._settings_controls.append((widget, state))
        return widget

    def _build_task_page(self, cities, tracks, stages) -> None:
        self.task_page.columnconfigure(0, minsize=345, weight=0)
        self.task_page.columnconfigure(1, weight=1)
        self.task_page.rowconfigure(0, weight=1)
        settings = ttk.Frame(self.task_page)
        settings.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        settings.rowconfigure(0, weight=1)
        settings.columnconfigure(0, weight=1)
        self.settings_canvas = self._theme_widget(tk.Canvas(settings, width=350, borderwidth=0, highlightthickness=0), background="background")
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(settings, orient="vertical", command=self.settings_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        self.settings_body = ttk.Frame(self.settings_canvas)
        window = self.settings_canvas.create_window((0, 0), window=self.settings_body, anchor="nw")
        self.settings_body.bind("<Configure>", lambda _: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.bind("<Configure>", lambda e: self.settings_canvas.itemconfigure(window, width=e.width))

        source = self._card(self.settings_body, "数据源与范围")
        source.pack(fill="x", pady=(0, 10))
        ttk.Label(source, text="采集总文件夹（某天的目录）", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        line = ttk.Frame(source, style="Card.TFrame")
        line.pack(fill="x", pady=(0, 10))
        self._register_setting(ttk.Entry(line, textvariable=self.data_root, width=18)).pack(side="left", fill="x", expand=True)
        self._register_setting(ttk.Button(line, text="选择", style="Small.TButton", command=self._choose_root)).pack(side="right", padx=(6, 0))
        ttk.Label(source, text="只选这里一次，程序自动识别 raw、JD、模型结果", style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(0, 10))
        row = ttk.Frame(source, style="Card.TFrame")
        row.pack(fill="x")
        for column, title, variable, values in ((0, "城市", self.city, cities), (1, "岗位轨道", self.track, tracks)):
            row.columnconfigure(column, weight=1, uniform="scope")
            ttk.Label(row, text=title, style="Hint.TLabel").grid(row=0, column=column, sticky="w", padx=(0 if column == 0 else 8, 0), pady=(0, 4))
            box = self._register_setting(ttk.Combobox(row, textvariable=variable, values=values, state="readonly", width=12), "readonly")
            box.grid(row=1, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
            box.bind("<<ComboboxSelected>>", lambda _: self._refresh_stats())
        ttk.Label(source, textvariable=self.stats_text, style="Hint.TLabel", wraplength=300).pack(anchor="w", fill="x", pady=(12, 4))
        ttk.Button(source, text="刷新城市数据量", style="Small.TButton", command=self._refresh_stats).pack(anchor="w")

        task = self._card(self.settings_body, "本次任务")
        task.pack(fill="x", pady=(0, 10))
        ttk.Label(task, text="运行阶段", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        self._register_setting(ttk.Combobox(task, textvariable=self.stage, values=stages, state="readonly", width=26), "readonly").pack(fill="x")
        ttk.Label(task, textvariable=self.stage_hint, style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(7, 10))
        ttk.Label(task, text="后处理数量（去重岗位，留空 = 全部）", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        self._register_setting(ttk.Entry(task, textvariable=self.sample_size)).pack(fill="x")
        presets = ttk.Frame(task, style="Card.TFrame")
        presets.pack(fill="x", pady=(7, 10))
        for label, value in (("50 岗", "50"), ("200 岗", "200"), ("全部", "")):
            self._register_setting(ttk.Button(presets, text=label, style="Small.TButton", command=lambda v=value: self.sample_size.set(v))).pack(side="left", padx=(0, 5))
        ttk.Label(task, text="数据路由", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        choices = list(dict.fromkeys((AUTO_RUN_CHOICE, NEW_RUN_CHOICE, self.resume_run.get() or AUTO_RUN_CHOICE)))
        self.history_values = choices
        self.history_box = self._register_setting(ttk.Combobox(task, textvariable=self.resume_run, values=choices, state="readonly", width=25), "readonly")
        self.history_box.pack(fill="x")
        ttk.Label(task, text="推荐自动选择：按本次范围识别已有列表、JD 与模型结果。新建运行或指定历史目录是高级选项。", style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(7, 7))
        routing = ttk.Frame(task, style="Card.TFrame")
        routing.pack(fill="x")
        routing.columnconfigure(0, weight=1)
        self.routing_summary_text = self._theme_text(tk.Text(routing, wrap="word", height=5, width=25, state="disabled", relief="flat", font=("Microsoft YaHei UI", 9), padx=4, pady=5, highlightthickness=1))
        self.routing_summary_text.grid(row=0, column=0, sticky="ew")
        routing_scroll = ttk.Scrollbar(routing, orient="vertical", command=self.routing_summary_text.yview)
        routing_scroll.grid(row=0, column=1, sticky="ns")
        self.routing_summary_text.configure(yscrollcommand=routing_scroll.set)
        self._routing_summary_trace_id = self.routing_summary.trace_add("write", self._render_routing_summary)
        self._render_routing_summary()

        advanced = self._card(self.settings_body, "采集与并发设置")
        advanced.pack(fill="x", pady=(0, 10))
        ttk.Label(advanced, text="上游采集工具（仅采集/JD需要，通常自动发现）", style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(0, 4))
        scraper_line = ttk.Frame(advanced, style="Card.TFrame")
        scraper_line.pack(fill="x", pady=(0, 10))
        self._register_setting(ttk.Entry(scraper_line, textvariable=self.scraper_root, width=18)).pack(side="left", fill="x", expand=True)
        self._register_setting(ttk.Button(scraper_line, text="选择", style="Small.TButton", command=self._choose_scraper)).pack(side="right", padx=(6, 0))
        grid = ttk.Frame(advanced, style="Card.TFrame")
        grid.pack(fill="x")
        settings_rows = (("检索组合数（空 = 全部）", self.combination_limit, None), ("每组合页数", self.pages, (1, 10)), ("语义复核并发", self.workers, (1, 32)), ("JD 每批岗位数", self.batch_size, (1, 30)))
        grid.columnconfigure(1, weight=1)
        for number, (label, variable, bounds) in enumerate(settings_rows):
            ttk.Label(grid, text=label, style="Hint.TLabel").grid(row=number, column=0, sticky="w", padx=(0, 10), pady=4)
            widget = ttk.Spinbox(grid, textvariable=variable, from_=bounds[0], to=bounds[1], width=7) if bounds else ttk.Entry(grid, textvariable=variable, width=7)
            self._register_setting(widget).grid(row=number, column=1, sticky="ew", pady=4)
        ttk.Label(advanced, text=f"语义复核：{AI_DEFAULT_MODEL} / {AI_DEFAULT_REASONING_EFFORT}。\nURL / 密钥沿用本机 Codex 配置，不修改全局设置。\n仅复用签名匹配的检查点；并发仅用于模型复核。", style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(10, 0))

        right = ttk.Frame(self.task_page)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(4, weight=1)
        progress = ttk.Frame(right, style="Card.TFrame", padding=(14, 10))
        progress.grid(row=0, column=0, sticky="ew")
        progress_heading = ttk.Frame(progress, style="Card.TFrame")
        progress_heading.pack(fill="x", pady=(0, 8))
        ttk.Label(progress_heading, text="工作流进度", style="Section.TLabel").pack(side="left")
        ttk.Label(progress_heading, textvariable=self.updated_text, style="Hint.TLabel").pack(side="right")
        self._step_labels = {}
        steps = ttk.Frame(progress, style="Card.TFrame")
        steps.pack(fill="x", pady=(0, 7))
        for column, (name, short) in enumerate((("岗位采集", "采集"), ("列表前处理", "前处理"), ("JD补抓", "JD 补抓"), ("Codex语义复核", "语义复核"), ("生成报告", "报告"))):
            steps.columnconfigure(column, weight=1, uniform="steps")
            label = self._theme_widget(tk.Label(steps, text=f"{column+1:02d}  {short}", padx=4, pady=5, font=("Microsoft YaHei UI", 9)), background="neutral_soft", foreground="muted")
            label.grid(row=0, column=column, sticky="ew", padx=(0, 4 if column < 4 else 0))
            self._step_labels[name] = label
        line = ttk.Frame(progress, style="Card.TFrame")
        line.pack(fill="x", pady=(0, 7))
        ttk.Label(line, textvariable=self.stage_caption, style="Card.TLabel").pack(side="left")
        ttk.Label(line, textvariable=self.progress_text, style="Hint.TLabel").pack(side="right")
        self.progress_bar = ttk.Progressbar(progress, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x")

        metrics = ttk.Frame(right)
        metrics.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        metric_note_widgets = []
        for column, (key, title, color) in enumerate((("unique", "去重样本", "text"), ("jd", "JD 已获取", "text"), ("llm", "模型已复核", "text"), ("priority", "重点投递", "green"), ("pool", "海投池", "accent"), ("excluded", "暂不投", "muted"))):
            metrics.columnconfigure(column, weight=1, uniform="metrics")
            card = ttk.Frame(metrics, style="Card.TFrame", padding=(8, 7))
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 6 if column < 5 else 0))
            ttk.Label(card, text=title, style="Hint.TLabel").pack(anchor="w")
            self._theme_widget(tk.Label(card, textvariable=self._metric_values[key], font=("Segoe UI", 21, "bold")), foreground=color, background="surface").pack(anchor="w", pady=(2, 0))
            note = ttk.Label(card, textvariable=self._metric_notes[key], style="Hint.TLabel")
            note.pack(anchor="w")
            metric_note_widgets.append(note)
        self.live_label = ttk.Label(right, textvariable=self.live_detail, style="Muted.TLabel", wraplength=760)
        self.live_label.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        self.gate_banner = self._theme_widget(tk.Label(right, text="请在专用浏览器中人工完成登录或安全验证，完成后点击顶部“人工处理完成，继续”。", padx=12, pady=10, anchor="w", justify="left", wraplength=700), background="amber_soft", foreground="amber")
        self.gate_banner.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        self.gate_banner.grid_remove()

        logs = ttk.Frame(right, style="Card.TFrame", padding=12)
        logs.grid(row=4, column=0, sticky="nsew")
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(1, weight=1)
        log_header = ttk.Frame(logs, style="Card.TFrame")
        log_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 9))
        ttk.Label(log_header, text="实时日志", style="Section.TLabel").pack(side="left")
        self.summary_button = ttk.Button(log_header, text="任务摘要", style="Small.TButton", state="disabled", command=self._show_run_summary)
        self.summary_button.pack(side="left", padx=(9, 0))
        ttk.Button(log_header, text="清空显示", style="Small.TButton", command=self._clear_log_display).pack(side="right")
        ttk.Button(log_header, text="复制", style="Small.TButton", command=self._copy_log).pack(side="right", padx=6)
        ttk.Checkbutton(log_header, text="自动滚动", variable=self.auto_scroll).pack(side="right", padx=8)
        self.log_view = self._theme_text(tk.Text(logs, wrap="word", height=12, state="disabled", font=("Consolas", 10), relief="flat", padx=13, pady=12), log=True)
        self.log_view.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(logs, orient="vertical", command=self.log_view.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log_view.configure(yscrollcommand=scroll.set)
        self.run_summary.trace_add("write", lambda *_: self.summary_button.configure(state="normal" if self.run_summary.get() else "disabled"))
        def resize_monitor(event):
            for widget in (self.live_label, self.gate_banner):
                widget.configure(wraplength=max(180, event.width - 16))
            for note in metric_note_widgets:
                if self.root.winfo_height() < 800:
                    note.pack_forget()
                else:
                    note.pack(anchor="w")
        right.bind("<Configure>", resize_monitor)

    def _show_run_summary(self) -> None:
        if self.run_summary.get():
            messagebox.showinfo("本次任务摘要", self.run_summary.get(), parent=self.root)

    def _build_results_page(self, cities) -> None:
        page = self.results_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        top = ttk.Frame(page)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="岗位与证据", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Button(top, text="选择运行目录", command=self._choose_result_run).pack(side="right")
        ttk.Button(top, text="刷新结果", command=self._reload_results).pack(side="right", padx=(0, 8))
        ttk.Label(page, textvariable=self.result_location, style="Muted.TLabel", wraplength=1120).grid(row=1, column=0, sticky="w", pady=(7, 12))
        filters = ttk.Frame(page, style="Card.TFrame", padding=12)
        filters.grid(row=2, column=0, sticky="ew")
        filters.columnconfigure(0, weight=1)
        self.result_search_entry = ttk.Entry(filters, textvariable=self.result_search, width=28)
        self.result_search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        for column, title, variable, values in ((1, "分层", self.result_class, ("全部", "重点投递", "海投池", "暂不投")), (2, "城市", self.result_city, ("全部", *cities[1:])), (3, "JD", self.result_jd, ("全部", "已抓取", "待核实")), (4, "模型", self.result_model, ("全部", "成功", "失败", "未运行"))):
            cell = ttk.Frame(filters, style="Card.TFrame")
            cell.grid(row=0, column=column, padx=(0, 8))
            ttk.Label(cell, text=title, style="Hint.TLabel").pack(side="left", padx=(0, 5))
            ttk.Combobox(cell, textvariable=variable, values=values, state="readonly", width=9).pack(side="left")
        ttk.Button(filters, text="重置", style="Small.TButton", command=self._reset_result_filters).grid(row=0, column=5)
        info = ttk.Frame(page)
        info.grid(row=3, column=0, sticky="ew", pady=(8, 6))
        ttk.Label(info, textvariable=self.result_count, style="Muted.TLabel").pack(side="left")
        ttk.Label(info, textvariable=self.result_warning, style="Warning.TLabel", wraplength=700).pack(side="right")
        panes = ttk.Panedwindow(page, orient="vertical")
        panes.grid(row=4, column=0, sticky="nsew")
        table_frame = ttk.Frame(panes)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ("classification", "title", "company", "city", "salary", "dimensions", "jd", "model")
        self.results_table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended", height=9)
        definitions = (("classification", "分层", 100, "priority"), ("title", "岗位名称", 280, "title"), ("company", "公司", 165, "company"), ("city", "城市", 95, "city"), ("salary", "薪资", 130, "salary"), ("dimensions", "职责", 65, "dimensions"), ("jd", "JD", 85, None), ("model", "模型", 100, None))
        for name, title, width, sort in definitions:
            self.results_table.heading(name, text=title, command=(lambda key=sort: self._sort_results(key)) if sort else lambda: None)
            self.results_table.column(name, width=width, minwidth=55, stretch=name in ("title", "company"))
        self.results_table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.results_table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.results_table.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.results_table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.results_table.bind("<<TreeviewSelect>>", self._show_selected_job)
        panes.add(table_frame, weight=3)
        detail_frame = ttk.Frame(panes, style="Card.TFrame", padding=(12, 10))
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)
        detail_head = ttk.Frame(detail_frame, style="Card.TFrame")
        detail_head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(detail_head, text="岗位详情 / JD 原文 / 判断依据", style="Section.TLabel").pack(side="left")
        ttk.Button(detail_head, text="打开岗位", style="Small.TButton", command=self._open_selected_job).pack(side="right")
        ttk.Button(detail_head, text="复制选中链接", style="Small.TButton", command=self._copy_selected_links).pack(side="right", padx=6)
        self.detail_text = self._theme_text(tk.Text(detail_frame, wrap="word", height=9, relief="flat", state="disabled", font=("Microsoft YaHei UI", 10), padx=6, pady=6))
        self.detail_text.grid(row=1, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail_text.yview)
        detail_scroll.grid(row=1, column=1, sticky="ns")
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self._set_detail("选择一个岗位，查看完整 JD、六项职责证据、模型判断及需核实事项。\n\n重点投递是程序分层，不代表已经通过学历、校招资格与能力核验。")
        panes.add(detail_frame, weight=2)
        actions = ttk.Frame(page)
        actions.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="导出当前列表…", style="Primary.TButton", command=self._export_filtered_jobs).pack(side="left")
        ttk.Button(actions, text="打开 Markdown 报告", command=self._open_report).pack(side="left", padx=8)
        ttk.Button(actions, text="打开运行 JSON", command=self._open_run_json).pack(side="left")
        self.next_page_button = ttk.Button(actions, text="下一页", style="Small.TButton", command=lambda: self._change_result_page(1))
        self.next_page_button.pack(side="right")
        ttk.Label(actions, textvariable=self.result_page, style="Muted.TLabel").pack(side="right", padx=10)
        self.previous_page_button = ttk.Button(actions, text="上一页", style="Small.TButton", command=lambda: self._change_result_page(-1))
        self.previous_page_button.pack(side="right")

    def _build_history_page(self) -> None:
        page = self.history_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        header = ttk.Frame(page)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="运行记录", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Button(header, text="刷新历史", command=self._refresh_history).pack(side="right")
        ttk.Label(page, textvariable=self.history_hint, style="Muted.TLabel", wraplength=1120).grid(row=1, column=0, sticky="ew", pady=(8, 14))
        table = ttk.Frame(page)
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.history_table = ttk.Treeview(table, columns=("started", "city", "size", "status", "priority", "pool", "excluded", "name"), show="headings", selectmode="browse")
        for name, title, width in (("started", "开始时间", 165), ("city", "城市", 75), ("size", "去重岗位", 85), ("status", "状态", 130), ("priority", "重点", 65), ("pool", "海投池", 70), ("excluded", "暂不投", 70), ("name", "运行目录", 350)):
            self.history_table.heading(name, text=title)
            self.history_table.column(name, width=width, minwidth=55, stretch=name == "name")
        self.history_table.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.history_table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.history_table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.history_table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.history_table.bind("<Double-1>", lambda _: self._view_history_results())
        actions = ttk.Frame(page)
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="查看岗位结果", style="Primary.TButton", command=self._view_history_results).pack(side="left")
        self.resume_history_button = ttk.Button(actions, text="载入续跑设置", command=self._resume_history_run)
        self.resume_history_button.pack(side="left", padx=8)
        ttk.Button(actions, text="打开目录", command=self._open_history_directory).pack(side="left")
        ttk.Button(actions, text="查看日志", command=self._open_history_log).pack(side="left", padx=8)

    def _scroll_settings(self, event):
        try:
            target = self.root.winfo_containing(event.x_root, event.y_root)
            if isinstance(target, tk.Text):
                return None
            if target and str(target).startswith(str(self.settings_body)):
                self.settings_canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
        except tk.TclError:
            return None

    def _update_stage_hint(self) -> None:
        hints = {
            "完整流程（采集+后处理）": "先采集，再合并、补 JD、模型复核和生成报告。",
            "仅采集": "写入采集根目录；已成功组合自动跳过。",
            "列表前处理": "读取已有列表，去重并均衡抽样；不请求 BOSS 或模型。",
            "JD补抓": "只补抓候选岗位正文，遇验证等待人工操作。",
            "Codex语义复核": "复核已有 JD，成功检查点自动复用。",
            "生成报告": "用已有 JD 与模型结果生成三层清单，不调用模型。",
            "后处理全流程（已有数据）": "不重新采集列表；从已有数据开始处理。",
        }
        self.stage_hint.set(hints.get(self.stage.get(), "请选择运行阶段。"))

    def _debounce_filters(self) -> None:
        if self._search_after_id:
            self.root.after_cancel(self._search_after_id)
        self._search_after_id = self.root.after(220, self._apply_result_filters)

    def _apply_result_filters(self) -> None:
        if not getattr(self, "_workspace_ready", False):
            return
        self._search_after_id = None
        self._filter_request_id += 1
        request_id = self._filter_request_id
        jobs = self._result_jobs
        options = dict(search=self.result_search.get(), classification=self.result_class.get(), city=self.result_city.get(), jd_status=self.result_jd.get(), model_status={"成功": "success", "失败": "failed", "未运行": "not_run"}.get(self.result_model.get(), "全部"), sort_by=self._sort_by, reverse=self._sort_reverse)
        def work():
            try:
                result = desktop_data.filter_jobs(jobs, **options)
                self.events.put(("result_filter", (request_id, result)))
            except Exception as error:
                self.events.put(("result_filter_error", (request_id, type(error).__name__)))
        threading.Thread(target=work, name="desktop-result-filter", daemon=True).start()

    def _sort_results(self, key: str) -> None:
        self._sort_reverse = not self._sort_reverse if self._sort_by == key else False
        self._sort_by = key
        self._apply_result_filters()

    def _reset_result_filters(self) -> None:
        self.result_search.set("")
        for variable in (self.result_class, self.result_city, self.result_jd, self.result_model):
            variable.set("全部")
        self._sort_by = "priority"
        self._sort_reverse = False
        self._apply_result_filters()

    def _render_results(self) -> None:
        self.results_table.delete(*self.results_table.get_children())
        self._shown_jobs.clear()
        start = self._filter_page * 100
        subset = self._filtered_jobs[start:start+100]
        for number, job in enumerate(subset):
            iid = f"job-{number}"
            self._shown_jobs[iid] = job
            status = job.get("model_status") or (job.get("llm_review") or {}).get("status", "not_run")
            values = (job.get("classification", "待处理"), job.get("title") or job.get("job_name") or "未提供", job.get("company") or "未知（待核实）", job.get("city") or job.get("location") or "未提供", job.get("salary") or "未提供", f'{job.get("jd_dimension_count", 0)}/6', job.get("jd_status") or ("已抓取" if job.get("jd") else "待核实"), {"success": "已复核", "failed": "失败 / 回退", "not_run": "未运行"}.get(status, str(status)))
            self.results_table.insert("", "end", iid=iid, values=values, tags=(job.get("classification", ""),))
        total = len(self._filtered_jobs)
        page_total = max(1, (total + 99) // 100)
        self.result_count.set(f"筛选 {total:,} / 已载入 {len(self._result_jobs):,} 岗  ·  搜索岗位 / 公司 / 技能；点击表头排序")
        self.result_page.set(f"{self._filter_page+1} / {page_total} 页")
        self.previous_page_button.configure(state="normal" if self._filter_page else "disabled")
        self.next_page_button.configure(state="normal" if self._filter_page+1 < page_total else "disabled")
        if subset:
            self.results_table.selection_set("job-0")
            self.results_table.focus("job-0")
            self._show_selected_job()
        else:
            self._set_detail("没有符合当前筛选条件的岗位。可重置筛选，或选择其他运行目录。")

    def _change_result_page(self, amount: int) -> None:
        pages = max(1, (len(self._filtered_jobs) + 99) // 100)
        self._filter_page = max(0, min(pages - 1, self._filter_page + amount))
        self._render_results()

    def _set_detail(self, value: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", value)
        self.detail_text.configure(state="disabled")

    def _selected_jobs(self) -> list[dict]:
        return [self._shown_jobs[iid] for iid in self.results_table.selection() if iid in self._shown_jobs]

    def _show_selected_job(self, _event=None) -> None:
        selected = self._selected_jobs()
        if selected:
            self._set_detail(desktop_data.format_job_detail(selected[0]))

    def _open_selected_job(self) -> None:
        jobs = self._selected_jobs()
        if not jobs:
            return
        link = str(jobs[0].get("job_link") or "")
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not (host == "zhipin.com" or host.endswith(".zhipin.com")):
            messagebox.showinfo("岗位链接", "没有有效的 BOSS 岗位链接。", parent=self.root)
            return
        webbrowser.open(link)

    def _copy_selected_links(self) -> None:
        links = [str(job.get("job_link")) for job in self._selected_jobs() if job.get("job_link")]
        if links:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(dict.fromkeys(links)))
            self.result_count.set(f"已复制 {len(set(links))} 个岗位链接；未投递或联系招聘方。")

    def _export_filtered_jobs(self) -> None:
        if not self._filtered_jobs:
            messagebox.showinfo("导出", "当前没有可导出的岗位。", parent=self.root)
            return
        chosen = filedialog.asksaveasfilename(parent=self.root, title="导出当前筛选列表", defaultextension=".json", initialfile="岗位筛选结果.json", filetypes=(("JSON（含证据）", "*.json"), ("CSV（Excel 可打开）", "*.csv")))
        if not chosen:
            return
        path = Path(chosen)
        # An export must never replace a pipeline checkpoint, even after a
        # user accidentally chooses the source file in a save dialog.
        reserved = {"merged_jobs_index.json", "hai_tou_pool.json", "llm_reviews.json", "llm_manifest.json", "details.json", "jd_input.json", "run_manifest.json", "raw_sample.json", "collection_manifest.json", "details_manifest.json", "current_candidates.json", "target_candidates.json"}
        if path.name.casefold() in reserved:
            messagebox.showerror("请使用新的文件名", "导出文件不能覆盖采集结果、报告数据或检查点。", parent=self.root)
            return
        destination = path.expanduser().resolve()
        source_roots = [Path(self.data_root.get()).expanduser().resolve()]
        if self._result_source_root:
            source_roots.append(self._result_source_root.resolve())
        if any(destination.is_relative_to((base / directory).resolve()) for base in source_roots for directory in ("raw", "checkpoints")):
            messagebox.showerror("请另选导出目录", "不能导出到原始采集列表或采集检查点目录；请另存为新的 JSON / CSV 文件。", parent=self.root)
            return
        jobs = list(self._filtered_jobs)
        self.result_count.set(f"正在导出 {len(jobs)} 个岗位…")
        def work():
            try:
                desktop_data.export_jobs(path, jobs)
                self.events.put(("export_done", (str(path), len(jobs), None)))
            except Exception as error:
                self.events.put(("export_done", (str(path), 0, type(error).__name__)))
        threading.Thread(target=work, name="desktop-result-export", daemon=True).start()

    def _choose_result_run(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, title="选择包含 prepared 或运行清单的目录", initialdir=str(Path(self.data_root.get()) / "runs"))
        if chosen:
            self._load_result_run(Path(chosen))

    def _reload_results(self) -> None:
        path = self._viewed_run or self.active_run
        if path:
            self._load_result_run(path)
        else:
            self._refresh_history()

    def _load_result_run(self, path: Path) -> None:
        self._results_request_id += 1
        request_id = self._results_request_id
        # Invalidate an older run's in-flight filters before changing the path.
        # Until loading completes, exports/details must not refer to old jobs.
        self._filter_request_id += 1
        if self._search_after_id:
            self.root.after_cancel(self._search_after_id)
            self._search_after_id = None
        self._result_jobs = []
        self._result_source_root = None
        self._filtered_jobs = []
        self._filter_page = 0
        self._render_results()
        self._set_detail("正在载入所选运行的岗位和证据…")
        self._viewed_run = Path(path)
        self.result_location.set(str(path))
        self.result_count.set("正在后台读取岗位、证据与报告…")
        self.result_warning.set("")
        def work():
            try:
                payload = desktop_data.load_run_results(Path(path))
                self.events.put(("results_loaded", (request_id, payload)))
            except Exception as error:
                self.events.put(("results_load_error", (request_id, type(error).__name__)))
        threading.Thread(target=work, name="desktop-results-load", daemon=True).start()

    def _refresh_workspace_history(self) -> None:
        self._history_request_id += 1
        request_id = self._history_request_id
        root = Path(self.data_root.get()).expanduser()
        def work():
            try:
                daily_root, _warnings = resolve_daily_root(root)
                rows = desktop_data.list_run_history(daily_root)
                self.events.put(("history_loaded", (request_id, rows)))
            except Exception as error:
                self.events.put(("history_error", (request_id, type(error).__name__)))
        threading.Thread(target=work, name="desktop-history-load", daemon=True).start()

    def _selected_history(self) -> dict | None:
        selected = self.history_table.selection()
        if not selected:
            messagebox.showinfo("运行历史", "请先选择一条运行记录。", parent=self.root)
            return None
        index = int(selected[0].split("-", 1)[1])
        return self._history_rows[index] if index < len(self._history_rows) else None

    def _view_history_results(self) -> None:
        row = self._selected_history()
        if row:
            self._load_result_run(Path(row["path"]))
            self.notebook.select(self.results_page)

    def _open_history_directory(self) -> None:
        row = self._selected_history()
        if row:
            self._open_path(Path(row["path"]))

    def _open_history_log(self) -> None:
        row = self._selected_history()
        if row:
            path = Path(row["path"]) / "desktop.log"
            if path.is_file():
                self._open_path(path)
            else:
                messagebox.showinfo("运行日志", "这次运行未生成桌面日志；可打开运行目录查看分阶段日志。", parent=self.root)

    def _resume_history_run(self) -> None:
        if self.workflow_running:
            return
        row = self._selected_history()
        if not row:
            return
        path = Path(row["path"])
        try:
            manifest = json.loads((path / "run_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            messagebox.showerror("无法续跑", "运行清单缺失或无法解析；请新建运行，不要覆盖此目录。", parent=self.root)
            return
        if not isinstance(manifest, dict):
            return
        if manifest.get("city") in ("全部城市", "上海", "深圳", "杭州", "广州", "成都", "北京"):
            self.city.set(manifest["city"])
        if manifest.get("track") in ("当前+成长", "当前岗位", "成长目标"):
            self.track.set(manifest["track"])
        for field, key in (("pages", "pages_per_combination"), ("sample_size", "sample_size"), ("combination_limit", "combination_limit"), ("batch_size", "batch_size"), ("workers", "workers")):
            if key in manifest:
                getattr(self, field).set("" if manifest[key] is None else str(manifest[key]))
        labels = ("列表前处理", "JD补抓", "Codex语义复核", "生成报告")
        stages = manifest.get("stages") or {}
        next_stage = next((name for name in labels if isinstance(stages.get(name), dict) and stages[name].get("status") not in ("success", "completed")), "生成报告")
        original = manifest.get("stage_selection")
        self.stage.set(original if original in ("完整流程（采集+后处理）", "后处理全流程（已有数据）") else next_stage)
        self.resume_run.set(str(path))
        self.active_run = path
        self._collection_only = False
        self.current_log = path / "desktop.log"
        self.output_directory.set(f"已载入续跑目录：{path}")
        self.notebook.select(self.task_page)
        self.status_text.set("续跑设置已载入；点击“开始运行”后执行")
        self._refresh_stats()

    def _live_snapshot_target(self) -> Path | None:
        # Task metrics belong to the active task, independently of the run
        # browsed on the results page. Collection has no prepared snapshot.
        if self.active_run:
            return None if getattr(self, "_collection_only", False) else self.active_run
        return self._viewed_run

    def _refresh_live(self) -> None:
        if self._closing or self._destroyed():
            return
        target = self._live_snapshot_target()
        if target and not self._snapshot_busy:
            self._snapshot_busy = True
            path = Path(target)
            collection_root = Path(self.data_root.get()).expanduser()
            def work():
                try:
                    value = desktop_data.read_live_snapshot(path, collection_root=collection_root)
                    self.events.put(("live_snapshot", (str(path), value)))
                except Exception as error:
                    self.events.put(("live_snapshot_error", (str(path), type(error).__name__)))
            threading.Thread(target=work, name="desktop-live-snapshot", daemon=True).start()
        self._live_after_id = self.root.after(1800 if self.workflow_running else 5000, self._refresh_live)

    def _apply_snapshot(self, snapshot: dict) -> None:
        classes = snapshot.get("classification_counts") or {}
        coverage = snapshot.get("classification_status") or ("complete" if classes else "unavailable")
        mapping = {"unique": snapshot.get("unique_job_count"), "jd": snapshot.get("jd_success"), "llm": snapshot.get("llm_success"), "priority": classes.get("重点投递"), "pool": classes.get("海投池"), "excluded": classes.get("暂不投")}
        for key, value in mapping.items():
            self._metric_values[key].set(format_count(value))
        self._metric_notes["unique"].set("当前任务" if self.active_run else "所选历史运行")
        self._metric_notes["jd"].set(f'/ {format_count(snapshot.get("jd_total"))} 候选')
        self._metric_notes["llm"].set(f'/ {format_count(snapshot.get("llm_total"))} 份 JD')
        for key in ("priority", "pool", "excluded"):
            self._metric_notes[key].set({"complete": "JSON 当前分层", "partial": "仅已分层部分", "unclassified": "尚未分层"}.get(coverage, "等待分层数据"))
        pending = f' · 未分层 {format_count(snapshot.get("unclassified_count"))} 岗' if coverage in ("partial", "unclassified") else " · 分层待生成" if coverage == "unavailable" else ""
        self.live_detail.set(f'JD {format_count(snapshot.get("jd_success"))}/{format_count(snapshot.get("jd_total"))} · 缺失 {format_count(snapshot.get("jd_missing"))} · 暂停 {format_count(snapshot.get("jd_paused"))} · 失败 {format_count(snapshot.get("jd_failed"))}\n模型 {format_count(snapshot.get("llm_success"))}/{format_count(snapshot.get("llm_total"))} · 失败 {format_count(snapshot.get("llm_failed"))} · 整岗回退 {format_count(snapshot.get("llm_fallback"))}{pending}')
        self.updated_text.set(f'检查点 {datetime.now().strftime("%H:%M:%S")}')
        for name, widget in self._step_labels.items():
            value = (snapshot.get("stage_statuses") or {}).get(name, "pending")
            state = value.get("status", "pending") if isinstance(value, dict) else value
            color, text = ("accent_soft", "accent") if state == "running" else ("green_soft", "green") if state in ("success", "completed") else ("amber_soft", "amber") if state in ("partial", "paused", "stale") else ("red_soft", "red") if state in ("failed", "stopped_or_failed") else ("neutral_soft", "muted")
            self._theme_widget(widget, background=color, foreground=text)
        stage = snapshot.get("stage")
        if stage:
            self.stage_caption.set(str(stage))
        if self.workflow_running and stage == "Codex语义复核":
            success, failed, total = snapshot.get("llm_success"), snapshot.get("llm_failed"), snapshot.get("llm_total")
            if isinstance(success, int) and isinstance(total, int) and total:
                self._set_progress(f"{success + (failed or 0)}/{total}")
        elif self.workflow_running and stage == "JD补抓":
            success, total = snapshot.get("jd_success"), snapshot.get("jd_total")
            if isinstance(success, int) and isinstance(total, int) and total:
                self._set_progress(f"{success}/{total}")

    def _set_progress(self, value: str) -> None:
        fraction = progress_fraction(value)
        if fraction:
            done, total = fraction
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", value=100*done/total)
            self.progress_text.set(f"{done:,} / {total:,}  ·  {100*done/total:.0f}%")
        elif self.workflow_running and not self.waiting_for_human:
            if self.progress_bar.cget("mode") != "indeterminate":
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start(16)

    def _workspace_event(self, kind: str, value) -> None:
        if kind == "progress":
            self._set_progress(value)
        elif kind == "status":
            self.stage_caption.set(value.removeprefix("正在运行："))
            stage = value.removeprefix("正在运行：")
            if stage in self._step_labels:
                self._theme_widget(self._step_labels[stage], background="accent_soft", foreground="accent")
        elif kind == "human_wait":
            if value:
                self.live_label.grid_remove()
                self.gate_banner.grid()
                self.progress_bar.stop()
                self._theme_widget(self.status_badge, background="amber_soft", foreground="amber")
            else:
                self.gate_banner.grid_remove()
                self.live_label.grid()
                if self.workflow_running:
                    self._theme_widget(self.status_badge, background="accent_soft", foreground="accent")
        elif kind == "live_snapshot":
            self._snapshot_busy = False
            path, snapshot = value
            current = self._live_snapshot_target()
            if current and str(current) == path:
                self._apply_snapshot(snapshot)
        elif kind == "live_snapshot_error":
            self._snapshot_busy = False
        elif kind == "results_loaded":
            request_id, payload = value
            if request_id == self._results_request_id:
                self._result_jobs = payload.get("jobs", [])
                source_root = (payload.get("manifest") or {}).get("source_root")
                self._result_source_root = Path(source_root) if isinstance(source_root, str) and source_root else None
                warnings = list(payload.get("warnings") or [])
                report_stage = ((payload.get("manifest") or {}).get("stages") or {}).get("生成报告") or {}
                if isinstance(report_stage, dict) and report_stage.get("status") == "stale":
                    warnings.insert(0, "上游数据已更新；当前分层是旧报告，请重新生成报告。")
                self.result_warning.set("；".join(str(x) for x in warnings[:2]))
                self._apply_result_filters()
        elif kind == "results_load_error":
            request_id, error = value
            if request_id == self._results_request_id:
                self._result_jobs = []
                self.result_warning.set(f"结果读取失败：{error}；原文件未修改。")
                self._apply_result_filters()
        elif kind == "result_filter":
            request_id, jobs = value
            if request_id == self._filter_request_id:
                self._filtered_jobs = jobs
                self._filter_page = 0
                self._render_results()
        elif kind == "result_filter_error":
            request_id, error = value
            if request_id == self._filter_request_id:
                self.result_warning.set(f"筛选失败：{error}，可重置后重试。")
        elif kind == "history_loaded":
            request_id, rows = value
            if request_id != self._history_request_id:
                return
            selection = self.history_table.selection()
            selected_path = None
            if selection:
                index = int(selection[0].split("-", 1)[1])
                if index < len(self._history_rows):
                    selected_path = self._history_rows[index].get("path")
            self._history_rows = rows
            current_choice = self.resume_run.get() or AUTO_RUN_CHOICE
            # A refresh / browsing another day must not silently replace an
            # explicit task source, even if that path is absent from this list.
            self.history_values = list(dict.fromkeys((AUTO_RUN_CHOICE, NEW_RUN_CHOICE, *(row["path"] for row in rows), current_choice)))
            self.history_box.configure(values=self.history_values)
            if not self.resume_run.get():
                self.resume_run.set(AUTO_RUN_CHOICE)
            self.history_table.delete(*self.history_table.get_children())
            for number, row in enumerate(rows):
                counts = row.get("classification_counts") or {}
                coverage_note = {"partial": " · 部分分层", "unclassified": " · 未分层"}.get(row.get("classification_status"), "")
                values = (str(row.get("started_at") or "未记录").replace("T", " ")[:19], row.get("city") or "未记录", format_count(row.get("unique_job_count")), STATUS_LABELS.get(row.get("status"), row.get("status") or "未记录") + coverage_note, format_count(counts.get("重点投递")), format_count(counts.get("海投池")), format_count(counts.get("暂不投")), row.get("name"))
                self.history_table.insert("", "end", iid=f"history-{number}", values=values)
                if selected_path and row.get("path") == selected_path:
                    self.history_table.selection_set(f"history-{number}")
            self.history_hint.set(f"共 {len(rows)} 次运行。查看不修改旧数据；续跑需载入设置，再点击开始。")
            if self._viewed_run is None and not self.workflow_running:
                latest = next((row for row in rows if row.get("unique_job_count")), None)
                if latest:
                    self._load_result_run(Path(latest["path"]))
        elif kind == "history_error":
            request_id, error = value
            if request_id == self._history_request_id:
                self.history_hint.set(f"历史读取失败：{error}；可选择运行目录直接查看。")
        elif kind == "export_done":
            path, count, error = value
            if error:
                messagebox.showerror("导出失败", error, parent=self.root)
            else:
                self.result_count.set(f"已导出 {count} 岗：{path}")
        elif kind == "finished":
            self.progress_bar.stop()
            if value.get("exit_code") == 0:
                self.progress_bar.configure(mode="determinate", value=100)
            status = value.get("manifest", {}).get("status")
            color = "amber" if status in ("paused", "partial") else "green" if value.get("exit_code") == 0 else "red"
            self._theme_widget(self.status_badge, background=f"{color}_soft", foreground=color)
            if self.run_summary.get():
                self._record_log(f"\n[任务摘要] {self.run_summary.get()}\n")
            if getattr(self, "_collection_only", False):
                self._apply_snapshot({"stage": "岗位采集", "stage_statuses": {name: entry.get("status") for name, entry in value.get("manifest", {}).get("stages", {}).items() if isinstance(entry, dict)}})
                self.live_detail.set("本次仅采集原始列表；请运行“列表前处理”创建新的去重样本，再补 JD、复核与生成报告。")
            else:
                self._load_result_run(Path(value["output"]))
        elif kind == "browser_ready":
            self.progress_bar.stop()
            self.gate_banner.grid_remove()

    def _on_workspace_start(self, stage: str) -> None:
        self._theme_widget(self.status_badge, background="accent_soft", foreground="accent")
        self.stage_caption.set(stage)
        self.gate_banner.grid_remove()
        self.live_label.grid()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(16)
        self.run_summary.set("")
        self.prompt_button_text.set("人工处理完成，继续")
        self.continue_button.configure(state="disabled")
        for variable in self._metric_values.values():
            variable.set("—")
        for variable in self._metric_notes.values():
            variable.set("等待本次数据")
        for widget in self._step_labels.values():
            self._theme_widget(widget, background="neutral_soft", foreground="muted")
        self.updated_text.set("等待本次检查点")
        self.live_detail.set("本次仅采集列表；岗位分层将在后处理后显示。" if getattr(self, "_collection_only", False) else "正在准备本次运行的检查点数据。")
        self.notebook.select(self.task_page)

    def _pause_workflow(self) -> None:
        if self.workflow_running:
            self._stop(pause=True)
            self.status_text.set("正在暂停并保存检查点；停止后可继续此任务…")

    def _clear_log_display(self) -> None:
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")

    def _copy_log(self) -> None:
        value = self.log_view.get("1.0", "end-1c")
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)

    def _focus_result_search(self, _event=None):
        self.notebook.select(self.results_page)
        self.result_search_entry.focus_set()
        return "break"

    def _refresh_current_view(self) -> None:
        selected = self.notebook.select()
        if selected == str(self.results_page):
            self._reload_results()
        elif selected == str(self.history_page):
            self._refresh_history()
        else:
            self._refresh_stats()

    def _artifact_run(self) -> Path | None:
        if getattr(self, "_workspace_ready", False):
            selected_page = self.notebook.select()
            if selected_page == str(self.history_page):
                selected = self.history_table.selection()
                if selected:
                    index = int(selected[0].split("-", 1)[1])
                    if index < len(self._history_rows):
                        return Path(self._history_rows[index]["path"])
            if selected_page in (str(self.results_page), str(self.history_page)):
                return self._viewed_run or self.active_run
        return self.active_run or getattr(self, "_viewed_run", None)

    def _save_workspace_preferences(self) -> None:
        try:
            save_preferences({name: getattr(self, name).get() for name in PREFERENCE_FIELDS if hasattr(self, name)})
        except OSError:
            pass

    def _close_workspace(self) -> None:
        self._save_workspace_preferences()
        self.progress_bar.stop()
        if getattr(self, "_routing_summary_trace_id", None):
            self.routing_summary.trace_remove("write", self._routing_summary_trace_id)
            self._routing_summary_trace_id = None
        for attribute in ("_live_after_id", "_search_after_id", "_route_after_id"):
            after_id = getattr(self, attribute, None)
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
