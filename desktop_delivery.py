"""Independent, explicit delivery export workspace; filesystem work stays off Tk."""

from __future__ import annotations

import json
from pathlib import Path
import queue
import stat
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Callable

import delivery_export
import desktop_data
from desktop_tasks import ReviewTask, TaskStateError
from desktop_theme import LIGHT_PALETTE, configure_combobox_popdown
from desktop_tk_lifecycle import StringVar, install_tk_lifecycle


PAGE_SIZE = 20
POLL_MS = 75
BLOCK_REASON_LABELS = {
    "missing_boss_identifier": "缺少 BOSS 岗位链接或真实岗位 ID",
    "invalid_boss_identifier": "BOSS 岗位 ID 无效",
    "invalid_job_url": "岗位链接无效或不是 BOSS 详情页",
    "conflicting_identifiers": "岗位链接或 ID 相互冲突",
    "ai_review_invalid": "AI 复核记录不可验证，请核查后重新确认",
    "duplicate_target": "多个保留项指向同一 BOSS 岗位",
}


def _safe(value: Any) -> str:
    return desktop_data._redact_text(str(value))


def _validated_export_path(path: Path, parent: Path, *, directory: bool = False) -> Path:
    """Open only the artifact returned by our exporter inside its chosen parent."""
    path, parent = Path(path).absolute(), Path(parent).absolute()
    try:
        for component in (path, *path.parents):
            info = component.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise ValueError()
        if not path.resolve().is_relative_to(parent.resolve()) or path.name != "delivery_jobs.json" or not path.is_file():
            raise ValueError()
    except (OSError, ValueError, RuntimeError):
        raise TaskStateError("导出文件缺失或路径不安全；请重新导出。") from None
    return path.parent if directory else path


class DeliveryPage(ttk.Frame):
    """One preview source, independent of the review page's task binding."""

    def __init__(self, parent: tk.Misc, *, can_start: Callable[[], bool],
                 on_busy_change: Callable[[bool], None], open_path: Callable[[Path], None]):
        super().__init__(parent)
        install_tk_lifecycle(self)
        self.can_start, self.on_busy_change, self.open_path = can_start, on_busy_change, open_path
        self._busy = self._closing = self._closed = False
        self._worker: threading.Thread | None = None
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._after_id: str | None = None
        self._cache = delivery_export.ExportCache()
        self._source_reader: ReviewTask | None = None  # Serial worker owns this reader and its projection cache.
        self._current_path: Path | None = None
        self._current_revision: int | None = None
        self._source_path: Path | None = None
        self._source_version = 0
        self._manual = False
        self._preview: dict[str, Any] | None = None
        self._prepared_rows: list[tuple] = []
        self._display_cache: tuple | None = None  # Serial worker owns parsing/search preparation.
        self._preview_current = False
        self._latest_export: Path | None = None
        self._latest_snapshot: str | None = None
        self._latest_parent: Path | None = None
        self._latest_current = False
        self._page = 0
        self._focus: str | None = None
        self._rows: dict[str, dict[str, Any]] = {}
        self.palette = dict(LIGHT_PALETTE)
        self.source_text = StringVar(self, "来源：02 当前任务（尚未绑定）")
        self.output_parent = StringVar(self, "")
        self.output_hint = StringVar(self, "默认保存到来源任务 / delivery-exports，每次导出保留独立历史。")
        self.status_text = StringVar(self, "尚未预览；只接受四步均完成且已确认的最终保留名单。")
        self.summary_text = StringVar(self, "最终保留 — · 可导出 — · 阻止导出 —")
        self.latest_text = StringVar(self, "尚未导出")
        self.search = StringVar(self, "")
        self.filter = StringVar(self, "全部")
        self.page_text = StringVar(self, "0 个岗位")
        self._build_ui()
        self.search.trace_add("write", self._filter_changed)
        self.filter.trace_add("write", self._filter_changed)
        self.bind("<Destroy>", self._destroyed, add="+")
        self.apply_theme(self.palette)
        self.render_controls()
        self._schedule_poll()

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def shutdown_complete(self) -> bool:
        return self._worker is None or not self._worker.is_alive()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        ttk.Label(self, text="投递数据导出", style="Review.Title.TLabel").grid(row=0, column=0, sticky="w")
        source = ttk.Frame(self)
        source.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        source.columnconfigure(0, weight=1)
        ttk.Label(source, textvariable=self.source_text, wraplength=800).grid(row=0, column=0, sticky="w")
        actions = ttk.Frame(source)
        actions.grid(row=1, column=0, sticky="w", pady=5)
        self.current_button = ttk.Button(actions, text="使用 02 当前任务", command=self.use_current_task)
        self.current_button.pack(side="left", padx=(0, 7))
        self.open_task_button = ttk.Button(actions, text="打开其他任务…", command=self.choose_task)
        self.open_task_button.pack(side="left", padx=(0, 7))
        self.refresh_button = ttk.Button(actions, text="刷新预览", command=self.refresh_preview)
        self.refresh_button.pack(side="left")
        ttk.Label(self, textvariable=self.status_text, style="Muted.TLabel", wraplength=1100).grid(row=2, column=0, sticky="ew", pady=(0, 5))
        toolbar = ttk.Frame(self)
        toolbar.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(toolbar, textvariable=self.summary_text, style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search, width=24)
        self.search_entry.grid(row=0, column=1, padx=8)
        self.filter_box = ttk.Combobox(toolbar, textvariable=self.filter, values=("全部", "可导出", "阻止导出"), width=10, state="readonly")
        self.filter_box.grid(row=0, column=2)
        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=4, column=0, sticky="nsew")
        table_frame = ttk.Frame(body)
        body.add(table_frame, weight=3)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ("state", "title", "company", "location", "salary", "reason")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", style="Review.Treeview")
        for name, title, width in zip(columns, ("状态", "岗位", "公司", "地点", "薪资", "阻止原因"), (80, 210, 130, 100, 95, 260)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=65, stretch=True)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.bind("<<TreeviewSelect>>", self._show_detail)
        paging = ttk.Frame(table_frame)
        paging.grid(row=2, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Label(paging, textvariable=self.page_text).pack(side="left")
        self.next_button = ttk.Button(paging, text="下一页", command=lambda: self.change_page(1))
        self.next_button.pack(side="right")
        self.previous_button = ttk.Button(paging, text="上一页", command=lambda: self.change_page(-1))
        self.previous_button.pack(side="right", padx=7)
        self.detail = tk.Text(body, state="disabled", wrap="word", relief="flat", width=32, padx=10, pady=8, exportselection=False)
        body.add(self.detail, weight=1)
        output = ttk.Frame(self)
        output.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="导出父目录").grid(row=0, column=0, padx=(0, 8))
        self.output_entry = ttk.Entry(output, textvariable=self.output_parent, state="readonly")
        self.output_entry.grid(row=0, column=1, sticky="ew")
        self.choose_parent_button = ttk.Button(output, text="选择…", command=self.choose_parent)
        self.choose_parent_button.grid(row=0, column=2, padx=6)
        self.default_parent_button = ttk.Button(output, text="使用默认", command=lambda: self.output_parent.set(""))
        self.default_parent_button.grid(row=0, column=3)
        ttk.Label(output, textvariable=self.output_hint, style="Muted.TLabel", wraplength=1000).grid(row=1, column=0, columnspan=4, sticky="w", pady=4)
        footer = ttk.Frame(self)
        footer.grid(row=6, column=0, sticky="ew", pady=(2, 4))
        self.export_button = ttk.Button(footer, text="导出投递 JSON", style="Review.Primary.TButton", command=self.export)
        self.export_button.pack(side="left")
        self.open_json_button = ttk.Button(footer, text="打开 JSON", command=lambda: self.open_latest(directory=False))
        self.open_json_button.pack(side="left", padx=8)
        self.open_directory_button = ttk.Button(footer, text="打开输出目录", command=lambda: self.open_latest(directory=True))
        self.open_directory_button.pack(side="left")
        ttk.Label(self, textvariable=self.latest_text, style="Muted.TLabel", wraplength=1100).grid(row=7, column=0, sticky="ew")

    def set_current_task(self, path: Path | None, revision: int | None = None) -> None:
        """Receive UI-owned binding metadata, without opening or exporting it."""
        changed = self._current_path != path
        revised = self._current_revision != revision
        self._current_path, self._current_revision = path, revision
        if not self._manual:
            if changed:
                self._set_source(path, manual=False)
            elif revised:
                self._invalidate("02 任务已更新，请刷新预览后再导出。")
        self.render_controls()

    def _set_source(self, path: Path | None, *, manual: bool) -> None:
        changed = self._source_path != path
        self._source_path, self._manual = path, manual
        self._source_version += 1
        if changed:
            self._preview = None
            self._prepared_rows = []
            self._latest_export = self._latest_snapshot = self._latest_parent = None
            self._page, self._focus = 0, None
            self.latest_text.set("尚未导出")
            self.summary_text.set("最终保留 — · 可导出 — · 阻止导出 —")
        self.source_text.set(("来源：手动任务（不影响 02 绑定）\n" if manual else "来源：02 当前任务\n") + (str(path) if path else "尚未绑定任务"))
        self._invalidate("请刷新预览；四步均须完成且已确认。" if path else "02 尚未绑定任务，也可显式打开其他任务。")
        self._render_rows()

    def _invalidate(self, message: str) -> None:
        # An in-flight worker must not re-enable stale controls after the
        # review page reports a newer revision of the same source task.
        self._source_version += 1
        self._preview_current = self._latest_current = False
        self.status_text.set(message)
        if self._preview is not None:
            summary = self._preview["summary"]
            self.summary_text.set(f"旧预览（请刷新） · 最终保留 {summary['selected_count']} · 可导出 {summary['ready_count']} · 阻止导出 {summary['blocked_count']}")
        if self._latest_export is not None:
            self.latest_text.set("历史导出（需刷新核对当前名单）：" + str(self._latest_export))

    def use_current_task(self) -> None:
        if self._busy or self._closing:
            return
        self._set_source(self._current_path, manual=False)
        self.render_controls()
        self.refresh_preview()

    def choose_task(self) -> None:
        if self._busy or self._closing or not self.can_start():
            return
        path = filedialog.askdirectory(parent=self, title="显式选择导出来源任务（不改变 02）")
        if path:
            self.open_task(Path(path))

    def open_task(self, path: Path) -> None:
        if self._busy or self._closing or not self.can_start():
            return
        self._set_source(Path(path).expanduser(), manual=True)
        self.refresh_preview()

    def choose_parent(self) -> None:
        if self._busy or self._closing:
            return
        path = filedialog.askdirectory(parent=self, title="选择导出父目录（每次保存为独立历史）")
        if path:
            self.output_parent.set(path)

    def _open_source(self, path: Path) -> ReviewTask:
        if self._source_reader is None or self._source_reader.path != path.absolute():
            self._source_reader = ReviewTask.open_readonly(path, review_only=True)
        return self._source_reader

    def refresh_preview(self) -> None:
        path = self._source_path
        if path is None:
            self.status_text.set("请先绑定 02 当前任务，或打开其他任务。")
            return

        def preview() -> dict[str, Any]:
            task = self._open_source(path)
            return {"preview": delivery_export.preview_task(task, cache=self._cache)}

        self._submit(preview, "正在后台核验四步确认状态并读取投递预览…")

    def export(self) -> None:
        if not self._preview_current or self._preview is None or self._source_path is None:
            self.status_text.set("当前预览不可用或已过期；请先刷新预览。")
            return
        path, snapshot = self._source_path, self._preview["snapshot_token"]
        parent = Path(self.output_parent.get()).expanduser() if self.output_parent.get().strip() else path / "delivery-exports"
        self._latest_current = False

        def publish() -> dict[str, Any]:
            task = self._open_source(path)
            artifact = delivery_export.export_task(task, output_dir=parent, expected_snapshot=snapshot, cache=self._cache)
            artifact = _validated_export_path(artifact, parent)
            preview = delivery_export.preview_task(task, cache=self._cache)
            return {"preview": preview, "export_path": artifact, "export_snapshot": snapshot, "export_parent": parent}

        self._submit(publish, "正在后台导出；名单变化会取消本次发布…")

    def open_latest(self, *, directory: bool) -> None:
        if self._source_path is None or self._latest_export is None or not self._latest_current:
            self.status_text.set("尚无与当前预览一致的导出；请刷新并重新导出。")
            return
        path, artifact, parent = self._source_path, self._latest_export, self._latest_parent
        snapshot = self._latest_snapshot
        assert parent is not None

        def locate() -> dict[str, Any]:
            task = self._open_source(path)
            preview = delivery_export.preview_task(task, cache=self._cache)
            if preview["snapshot_token"] != snapshot:
                raise TaskStateError("最终名单或岗位数据已改变，历史导出不能标为当前结果；请刷新并重新导出。")
            return {"preview": preview, "open_path": _validated_export_path(artifact, parent, directory=directory)}

        self._submit(locate, "正在后台核对导出与当前名单…")

    def _submit(self, function: Callable[[], dict[str, Any]], status: str) -> None:
        if self._busy or self._closing:
            return
        if not self.can_start():
            self.status_text.set("其他页面正在处理任务；可切回查看进度，完成后再操作导出。")
            return
        self._busy = True
        source_version = self._source_version
        self.status_text.set(status)
        self.on_busy_change(True)
        self.render_controls()

        def work() -> None:
            try:
                result = function()
                if "preview" in result:
                    preview = result["preview"]
                    token = preview["snapshot_token"]
                    if self._display_cache is None or self._display_cache[0] != token:
                        prepared = []
                        for ready, values in ((True, preview["jobs"]), (False, preview["blocked_jobs"])):
                            for job in values:
                                reasons = job.get("blocked_reasons", [])
                                prepared.append((job["job_key"], ready, job, reasons,
                                                 json.dumps(job, ensure_ascii=False).casefold()))
                        self._display_cache = (token, prepared)
                    result["prepared_rows"] = self._display_cache[1]
            except Exception as error:
                result = {"error": _safe(error)}
            result["source_version"] = source_version
            self._events.put(result)

        self._worker = threading.Thread(target=work, name="delivery-export", daemon=True)
        self._worker.start()

    def _schedule_poll(self) -> None:
        if not self._closing and not self._closed:
            self._after_id = self.after(POLL_MS, self._drain_events)

    def _drain_events(self) -> None:
        self._after_id = None
        if self._closing or self._closed:
            return
        try:
            result = self._events.get_nowait()
        except queue.Empty:
            self._schedule_poll()
            return
        self._busy = False
        if result["source_version"] != self._source_version:
            self._invalidate("导出来源已改变，请刷新当前任务预览。")
        elif result.get("error"):
            self._invalidate("投递导出不可用：" + result["error"])
        else:
            self._preview = result["preview"]
            self._prepared_rows = result["prepared_rows"]
            self._preview_current = True
            summary = self._preview["summary"]
            self.summary_text.set(f"最终保留 {summary['selected_count']} · 可导出 {summary['ready_count']} · 阻止导出 {summary['blocked_count']}")
            self.status_text.set("已核验四步确认状态；可投递岗位写入 jobs，阻止项及原因写入 blocked_jobs。")
            if "export_path" in result:
                self._latest_export = result["export_path"]
                self._latest_parent = result["export_parent"]
                self._latest_snapshot = result["export_snapshot"]
            self._latest_current = self._latest_export is not None and self._latest_snapshot == self._preview["snapshot_token"]
            if self._latest_export is not None:
                self.latest_text.set(("当前名单导出：" if self._latest_current else "历史导出（与当前名单不同）：") + str(self._latest_export))
            self._render_rows()
            if result.get("open_path"):
                self.open_path(result["open_path"])
        self.on_busy_change(False)
        self.render_controls()
        self._schedule_poll()

    def render_controls(self) -> None:
        if self._closed:
            return
        available = not self._busy and not self._closing
        allowed = available and self.can_start()
        for widget in (self.open_task_button, self.refresh_button):
            widget.configure(state="normal" if allowed else "disabled")
        self.current_button.configure(state="normal" if available and self._current_path is not None else "disabled")
        for widget in (self.choose_parent_button, self.default_parent_button):
            widget.configure(state="normal" if available else "disabled")
        self.export_button.configure(state="normal" if allowed and self._preview_current else "disabled")
        for widget in (self.open_json_button, self.open_directory_button):
            widget.configure(state="normal" if allowed and self._latest_current else "disabled")

    def _filter_changed(self, *_args: Any) -> None:
        self._page = 0
        self._render_rows()

    def _render_rows(self) -> None:
        if self._closed:
            return
        query, selected = self.search.get().strip().casefold(), self.filter.get()
        filtered = [row for row in self._prepared_rows if (selected == "全部" or row[1] == (selected == "可导出"))
                    and (not query or query in row[4])]
        pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._page = max(0, min(self._page, pages - 1))
        self.table.delete(*self.table.get_children())
        self._rows = {}
        for key, ready, job, reasons, _search in filtered[self._page * PAGE_SIZE:(self._page + 1) * PAGE_SIZE]:
            text = "；".join(_safe(BLOCK_REASON_LABELS.get(reason, reason)) for reason in reasons)
            self._rows[key] = {"job": job, "ready": ready, "reasons": text}
            self.table.insert("", "end", iid=key, values=("可导出" if ready else "阻止导出", _safe(job.get("title", "")),
                              _safe(job.get("company", "")), _safe(job.get("location", "")), _safe(job.get("salary", "")), text), tags=("ready" if ready else "blocked",))
        self.page_text.set(f"{len(filtered)} 个岗位 · 第 {self._page + 1} / {pages} 页")
        self.previous_button.configure(state="normal" if self._page > 0 else "disabled")
        self.next_button.configure(state="normal" if self._page + 1 < pages else "disabled")
        if self._focus in self._rows:
            self.table.selection_set(self._focus)
            self.table.focus(self._focus)
        self._show_detail()

    def change_page(self, amount: int) -> None:
        self._page += amount
        self._render_rows()

    def _show_detail(self, _event: tk.Event | None = None) -> None:
        selection = self.table.selection()
        if selection:
            self._focus = selection[0]
        row = self._rows.get(self._focus or "")
        text = "选择岗位查看导出字段、JD 与阻止原因。"
        if row:
            text = ("可导出" if row["ready"] else "阻止导出：" + row["reasons"]) + "\n\n" + json.dumps(desktop_data._redact(row["job"]), ensure_ascii=False, indent=2)
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", _safe(text))
        self.detail.configure(state="disabled")

    def apply_theme(self, palette: dict[str, str]) -> None:
        self.palette = dict(palette)
        self.detail.configure(background=palette["surface"], foreground=palette["text"],
                              selectbackground=palette["selection"], selectforeground=palette["selected_text"],
                              insertbackground=palette["text"])
        self.table.tag_configure("ready", background=palette["surface"], foreground=palette["text"])
        self.table.tag_configure("blocked", background=palette["amber_soft"], foreground=palette["amber"])
        configure_combobox_popdown(self.filter_box, palette)

    def request_close(self) -> None:
        self._closing = True
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        self.render_controls()

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is self:
            self._closing = self._closed = True
            if self._after_id is not None:
                self.after_cancel(self._after_id)
                self._after_id = None
