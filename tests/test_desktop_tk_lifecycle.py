"""Real-window shutdown followed by worker-thread GC, isolated from the suite."""

from __future__ import annotations

import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import weakref

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def _probe() -> None:
    import tkinter as tk
    import collect_architecture as collector
    import collection_plan
    import desktop_app
    import desktop_collection
    import desktop_review_ui
    import desktop_ui
    from desktop_tk_lifecycle import StringVar

    callback_errors = []
    unraisable_errors = []
    sys.unraisablehook = lambda error: unraisable_errors.append(str(error.exc_value))

    def hidden_root():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-alpha", 0.0)
        if sys.platform == "win32":
            root.attributes("-disabled", True)
        root.report_callback_exception = lambda *error: callback_errors.append(error)
        # Deliberately retain a destroyed window until the GC worker releases it.
        root.cycle = root
        return root

    # Cross-thread cleanup must fail before touching Tcl, leaving the live
    # variable/window usable on the owning thread.
    root = hidden_root()
    value = StringVar(root, "still live")
    affinity_errors = []

    def wrong_thread():
        for action in (value.release, root.destroy):
            try:
                action()
            except RuntimeError as error:
                affinity_errors.append(str(error))

    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(affinity_errors) == 2, affinity_errors
    assert value.get() == "still live"
    root.destroy()
    del root, value, worker
    gc.collect()

    def collect_on_worker(held, references):
        errors = []

        def collect():
            try:
                held.clear()
                gc.collect()
                assert all(reference() is None for reference in references)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=collect)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors, errors

    def closed_window(kind, directory):
        root = hidden_root()
        args = desktop_app.parse_args(["--root", str(directory / "source")])
        if kind == "collection":
            app = desktop_collection.CollectionPage(root, config_path=args.config,
                                                    initial_root=str(directory / "source"))
            app.pack(fill="both", expand=True)
            config = collector.load_config(args.config)
            editor = desktop_collection._CollectionPlanEditor(
                app, config, collection_plan.plan_editor_values(config))
            editor.withdraw()
            app._editor = editor
            editor.close()
            assert editor.tk is None
            assert all(variable._tk is None for variable in editor._variables)
            # A removed page/editor must detach independently while its host
            # root stays live; a later root.destroy must remain idempotent.
            app.request_close()
            deadline = time.monotonic() + 5
            while not app.shutdown_complete and time.monotonic() < deadline:
                root.update()
                time.sleep(.005)
            assert app.shutdown_complete
            app.destroy()
            assert root.winfo_exists()
            assert app.tk is None
            root.destroy()
            held = [root, app, editor]
        elif kind == "legacy":
            with patch.object(desktop_app.CollectorDesktopApp, "_refresh_stats"), \
                    patch.object(desktop_app.CollectorDesktopApp, "_refresh_history"), \
                    patch.object(desktop_app.CollectorDesktopApp, "_request_route_preview"):
                app = desktop_app.CollectorDesktopApp(root, args)
            app._close()
            assert app._app_icon.tk is None
            assert app.data_root._tk is None
            held = [root, app]
        else:
            app = desktop_review_ui.ReviewDesktopApp(root, args)
            root.update_idletasks()
            dialogs = []
            for _ in range(2):
                app._edit_model_configuration()
                dialog = app._model_config_dialog
                variables = list(app._model_config_vars.values())
                app._model_config_vars["api_mode"].set("Chat Completions")
                effort_box = app._model_config_effort_box
                dialog.cycle = dialog
                app._close_model_configuration(dialog)
                assert dialog.tk is None and effort_box.tk is None
                assert all(variable._tk is None for variable in variables)
                assert root.winfo_exists(), "closing model configuration must leave the workbench alive"
                dialogs.extend([dialog, effort_box, *variables])
            app._close()
            deadline = time.monotonic() + 5
            while not app._closed and time.monotonic() < deadline:
                root.update()
                time.sleep(.005)
            assert app._closed
            assert app.source_root._tk is None
            held = [root, app, *dialogs]
        assert root.tk is None
        root.destroy()
        return held, [weakref.ref(item) for item in held]

    with tempfile.TemporaryDirectory(prefix="boss-tk-probe-") as temporary, \
            patch.object(desktop_app, "load_preferences", return_value={}), \
            patch.object(desktop_ui, "load_preferences", return_value={}), \
            patch.object(desktop_ui, "save_preferences"), \
            patch.object(desktop_review_ui, "load_preferences", return_value={}), \
            patch.object(desktop_review_ui, "save_preferences"), \
            patch.object(subprocess, "Popen", side_effect=AssertionError("no external process in GUI lifecycle probe")):
        directory = Path(temporary)
        for _ in range(3):
            for kind in ("collection", "legacy", "review"):
                held, references = closed_window(kind, directory)
                collect_on_worker(held, references)
    assert not callback_errors, callback_errors
    assert not unraisable_errors, unraisable_errors
    print("Closed 9 collection/legacy/review windows; worker GC and Tk thread checks passed")


def _probe_callback_ownership() -> None:
    import tkinter as tk
    import collect_architecture as collector
    import collection_plan
    import desktop_app
    import desktop_collection
    from desktop_tk_lifecycle import StringVar, destroy_tk_widget, install_tk_lifecycle

    errors = []
    called = []

    def hidden_root():
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *error: errors.append(error)
        install_tk_lifecycle(root)
        return root

    root = hidden_root()
    frame = tk.Frame(root)
    nested = tk.Frame(frame)
    label = tk.Label(nested, text="nested timer")
    widgets = (root, frame, nested, label)
    for widget in widgets:
        widget.after(60000, lambda: called.append("timer"))
        widget.after_idle(lambda: called.append("idle"))
    # Tcl-only timers have no Python command registry entry.
    root.tk.call("after", 60000, "set", "native_timer", "1")
    commands = [name for widget in widgets for name in widget._tclCommands or ()]
    interpreter = root.tk
    root.destroy()
    assert not interpreter.call("after", "info")
    assert all(not interpreter.call("info", "commands", name) for name in commands)
    assert all(widget._tclCommands is None and widget.tk is None for widget in widgets)
    assert not called, called
    del interpreter

    # A destroy failure is propagated while the live objects remain attached;
    # retrying normal destruction after fixing the child remains possible.
    for destroy_root in (True, False):
        root = hidden_root()
        frame = tk.Frame(root)
        nested = tk.Frame(frame)
        value = StringVar(frame, "live until destruction succeeds")
        nested.after(60000, lambda: called.append("retry timer"))
        action = root.destroy if destroy_root else lambda: destroy_tk_widget(frame)
        with patch.object(nested, "destroy", side_effect=RuntimeError("fixture destroy failure")):
            try:
                action()
            except RuntimeError as error:
                assert str(error) == "fixture destroy failure"
            else:
                raise AssertionError("destroy must propagate the original child failure")
        assert root.tk is not None and frame.tk is root.tk and nested.tk is root.tk
        assert root.winfo_exists() and frame.winfo_exists() and nested.winfo_exists()
        assert value.get() == "live until destruction succeeds"
        action()
        assert value._tk is None and frame.tk is None and nested.tk is None
        if not destroy_root:
            assert root.winfo_exists()
            root.update()
            root.destroy()

    with tempfile.TemporaryDirectory(prefix="boss-tk-active-page-") as temporary:
        for destroy_root in (False, True):
            root = hidden_root()
            args = desktop_app.parse_args(["--root", str(Path(temporary) / "source")])
            page = desktop_collection.CollectionPage(root, config_path=args.config,
                                                     initial_root=str(args.root))
            page.pack(fill="both", expand=True)
            config = collector.load_config(args.config)
            editor = desktop_collection._CollectionPlanEditor(
                page, config, collection_plan.plan_editor_values(config))
            editor.withdraw()
            page._editor = editor
            nested = tk.Frame(page)
            nested.after(60000, lambda: called.append("page child timer"))
            nested.after_idle(lambda: called.append("page child idle"))
            assert page._after_ids, "the page must still own active timers"
            # Keep the editor open and callbacks active; neither path should
            # require callers to request_close before destroying the window.
            if destroy_root:
                root.destroy()
            else:
                page.destroy()
                assert root.winfo_exists()
                root.update()
                root.destroy()
            assert editor._closed and editor.tk is None
            assert page._closing and not page._after_ids
            assert root.tk is None and page.tk is None and nested.tk is None
            assert all(variable._tk is None for variable in page._variables)
    assert not errors, errors
    assert not called, called
    print("Active page, nested widget timer ownership, and failed-destroy retry checks passed")


class TkLifecycleTests(unittest.TestCase):
    def run_probe(self, option):
        with tempfile.TemporaryDirectory(prefix="boss-tk-test-") as temporary:
            environment = dict(os.environ)
            environment.update({
                "CODEX_HOME": str(Path(temporary) / "codex"),
                "LOCALAPPDATA": str(Path(temporary) / "local-app-data"),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", __file__, option],
                cwd=PROJECT, env=environment, capture_output=True, text=True,
                encoding="utf-8", timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_repeated_real_windows_survive_worker_gc_after_shutdown(self):
        result = self.run_probe("--probe")
        self.assertIn("Closed 9 collection/legacy/review windows", result)

    def test_direct_destroy_preserves_child_timer_ownership_and_can_retry_failure(self):
        result = self.run_probe("--callback-probe")
        self.assertIn("Active page, nested widget timer ownership", result)


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        _probe()
    elif sys.argv[1:] == ["--callback-probe"]:
        _probe_callback_ownership()
    else:
        unittest.main()
