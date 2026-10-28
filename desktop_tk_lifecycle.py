"""Release a workbench's Tcl ownership on the thread that created its root.

Tk.destroy() removes windows and callbacks, but Python widget/variable cycles
can retain the Tcl interpreter. A later worker-thread GC must not finalize it.
Only the root's destroy method is wrapped; no global Tk or GC behavior changes.
"""

from __future__ import annotations

import threading
import tkinter as tk


def _widget_tree(widget: tk.Misc) -> list[tk.Misc]:
    widgets = [widget]
    for child in tuple(widget.children.values()):
        widgets.extend(_widget_tree(child))
    return widgets


class _TkLifecycle:
    def __init__(self, root: tk.Tk):
        self.root: tk.Tk | None = root
        self.thread_id = threading.get_ident()
        self.resources: dict[int, _Variable | PhotoImage] = {}
        self.closed = False
        self._destroy = root.destroy
        root.destroy = self.destroy

    def check_thread(self) -> None:
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Tk resources must be released on their owning thread")

    def release_widgets(self, widgets: list[tk.Misc]) -> None:
        owners = {id(widget) for widget in widgets}
        for resource in tuple(self.resources.values()):
            if id(resource._tk_owner) in owners:
                resource.release()
        for widget in widgets:
            # Destroyed widgets may remain in application/worker cycles. None
            # of those cycles should retain the native Tcl interpreter.
            widget.tk = None
            widget.master = None

    def destroy(self) -> None:
        self.check_thread()
        if self.closed:
            return
        root = self.root
        assert root is not None
        widgets = _widget_tree(root)
        # Cancel Tcl timers without deleting their Python commands. Each
        # command belongs to the widget that registered it; normal destroy()
        # must remove it from that widget's own _tclCommands exactly once.
        for identifier in root.tk.call("after", "info"):
            root.tk.call("after", "cancel", identifier)
        self._destroy()
        # A failing widget destroy must leave the still-live tree attached so
        # the caller receives the original failure and can retry cleanup.
        self.release_widgets(widgets)
        for resource in tuple(self.resources.values()):
            resource.release()
        self.closed = True
        self._destroy = None
        self.root = None


def install_tk_lifecycle(widget: tk.Misc) -> _TkLifecycle:
    """Install once per root; also covers callers that directly destroy it."""
    root = widget._root()
    lifecycle = vars(root).get("_desktop_tk_lifecycle")
    if lifecycle is None:
        lifecycle = _TkLifecycle(root)
        root._desktop_tk_lifecycle = lifecycle
    lifecycle.check_thread()
    if lifecycle.closed:
        raise tk.TclError("the workbench has been destroyed")
    return lifecycle


def destroy_tk_widget(widget: tk.Misc) -> None:
    """Destroy an independently removable page/editor and detach its tree."""
    if widget.tk is None:
        return
    lifecycle = install_tk_lifecycle(widget)
    widgets = _widget_tree(widget)
    commands = {name for item in widgets for name in item._tclCommands or ()}
    # A removed subtree can own timers while the host root remains live.
    # Cancel only its timers, preserving the command registries for destroy.
    for identifier in widget.tk.call("after", "info"):
        script = widget.tk.splitlist(widget.tk.call("after", "info", identifier))[0]
        if script in commands:
            widget.tk.call("after", "cancel", identifier)
    tk.BaseWidget.destroy(widget)
    lifecycle.release_widgets(widgets)


class _Variable:
    def __init__(self, master=None, value=None, name=None):
        if master is None:
            master = tk._get_default_root("create variable")
        self._lifecycle = install_tk_lifecycle(master)
        self._tk_owner = master
        super().__init__(master, value, name)
        self._lifecycle.resources[id(self)] = self

    def release(self) -> None:
        if self._tk is None:
            return
        lifecycle = self._lifecycle
        lifecycle.check_thread()
        try:
            tk.Variable.__del__(self)
        except tk.TclError:
            pass
        finally:
            self._tk = self._root = self._tk_owner = None
            self._tclCommands = None
            lifecycle.resources.pop(id(self), None)
            self._lifecycle = None


class StringVar(_Variable, tk.StringVar):
    """A StringVar whose Tcl state is explicitly released during destruction."""


class BooleanVar(_Variable, tk.BooleanVar):
    """A BooleanVar with the same workbench-owned lifetime as StringVar."""


class PhotoImage(tk.PhotoImage):
    def __init__(self, *, master: tk.Misc, **kwargs):
        self._lifecycle = install_tk_lifecycle(master)
        self._tk_owner = master
        super().__init__(master=master, **kwargs)
        self._lifecycle.resources[id(self)] = self

    def release(self) -> None:
        if self.tk is None:
            return
        lifecycle = self._lifecycle
        lifecycle.check_thread()
        try:
            tk.Image.__del__(self)
        finally:
            self.name = self.tk = self._tk_owner = None
            lifecycle.resources.pop(id(self), None)
            self._lifecycle = None
