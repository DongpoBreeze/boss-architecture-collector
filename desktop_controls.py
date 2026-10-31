"""Shared, stateless controls for the three desktop workspaces."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable


PAGE_SIZE = 20


def bind_search(entry: ttk.Entry, value: tk.Variable) -> None:
    def clear(_event: tk.Event) -> str:
        value.set("")
        return "break"
    entry.bind("<Escape>", clear)


def flow_row(frame: ttk.Frame, *, gap: int = 6) -> None:
    """Wrap a row of fixed-size controls using measured font/DPI requests."""
    children = frame.winfo_children()
    for child in children:
        child.pack_forget()
    previous: list[tuple[int, int, int, int]] = []

    def layout(_event: tk.Event | None = None) -> None:
        available = max(1, frame.winfo_width())
        x, y, row_height = 0, 2, 0
        positions = []
        for child in children:
            width, height = child.winfo_reqwidth(), child.winfo_reqheight()
            if x and x + width > available:
                x, y, row_height = 0, y + row_height + 4, 0
            positions.append((x, y, width, height))
            x += width + gap
            row_height = max(row_height, height)
        if positions != previous:
            previous[:] = positions
            for child, (x, y, width, height) in zip(children, positions):
                child.place(x=x, y=y, width=width, height=height)
            frame.configure(height=y + row_height + 2)
    frame.bind("<Configure>", layout, add="+")
    layout()


def configure_editor_dialog(dialog: tk.Toplevel, owner: tk.Misc) -> int:
    dialog.transient(owner)
    dialog.minsize(620, 440)
    width = min(820, max(620, owner.winfo_width() - 80), owner.winfo_screenwidth() - 60)
    height = min(700, max(460, owner.winfo_height() - 70), owner.winfo_screenheight() - 100)
    dialog.geometry(f"{width}x{height}")
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(0, weight=1)
    return width


def page_caption(total: int, page: int, size: int = PAGE_SIZE) -> str:
    if total == 0:
        return "0 个岗位"
    pages = (total + size - 1) // size
    page = max(0, min(page, pages - 1))
    return f"{page * size + 1}–{min((page + 1) * size, total)} / {total} 岗 · 第 {page + 1} / {pages} 页"


class Pagination(ttk.Frame):
    """One ordering and visual treatment; the owning page controls state."""

    def __init__(self, parent: tk.Misc, *, textvariable: tk.Variable,
                 previous: Callable[[], None], next: Callable[[], None]):
        super().__init__(parent, style="Card.TFrame")
        self.previous_button = ttk.Button(self, text="上一页", style="Workspace.TButton", command=previous)
        self.previous_button.pack(side="left")
        ttk.Label(self, textvariable=textvariable, style="Hint.TLabel").pack(side="left", padx=7)
        self.next_button = ttk.Button(self, text="下一页", style="Workspace.TButton", command=next)
        self.next_button.pack(side="left")


class ReadOnlyText(ttk.Frame):
    def __init__(self, parent: tk.Misc, **options):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(self, state="disabled", wrap="word", relief="flat", borderwidth=0,
                            padx=8, pady=7, font=("Microsoft YaHei UI", 9), exportselection=False, **options)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=self.scrollbar.set)


class DialogActions(ttk.Frame):
    """Draft actions share placement while keeping their explicit save scope."""

    def __init__(self, parent: tk.Misc, *, save: Callable[[], None], cancel: Callable[[], None],
                 save_text: str, reset: Callable[[], None] | None = None):
        super().__init__(parent)
        self.buttons: dict[str, ttk.Button] = {}
        if reset is not None:
            button = ttk.Button(self, text="恢复默认", style="Workspace.TButton", command=reset)
            button.pack(side="left")
            self.buttons["reset"] = button
        button = ttk.Button(self, text="取消", style="Workspace.TButton", command=cancel)
        button.pack(side="right")
        self.buttons["cancel"] = button
        button = ttk.Button(self, text=save_text, style="Workspace.Primary.TButton", command=save)
        button.pack(side="right", padx=(0, 8))
        self.buttons["save"] = button
