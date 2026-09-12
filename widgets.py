"""Rounded toolbar buttons with visible press feedback."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable


def _rounded_rect_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    r = max(0.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    return [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1, x2, y1 + r,
        x2, y2 - r,
        x2, y2, x2 - r, y2,
        x1 + r, y2,
        x1, y2, x1, y2 - r,
        x1, y1 + r,
        x1, y1, x1 + r, y1,
    ]


class _RoundedWidget(tk.Canvas):
    def __init__(
        self,
        master,
        text: str = "",
        *,
        bg: str = "#202329",
        fill: str = "#2c313a",
        fill_hover: str = "#3a4250",
        fill_press: str = "#5aa2ff",
        outline: str = "#4a5568",
        fg: str = "#e7e9ed",
        fg_press: str = "#0d1117",
        font=("Segoe UI", 10),
        bold: bool = False,
        padx: int = 14,
        pady: int = 8,
        radius: int = 12,
        command: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        self._text = text
        self._fill = fill
        self._fill_hover = fill_hover
        self._fill_press = fill_press
        self._outline = outline
        self._fg = fg
        self._fg_press = fg_press
        self._font = (font[0], font[1], "bold") if bold else tuple(font)
        self._padx = padx
        self._pady = pady
        self._radius = radius
        self._command = command
        self._enabled = True
        self._pressed = False
        self._hover = False

        try:
            fnt = tkfont.Font(font=self._font)
            tw, th = fnt.measure(text), fnt.metrics("linespace")
        except Exception:
            tw, th = max(48, len(text) * 9), 16
        w = max(40, tw + padx * 2)
        h = max(28, th + pady * 2)

        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("width", w)
        kwargs.setdefault("height", h)
        super().__init__(master, bg=bg, **kwargs)

        self.bind("<Configure>", self._on_configure)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Key-space>", self._on_key_activate)
        self.bind("<Key-Return>", self._on_key_activate)
        self.configure(takefocus=1)
        self._draw()

    # --- public API compatible with ttk.Button ---

    def cget(self, key: str):
        if key == "text":
            return self._text
        return super().cget(key)

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if cnf:
            kw = {**cnf, **kw}
        text = kw.pop("text", None)
        state = kw.pop("state", None)
        command = kw.pop("command", None)
        cursor = kw.pop("cursor", None)
        if text is not None:
            self._text = str(text)
            try:
                fnt = tkfont.Font(font=self._font)
                tw, th = fnt.measure(self._text), fnt.metrics("linespace")
            except Exception:
                tw, th = max(48, len(self._text) * 9), 16
            tk.Canvas.configure(
                self,
                width=max(40, tw + self._padx * 2),
                height=max(28, th + self._pady * 2),
            )
        if command is not None:
            self._command = command
        if state is not None:
            self.set_enabled(str(state) != "disabled")
        if cursor is not None:
            tk.Canvas.configure(self, cursor=cursor)
        if kw:
            tk.Canvas.configure(self, **kw)
        self._draw()

    config = configure

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._pressed = False
            self._hover = False
        self._draw()

    def state(self, flags) -> None:
        for flag in flags:
            if flag == "disabled":
                self.set_enabled(False)
            elif flag in ("!disabled", "normal"):
                self.set_enabled(True)

    def invoke(self) -> None:
        self._flash_then_invoke()

    # --- interaction ---

    def _on_configure(self, _event=None) -> None:
        self._draw()

    def _on_enter(self, _event) -> None:
        if self._enabled:
            self._hover = True
            tk.Canvas.configure(self, cursor="hand2")
            self._draw()

    def _on_leave(self, _event) -> None:
        self._hover = False
        self._pressed = False
        tk.Canvas.configure(self, cursor="arrow")
        self._draw()

    def _on_press(self, _event) -> None:
        if not self._enabled:
            return
        self.focus_set()
        self._pressed = True
        self._draw()
        # Force immediate paint so the press state is visible this frame.
        self.update_idletasks()

    def _on_release(self, event) -> None:
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._draw()
        self.update_idletasks()
        if was_pressed and inside:
            self._flash_then_invoke()

    def _on_key_activate(self, _event) -> str:
        if self._enabled:
            self._flash_then_invoke()
        return "break"

    def _flash_then_invoke(self) -> None:
        if not self._enabled:
            return
        self._pressed = True
        self._draw()
        self.update_idletasks()

        def _finish() -> None:
            self._pressed = False
            self._draw()
            if self._command is not None:
                self._command()

        # Short hold so the press color is perceptible, then run command.
        self.after(90, _finish)

    # --- painting ---

    def _colors(self) -> tuple[str, str, str]:
        if not self._enabled:
            return "#252930", "#6b7280", "#3a4150"
        if self._pressed:
            return self._fill_press, self._fg_press, self._fill_press
        if self._hover:
            return self._fill_hover, self._fg, self._fill_hover
        return self._fill, self._fg, self._outline

    def _draw(self) -> None:
        self.delete("all")
        w = max(self.winfo_width(), 4)
        h = max(self.winfo_height(), 4)
        # Press: content shifts down 2px for a tactile feel.
        dy = 2 if (self._pressed and self._enabled) else 0
        fill, fg, outline = self._colors()
        pad = 1
        points = _rounded_rect_points(pad, pad + dy, w - pad, h - pad + dy, self._radius)
        self.create_polygon(points, smooth=True, fill=fill, outline=outline, width=1)
        self.create_text(w / 2, h / 2 + dy, text=self._text, fill=fg, font=self._font)


class RoundedButton(_RoundedWidget):
    def __init__(self, master, text: str = "", command=None, **kwargs) -> None:
        accent = kwargs.pop("accent", False)
        danger = kwargs.pop("danger", False)
        if accent:
            kwargs.setdefault("fill", "#2f6fed")
            kwargs.setdefault("fill_hover", "#3d7ff0")
            kwargs.setdefault("fill_press", "#9ec2ff")
            kwargs.setdefault("fg", "#f5f8ff")
            kwargs.setdefault("fg_press", "#0d1117")
            kwargs.setdefault("outline", "#2f6fed")
            kwargs.setdefault("bold", True)
        elif danger:
            kwargs.setdefault("fill", "#4a2c32")
            kwargs.setdefault("fill_hover", "#5c3840")
            kwargs.setdefault("fill_press", "#ef9a9a")
            kwargs.setdefault("fg", "#f3e0e2")
            kwargs.setdefault("fg_press", "#1a0d0f")
            kwargs.setdefault("outline", "#6a4048")
        super().__init__(master, text=text, command=command, **kwargs)


class RoundedToggle(_RoundedWidget):
    def __init__(self, master, text: str, variable: tk.BooleanVar, command=None, **kwargs) -> None:
        self._variable = variable
        self._user_command = command
        super().__init__(master, text=text, command=self._toggle, **kwargs)
        try:
            self._variable.trace_add("write", lambda *_: self._draw())
        except Exception:
            pass

    def _toggle(self) -> None:
        self._variable.set(not bool(self._variable.get()))
        if self._user_command is not None:
            self._user_command()

    def _colors(self) -> tuple[str, str, str]:
        if self._enabled and self._variable.get():
            if self._pressed:
                return "#9ec2ff", "#0d1117", "#9ec2ff"
            if self._hover:
                return "#3d7ff0", "#f5f8ff", "#3d7ff0"
            return "#2f6fed", "#f5f8ff", "#2f6fed"
        return super()._colors()
