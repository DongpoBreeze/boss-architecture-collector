"""Semantic, interpreter-local palettes for the Tk desktop workbench.

No palette or Style is mutated globally: each workbench owns a palette copy,
and ttk styles / option defaults are applied only to the supplied Tk root.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping
import tkinter as tk
from tkinter import ttk


LIGHT_PALETTE = MappingProxyType({
    "background": "#f3f6fb", "surface": "#ffffff", "text": "#17263e",
    "muted": "#596b82", "border": "#d5deec", "accent": "#235bdd",
    "accent_soft": "#eaf0ff", "green": "#08735b", "green_soft": "#e7f6ef",
    "amber": "#885909", "amber_soft": "#fff4da", "red": "#b13248",
    "red_soft": "#fff0f1", "neutral_soft": "#edf2fa",
    "button_hover": "#e5ecf8", "button_pressed": "#dce6f7",
    "disabled": "#e9eef5", "disabled_text": "#5b6a80",
    "primary": "#2864d9", "primary_hover": "#1c52bb",
    "primary_pressed": "#1547a6", "on_primary": "#ffffff",
    "selection": "#d6e5ff", "selected_text": "#17263e", "focus": "#235bdd",
    "trough": "#e3eaf5", "scrollbar": "#bdcbe0", "scrollbar_hover": "#8fa8cf",
    "table_priority": "#edf8f2", "table_pool": "#f1f5ff", "table_excluded": "#f5f7fb",
    "table_suggest_exclude": "#fff0f1",
    "log": "#142138", "log_text": "#d7e2f2", "log_selection": "#37577c",
    "log_selected_text": "#ffffff", "log_warning": "#ffd38d",
    "log_error": "#ff9faa", "log_success": "#8ce1bb",
})
DARK_PALETTE = MappingProxyType({
    "background": "#101824", "surface": "#192437", "text": "#e6edf8",
    "muted": "#a6b5cc", "border": "#394b66", "accent": "#9dbdff",
    "accent_soft": "#253c65", "green": "#8de1bc", "green_soft": "#173e35",
    "amber": "#f4cf88", "amber_soft": "#49391f", "red": "#ffb0bb",
    "red_soft": "#4c2938", "neutral_soft": "#243146",
    "button_hover": "#2b3c55", "button_pressed": "#354b6c",
    "disabled": "#202d41", "disabled_text": "#a2b1c8",
    "primary": "#3565be", "primary_hover": "#4072cb",
    "primary_pressed": "#2c58ab", "on_primary": "#ffffff",
    "selection": "#365888", "selected_text": "#ffffff", "focus": "#9dbdff",
    "trough": "#27354b", "scrollbar": "#4b6180", "scrollbar_hover": "#6885ad",
    "table_priority": "#1c352f", "table_pool": "#20324c", "table_excluded": "#202b3d",
    "table_suggest_exclude": "#4c2938",
    "log": "#0c1421", "log_text": "#d7e2f2", "log_selection": "#365888",
    "log_selected_text": "#ffffff", "log_warning": "#ffd38d",
    "log_error": "#ffabb8", "log_success": "#8ce1bb",
})


def normalize_theme(value: object) -> str:
    """Unknown / corrupted preference values safely use the daytime theme."""
    return "dark" if value == "dark" else "light"


def palette_for(theme: object) -> dict[str, str]:
    return dict(DARK_PALETTE if normalize_theme(theme) == "dark" else LIGHT_PALETTE)


def configure_styles(root: tk.Tk, palette: Mapping[str, str] | None = None) -> None:
    """Retheme this interpreter without rebuilding widgets or changing states.

    The one-argument form remains the original daytime-style entry point.
    Clam is used on Windows so native field / selection painters cannot leave
    bright, unreadable controls inside a night-mode window.
    """
    p = palette if palette is not None else LIGHT_PALETTE
    style = ttk.Style(root)
    if style.theme_use() != "clam":
        style.theme_use("clam")
    root.configure(background=p["background"])
    root.option_add("*Font", ("Microsoft YaHei UI", 10))
    for name, value in {
        "background": p["surface"], "foreground": p["text"],
        "selectBackground": p["selection"], "selectForeground": p["selected_text"],
        "highlightBackground": p["border"], "highlightColor": p["focus"],
    }.items():
        root.option_add(f"*TCombobox*Listbox.{name}", value)
    for name, value in {
        "background": p["surface"], "foreground": p["text"],
        "activeBackground": p["selection"], "activeForeground": p["selected_text"],
        "disabledForeground": p["disabled_text"], "selectColor": p["accent"],
    }.items():
        root.option_add(f"*Menu.{name}", value)
    style.configure(".", background=p["background"], foreground=p["text"],
                    fieldbackground=p["surface"], bordercolor=p["border"],
                    lightcolor=p["border"], darkcolor=p["border"],
                    troughcolor=p["trough"], focuscolor=p["focus"],
                    selectbackground=p["selection"], selectforeground=p["selected_text"])
    style.configure("TFrame", background=p["background"])
    style.configure("TLabel", background=p["background"], foreground=p["text"])
    style.configure("Card.TFrame", background=p["surface"])
    style.configure("Card.TLabel", background=p["surface"], foreground=p["text"])
    style.configure("Muted.TLabel", background=p["background"], foreground=p["muted"])
    style.configure("Hint.TLabel", background=p["surface"], foreground=p["muted"], font=("Microsoft YaHei UI", 9))
    style.configure("Section.TLabel", background=p["surface"], foreground=p["text"], font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("Title.TLabel", foreground=p["text"], font=("Microsoft YaHei UI", 22, "bold"))
    style.configure("Warning.TLabel", foreground=p["amber"])
    style.configure("TButton", padding=(12, 8), background=p["surface"], foreground=p["text"],
                    bordercolor=p["border"], relief="flat", focusthickness=2)
    style.map("TButton", background=[("disabled", p["disabled"]), ("pressed", p["button_pressed"]), ("active", p["button_hover"])],
              foreground=[("disabled", p["disabled_text"])],
              bordercolor=[("focus", p["focus"])], lightcolor=[], darkcolor=[])
    style.configure("Primary.TButton", background=p["primary"], foreground=p["on_primary"],
                    font=("Microsoft YaHei UI", 10, "bold"), borderwidth=0)
    style.map("Primary.TButton", background=[("disabled", p["disabled"]), ("pressed", p["primary_pressed"]), ("active", p["primary_hover"])],
              foreground=[("disabled", p["disabled_text"]), ("!disabled", p["on_primary"])])
    style.configure("Danger.TButton", foreground=p["red"])
    style.map("Danger.TButton", foreground=[("disabled", p["disabled_text"]), ("!disabled", p["red"])])
    style.configure("Small.TButton", padding=(9, 5), font=("Microsoft YaHei UI", 9))
    # Current workspaces use one density. Keep legacy style names untouched.
    style.configure("Workspace.TButton", padding=(6, 3), font=("Microsoft YaHei UI", 9))
    style.configure("Workspace.Primary.TButton", padding=(10, 5), font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Workspace.TEntry", padding=4, font=("Microsoft YaHei UI", 9))
    style.configure("Workspace.TCombobox", padding=3, font=("Microsoft YaHei UI", 9))
    style.configure("Workspace.TSpinbox", padding=3, font=("Microsoft YaHei UI", 9))
    style.configure("Theme.TButton", background=p["accent_soft"], foreground=p["accent"],
                    padding=(10, 7), font=("Microsoft YaHei UI", 9, "bold"))
    style.map("Theme.TButton", foreground=[("disabled", p["disabled_text"]), ("!disabled", p["accent"])])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, padding=7 if name == "TEntry" else 6,
                        fieldbackground=p["surface"], background=p["surface"], foreground=p["text"],
                        insertcolor=p["text"], arrowcolor=p["muted"], bordercolor=p["border"],
                        selectbackground=p["selection"], selectforeground=p["selected_text"])
        style.map(name, fieldbackground=[("disabled", p["disabled"]), ("readonly", p["surface"])],
                  background=[("disabled", p["disabled"]), ("active", p["button_hover"])],
                  foreground=[("disabled", p["disabled_text"]), ("readonly", p["text"])],
                  arrowcolor=[("disabled", p["disabled_text"]), ("active", p["text"])],
                  bordercolor=[("focus", p["focus"])], lightcolor=[], darkcolor=[])
    style.configure("ComboboxPopdownFrame", background=p["surface"], bordercolor=p["border"])
    style.configure("TNotebook", borderwidth=0, background=p["background"])
    style.configure("TNotebook.Tab", padding=(24, 12), background=p["background"], foreground=p["muted"],
                    bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"])
    style.map("TNotebook.Tab", background=[("selected", p["surface"]), ("active", p["neutral_soft"])],
              foreground=[("disabled", p["disabled_text"]), ("selected", p["accent"]), ("active", p["text"])],
              bordercolor=[("focus", p["focus"])], lightcolor=[], darkcolor=[], padding=[])
    style.configure("Treeview", rowheight=34, background=p["surface"], fieldbackground=p["surface"],
                    foreground=p["text"], borderwidth=0)
    style.configure("Treeview.Heading", padding=(9, 10), background=p["neutral_soft"], foreground=p["muted"],
                    font=("Microsoft YaHei UI", 9, "bold"), relief="flat")
    style.map("Treeview.Heading", background=[("active", p["button_hover"]), ("pressed", p["button_pressed"])],
              foreground=[("active", p["text"])], lightcolor=[], darkcolor=[])
    style.map("Treeview", background=[("selected", p["selection"]), ("disabled", p["disabled"])],
              foreground=[("selected", p["selected_text"]), ("disabled", p["disabled_text"])])
    style.configure("Horizontal.TProgressbar", troughcolor=p["trough"], background=p["primary"],
                    borderwidth=0, thickness=8, lightcolor=p["primary"], darkcolor=p["primary"])
    style.configure("TCheckbutton", background=p["surface"], foreground=p["muted"],
                    indicatorbackground=p["surface"], indicatorforeground=p["text"], focuscolor=p["focus"])
    style.map("TCheckbutton", background=[("active", p["surface"]), ("disabled", p["surface"])],
              foreground=[("disabled", p["disabled_text"]), ("active", p["text"])],
              indicatorbackground=[("disabled", p["disabled"]), ("selected", p["primary"])],
              indicatorforeground=[("selected", p["on_primary"])])
    for name in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=p["scrollbar"], troughcolor=p["background"],
                        arrowcolor=p["text"], bordercolor=p["border"],
                        lightcolor=p["scrollbar"], darkcolor=p["scrollbar"])
        style.map(name, background=[("disabled", p["disabled"]), ("pressed", p["scrollbar_hover"]), ("active", p["scrollbar_hover"])],
                  arrowcolor=[("disabled", p["disabled_text"])],
                  lightcolor=[("active", p["scrollbar_hover"])], darkcolor=[("active", p["scrollbar_hover"])])
    style.configure("TPanedwindow", background=p["background"])
    style.configure("Sash", background=p["border"], sashthickness=6)
    style.configure("TSeparator", background=p["border"])


def configure_combobox_popdown(widget: ttk.Combobox, palette: Mapping[str, str]) -> None:
    """Also repaint already-created Tcl listboxes, not only future popdowns."""
    popdown = widget.tk.call("ttk::combobox::PopdownWindow", str(widget))
    widget.tk.call(f"{popdown}.f.l", "configure",
                   "-background", palette["surface"], "-foreground", palette["text"],
                   "-selectbackground", palette["selection"], "-selectforeground", palette["selected_text"],
                   "-highlightbackground", palette["border"], "-highlightcolor", palette["focus"],
                   "-borderwidth", 0)
