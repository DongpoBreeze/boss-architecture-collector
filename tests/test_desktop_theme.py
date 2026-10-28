"""Live theme / source-explanation checks using real Tk, without any network."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_app
import desktop_ui
from desktop_routing import AUTO_RUN_CHOICE, NEW_RUN_CHOICE
from desktop_theme import LIGHT_PALETTE, normalize_theme, palette_for


def luminance(value):
    value = str(value)
    rgb = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
    return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))


def contrast(first, second):
    dark, light = sorted((luminance(first), luminance(second)))
    return (light + .05) / (dark + .05)


class ThemePreferenceTests(unittest.TestCase):
    def test_theme_is_whitelisted_and_secrets_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            desktop_ui.save_preferences({"theme": "dark", "city": "上海", "api_key": "private-key", "token": "private-token", "resume_run": "do-not-persist"}, path)
            self.assertEqual(desktop_ui.load_preferences(path), {"city": "上海", "theme": "dark"})
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("private", content)
            self.assertNotIn("do-not-persist", content)

    def test_invalid_themes_use_light_and_do_not_inject_style_values(self):
        for value in (None, "", "Dark", "unknown", "#ffffff", 4, {"theme": "dark"}):
            with self.subTest(value=value):
                self.assertEqual(normalize_theme(value), "light")
                self.assertEqual(palette_for(value), dict(LIGHT_PALETTE))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"theme":"unknown","city":"上海"}', encoding="utf-8")
            self.assertEqual(desktop_ui.load_preferences(path), {"city": "上海", "theme": "light"})
            desktop_ui.save_preferences({"theme": "unknown"}, path)
            self.assertEqual(desktop_ui.load_preferences(path), {"theme": "light"})

    def test_palette_copies_are_isolated_and_all_text_roles_have_contrast(self):
        daytime = palette_for("light")
        night = palette_for("dark")
        self.assertEqual(set(daytime), set(night))
        night["text"] = "#000000"
        self.assertNotEqual(palette_for("dark")["text"], night["text"])
        self.assertEqual(dict(desktop_ui.COLORS), daytime)
        pairs = (("text", "surface"), ("muted", "surface"), ("muted", "background"),
                 ("accent", "accent_soft"), ("green", "green_soft"), ("amber", "amber_soft"),
                 ("red", "red_soft"), ("disabled_text", "disabled"), ("on_primary", "primary"),
                 ("on_primary", "primary_hover"), ("selected_text", "selection"),
                 ("log_text", "log"), ("log_selected_text", "log_selection"),
                 ("log_warning", "log"), ("log_error", "log"), ("log_success", "log"))
        for theme in ("light", "dark"):
            colors = palette_for(theme)
            for foreground, background in pairs:
                with self.subTest(theme=theme, foreground=foreground, background=background):
                    self.assertGreaterEqual(contrast(colors[foreground], colors[background]), 4.5)


class TkThemeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.preference_file = Path(self.temp.name) / "preferences.json"
        self.source = Path(self.temp.name) / "day"
        self.source.mkdir()
        self.windows = []
        for patcher in (
            patch.object(desktop_ui, "preference_path", return_value=self.preference_file),
            patch.object(desktop_app.CollectorDesktopApp, "_refresh_stats"),
            patch.object(desktop_app.CollectorDesktopApp, "_refresh_history"),
            patch.object(desktop_app.CollectorDesktopApp, "_request_route_preview"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.close_windows)
        self.root, self.app = self.make_window()

    def make_window(self):
        try:
            root = desktop_app.tk.Tk()
        except desktop_app.tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        root.withdraw()
        try:
            app = desktop_app.CollectorDesktopApp(root, desktop_app.parse_args(["--root", str(self.source)]))
        except BaseException:
            root.destroy()
            raise
        self.windows.append((root, app))
        root.update_idletasks()
        return root, app

    def close_windows(self):
        for root, app in reversed(self.windows):
            app._closing = True
            app._close_workspace()
            if app._poll_after_id:
                root.after_cancel(app._poll_after_id)
            root.update_idletasks()
            root.destroy()
        self.windows.clear()

    def widgets(self, parent=None):
        pending = [parent or self.root]
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            yield widget

    def assert_widget_contrast(self, widget, foreground="foreground", background="background"):
        self.assertGreaterEqual(contrast(widget.cget(foreground), widget.cget(background)), 4.5)

    def test_live_toggle_preserves_results_logs_selection_filters_and_task(self):
        app = self.app
        jobs = [dict(job_id="one", title="AI方案", city="上海", company="测试", salary="20-30K",
                     jd="JD 原文与证据保留", classification="重点投递", model_status="success")]
        app._result_jobs = jobs
        app._filtered_jobs = jobs
        app._render_results()
        with patch.object(app, "_debounce_filters"), patch.object(app, "_apply_result_filters"):
            app.result_search.set("证据")
            app.result_class.set("重点投递")
        app._append_log("日志保留：成功完成\n日志保留：warning\n")
        app.log_view.tag_add("sel", "1.0", "1.3")
        app.detail_text.tag_add("sel", "1.0", "1.2")
        app.workflow_running = True
        app.active_run = self.source / "runs" / "active"
        app.resume_run.set(str(app.active_run))
        app._set_progress("17/50")
        app._workspace_event("status", "正在运行：JD补抓")
        app.notebook.select(app.results_page)
        before = dict(log=app.log_view.get("1.0", "end-1c"), detail=app.detail_text.get("1.0", "end-1c"),
                      log_selection=app.log_view.tag_ranges("sel"), detail_selection=app.detail_text.tag_ranges("sel"),
                      rows=app.results_table.get_children(), selection=app.results_table.selection(),
                      widgets=tuple(str(widget) for widget in self.widgets()), progress=app.progress_bar.cget("value"),
                      tab=app.notebook.select(), summary=app.routing_summary.get())
        for theme in ("dark", "light", "dark"):
            app._set_theme(theme, persist=False)
            self.assertEqual(app.theme.get(), theme)
            self.assertEqual(app.log_view.get("1.0", "end-1c"), before["log"])
            self.assertEqual(app.detail_text.get("1.0", "end-1c"), before["detail"])
            self.assertEqual(app.log_view.tag_ranges("sel"), before["log_selection"])
            self.assertEqual(app.detail_text.tag_ranges("sel"), before["detail_selection"])
            self.assertEqual(app.results_table.get_children(), before["rows"])
            self.assertEqual(app.results_table.selection(), before["selection"])
            self.assertEqual(tuple(str(widget) for widget in self.widgets()), before["widgets"])
            self.assertEqual(app.progress_bar.cget("value"), before["progress"])
            self.assertEqual(app.notebook.select(), before["tab"])
            self.assertEqual(app.routing_summary.get(), before["summary"])
            self.assertEqual(app.result_search.get(), "证据")
            self.assertEqual(app.result_class.get(), "重点投递")
            self.assertIs(app._result_jobs, jobs)
            self.assertTrue(app.workflow_running)
            self.assertEqual(app.resume_run.get(), str(app.active_run))
            self.assertEqual(app.sample_size.get(), "50")
            self.assertEqual(str(app.log_view.cget("state")), "disabled")
            self.assertEqual(str(app.detail_text.cget("state")), "disabled")
        self.assertFalse(self.preference_file.exists())

    def test_button_switches_immediately_and_new_window_restores_preference(self):
        self.assertEqual(self.app.theme.get(), "light")
        self.assertEqual(self.app.theme_button_text.get(), "夜间模式")
        self.app.theme_button.invoke()
        self.assertEqual(self.app.theme.get(), "dark")
        self.assertEqual(self.app.theme_button_text.get(), "日间模式")
        saved = json.loads(self.preference_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["theme"], "dark")
        self.assertLessEqual(set(saved), set(desktop_ui.PREFERENCE_FIELDS))
        root, second = self.make_window()
        self.assertEqual(second.theme.get(), "dark")
        self.assertEqual(root.cget("background"), palette_for("dark")["background"])
        second.theme_button.invoke()
        self.assertEqual(second.theme.get(), "light")
        self.assertEqual(self.app.theme.get(), "dark")

    def test_invalid_saved_and_runtime_theme_fall_back_safely(self):
        self.preference_file.write_text('{"theme":"not-a-theme"}', encoding="utf-8")
        root, app = self.make_window()
        self.assertEqual(app.theme.get(), "light")
        self.assertEqual(root.cget("background"), palette_for("light")["background"])
        app._set_theme("dark", persist=False)
        app._set_theme("invalid", persist=False)
        self.assertEqual(app.theme.get(), "light")
        self.assertEqual(app.detail_text.cget("background"), palette_for("light")["surface"])

    def test_two_tk_interpreters_keep_palettes_styles_and_variables_separate(self):
        root2, second = self.make_window()
        self.app._set_theme("dark", persist=False)
        second._set_theme("light", persist=False)
        self.app.status_text.set("first window")
        second.status_text.set("second window")
        self.assertEqual(self.app.status_text.get(), "first window")
        self.assertEqual(second.status_text.get(), "second window")
        for root, app, theme in ((self.root, self.app, "dark"), (root2, second, "light")):
            colors = palette_for(theme)
            self.assertEqual(root.cget("background"), colors["background"])
            self.assertEqual(app.detail_text.cget("background"), colors["surface"])
            self.assertEqual(desktop_app.ttk.Style(root).lookup("TCombobox", "fieldbackground", ("readonly",)), colors["surface"])
            self.assertIs(app.theme._tk, root.tk)
            self.assertIs(app.result_search._tk, root.tk)
        self.assertIsNot(self.app.colors, second.colors)
        self.assertEqual(dict(desktop_ui.COLORS), palette_for("light"))

    def test_night_controls_text_menus_and_popdowns_have_no_white_fields(self):
        combo = self.app.history_box
        popdown = combo.tk.call("ttk::combobox::PopdownWindow", str(combo))
        menu = desktop_app.tk.Menu(self.root, tearoff=False)
        menu.add_command(label="复制")
        menu.add_command(label="不可用", state="disabled")
        for theme in ("dark", "light", "dark"):
            self.app._set_theme(theme, persist=False)
            colors = self.app.colors
            style = desktop_app.ttk.Style(self.root)
            for widget in self.widgets():
                if isinstance(widget, desktop_app.tk.Text):
                    self.assert_widget_contrast(widget)
                    self.assert_widget_contrast(widget, "selectforeground", "selectbackground")
                    self.assertNotEqual(widget.cget("insertbackground"), widget.cget("background"))
                    if theme == "dark":
                        self.assertLess(luminance(widget.cget("background")), .1)
                if isinstance(widget, desktop_app.tk.Label):
                    self.assert_widget_contrast(widget)
                if isinstance(widget, desktop_app.ttk.Combobox):
                    dropdown = widget.tk.call("ttk::combobox::PopdownWindow", str(widget))
                    for option, role in (("background", "surface"), ("foreground", "text"),
                                         ("selectbackground", "selection"), ("selectforeground", "selected_text")):
                        self.assertEqual(widget.tk.call(f"{dropdown}.f.l", "cget", f"-{option}"), colors[role])
            self.assertEqual(combo.tk.call(f"{popdown}.f.l", "cget", "-background"), colors["surface"])
            for name in ("TEntry", "TCombobox", "TSpinbox"):
                for state in (("readonly",), ("readonly", "focus"), ("disabled",), ("disabled", "readonly")):
                    background = style.lookup(name, "fieldbackground", state)
                    foreground = style.lookup(name, "foreground", state)
                    self.assertEqual(background, colors["disabled"] if "disabled" in state else colors["surface"])
                    self.assertGreaterEqual(contrast(foreground, background), 4.5)
            for name in ("TButton", "Primary.TButton", "TCheckbutton", "TNotebook.Tab", "Treeview", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
                for state in ((), ("active",), ("selected",), ("disabled",)):
                    background = style.lookup(name, "background", state)
                    if theme == "dark":
                        self.assertLess(luminance(background), .3, (name, state, background))
            for tag in ("重点投递", "海投池", "暂不投"):
                self.assertGreaterEqual(contrast(self.app.results_table.tag_configure(tag, "foreground"), self.app.results_table.tag_configure(tag, "background")), 4.5)
            self.assertEqual(str(menu.cget("background")), colors["surface"])
            self.assertEqual(str(menu.cget("activebackground")), colors["selection"])
            self.assertGreaterEqual(contrast(menu.cget("disabledforeground"), menu.cget("background")), 4.5)
        future = desktop_app.ttk.Combobox(self.root, state="readonly", values=("之后创建的下拉",))
        dropdown = future.tk.call("ttk::combobox::PopdownWindow", str(future))
        self.assertEqual(future.tk.call(f"{dropdown}.f.l", "cget", "-background"), self.app.colors["surface"])
        self.assertEqual(future.tk.call(f"{dropdown}.f.l", "cget", "-selectforeground"), self.app.colors["selected_text"])

    def test_switch_keeps_real_disabled_settings_disabled_and_toggle_available(self):
        for widget, _normal_state in self.app._settings_controls:
            widget.configure(state="disabled")
        for theme in ("dark", "light"):
            self.app._set_theme(theme, persist=False)
            for widget, _normal_state in self.app._settings_controls:
                self.assertIn("disabled", widget.state())
            self.assertNotIn("disabled", self.app.theme_button.state())

    def test_step_and_badge_statuses_survive_switch_and_later_events_use_theme(self):
        app = self.app
        stages = list(app._step_labels)
        statuses = ("pending", "running", "success", "partial", "failed")
        roles = ("neutral_soft", "accent_soft", "green_soft", "amber_soft", "red_soft")
        app._apply_snapshot({"stage_statuses": dict(zip(stages, statuses))})
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            for stage, role in zip(stages, roles):
                self.assertEqual(app._step_labels[stage].cget("background"), app.colors[role])
                self.assert_widget_contrast(app._step_labels[stage])
        app._workspace_event("human_wait", True)
        app._set_theme("dark", persist=False)
        self.assertEqual(app.status_badge.cget("background"), app.colors["amber_soft"])
        app.workflow_running = True
        app._workspace_event("human_wait", False)
        self.assertEqual(app.status_badge.cget("foreground"), app.colors["accent"])
        app._on_workspace_start("JD补抓")
        self.assertTrue(all(widget.cget("background") == app.colors["neutral_soft"] for widget in app._step_labels.values()))
        app._workspace_event("status", "正在运行：JD补抓")
        self.assertEqual(app._step_labels["JD补抓"].cget("background"), app.colors["accent_soft"])
        with patch.object(app, "_load_result_run"):
            app._workspace_event("finished", {"exit_code": 0, "manifest": {"status": "success"}, "output": str(self.source)})
        app._set_theme("light", persist=False)
        self.assertEqual(app.status_badge.cget("background"), app.colors["green_soft"])

    def test_history_refresh_preserves_auto_new_and_explicit_missing_path(self):
        app = self.app
        app._viewed_run = self.source / "browsed"
        for selection in (AUTO_RUN_CHOICE, NEW_RUN_CHOICE, str(self.source / "runs" / "not-in-list")):
            app.resume_run.set(selection)
            scope = (app.city.get(), app.track.get(), app.sample_size.get(), app.stage.get())
            app._workspace_event("history_loaded", (app._history_request_id, []))
            self.assertEqual(app.resume_run.get(), selection)
            self.assertEqual(app.history_values[:2], [AUTO_RUN_CHOICE, NEW_RUN_CHOICE])
            self.assertIn(selection, app.history_box.cget("values"))
            self.assertEqual((app.city.get(), app.track.get(), app.sample_size.get(), app.stage.get()), scope)

    def test_daily_folder_explanation_and_scrollable_multiline_routing(self):
        labels = {widget.cget("text"): widget for widget in self.widgets() if isinstance(widget, desktop_app.ttk.Label)}
        self.assertIn("采集总文件夹（某天的目录）", labels)
        self.assertIn("只选这里一次，程序自动识别 raw、JD、模型结果", labels)
        self.assertIn("数据路由", labels)
        self.assertFalse(any("需选择已有运行" in text for text in labels))
        tool_label = labels["上游采集工具（仅采集/JD需要，通常自动发现）"]
        source_label = labels["采集总文件夹（某天的目录）"]
        self.assertIsNot(tool_label.master, source_label.master)
        text = "\n".join(["raw：日目录/raw", "JD：历史/prepared", "模型：历史模型检查点", "警告：仅复用匹配本次范围的数据"] * 8)
        self.app.routing_summary.set(text)
        self.app._set_theme("dark", persist=False)
        self.assertEqual(self.app.routing_summary_text.get("1.0", "end-1c"), text)
        self.assertEqual(str(self.app.routing_summary_text.cget("state")), "disabled")
        self.assertTrue(self.app.routing_summary_text.cget("yscrollcommand"))
        self.assertTrue(self.app.routing_summary_text.tag_ranges("warning"))

    def test_history_normalizes_nested_input_on_worker_without_changing_selection(self):
        nested = self.source / "raw" / "current" / "shanghai"
        self.app.data_root.set(str(nested))
        original_choice = self.app.resume_run.get()
        calls = []

        def history(root):
            calls.append((root, threading.get_ident()))
            return []

        with patch.object(desktop_ui, "resolve_daily_root", return_value=(self.source, [])) as normalize, patch.object(desktop_ui.desktop_data, "list_run_history", side_effect=history):
            self.app._refresh_workspace_history()
            kind, value = self.app.events.get(timeout=3)
        self.assertEqual(kind, "history_loaded")
        self.assertEqual(value, (self.app._history_request_id, []))
        normalize.assert_called_once_with(nested)
        self.assertEqual(calls[0][0], self.source)
        self.assertNotEqual(calls[0][1], threading.get_ident())
        self.assertEqual(self.app.data_root.get(), str(nested))
        self.assertEqual(self.app.resume_run.get(), original_choice)
        with patch.object(desktop_ui, "resolve_daily_root", side_effect=ValueError("ambiguous date")), patch.object(desktop_ui.desktop_data, "list_run_history") as loader:
            self.app._refresh_workspace_history()
            kind, value = self.app.events.get(timeout=3)
        self.assertEqual(kind, "history_error")
        self.assertEqual(value, (self.app._history_request_id, "ValueError"))
        loader.assert_not_called()

    def test_minimum_window_keeps_switch_buttons_and_log_reachable(self):
        self.root.geometry("1080x700")
        self.root.deiconify()
        self.app.status_text.set("正在等待人工完成登录或安全验证，检查点已保留。请完成后继续本次 JD 补抓任务。")
        for theme in ("dark", "light"):
            self.app._set_theme(theme, persist=False)
            self.app._workspace_event("human_wait", True)
            self.root.update()
            for widget in (self.app.theme_button, self.app.status_badge, self.app.start_button,
                           self.app.pause_button, self.app.continue_button, self.app.stop_button,
                           self.app.prepare_browser_button, self.app.log_view):
                with self.subTest(theme=theme, widget=str(widget)):
                    x = widget.winfo_rootx() - self.root.winfo_rootx()
                    y = widget.winfo_rooty() - self.root.winfo_rooty()
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                    self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
            self.assertGreater(self.app.log_view.winfo_height(), 50)

    def test_preference_write_failure_does_not_interrupt_switch(self):
        with patch.object(desktop_ui, "save_preferences", side_effect=OSError("read-only settings")):
            self.app._set_theme("dark")
        self.assertEqual(self.app.theme.get(), "dark")
        self.assertEqual(self.app.log_view.cget("background"), palette_for("dark")["log"])

    def test_original_configure_styles_signature_is_supported(self):
        self.app._set_theme("dark", persist=False)
        desktop_ui.configure_styles(self.root)
        self.assertEqual(desktop_app.ttk.Style(self.root).lookup("TEntry", "fieldbackground"), palette_for("light")["surface"])

    def test_workspace_close_cancels_pending_route_preview_callback(self):
        callback = self.root.after(60000, lambda: None)
        self.app._route_after_id = callback
        self.app._close_workspace()
        self.assertIsNone(self.app._route_after_id)
        self.assertNotIn(callback, self.root.tk.call("after", "info"))


if __name__ == "__main__":
    unittest.main()
