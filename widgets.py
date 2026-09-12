"""Custom rounded Tk widgets with press feedback for the Photo Culler toolbar."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable


def _rounded_rect_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
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
    """Shared rounded-rect chrome for buttons and toggles."""

    def __init__(
        self,
        master,
        text: str = "",
        *,
        width: int = 0,
        height: int = 0,
        bg: str = "#202329",
        fill: str = "#2c313a",
        fill_hover: str = "#353b46",
        fill_press: str = "#4f9cff",
        outline: str = "#3a4150",
        fg: str = "#e7e9ed",
        fg_press: str = "#0d1117",
        font=("Segoe UI", 10),
        bold: bool = False,
        padx: int = 12,
        pady: int = 7,
        radius: int = 10,
        command: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        super().__init__(master, bg=bg, **kwargs)
        self._text = text
        self._fill = fill
        self._fill_hover = fill_hover
        self._fill_press = fill_press
        self._outline = outline
        self._fg = fg
        self._fg_press = fg_press
        self._font = (font[0], font[1], "bold") if bold else font
        self._padx = padx
        self._pady = pady
        self._radius = radius
        self._command = command
        self._enabled = True
        self._pressed = False
        self._hover = False
        self._base_w = max(width, 1)
        self._base_h = max(height, 1)
        self._measure_text()
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Key-space>", self._on_space_key)
        self.bind("<Key-Return>", self._on_space_key)
        self.configure(takefocus=1)

    def _measure_text(self) -> None:
        self._request_size()

    def _request_size(self) -> None:
        try:
            font = tkfont.Font(font=self._font)
            self._text_w = font.measure(self._text)
            self._text_h = font.metrics("linespace")
        except Exception:
            self._text_w = max(48, len(self._text) * 8)
            self._text_h = 16
        super().configure(
            width=self._text_w + self._padx * 2,
            height=max(self._base_h, self._text_h + self._pady * 2),
        )

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
        if text is not None:
            self._text = text
            self._request_size()
        if command is not None:
            self._command = command
        if state is not None:
            self.set_enabled(str(state) != "disabled")
        if kw:
            super().configure(**kw)
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._pressed = False
            self._hover = False
        self._redraw()

    def state(self, flags) -> None:
        # Compatible with ttk.Button.state(["!disabled"]) / ["disabled"]
        for flag in flags:
            if flag in ("disabled",):
                self.set_enabled(False)
            elif flag in ("!disabled", "normal"):
                self.set_enabled(True)

    def invoke(self) -> None:
        if self._enabled and self._command is not None:
            self._command()

    def _on_enter(self, _event) -> None:
        if self._enabled:
            self._hover = True
            self.configure(cursor="hand2")
            self._redraw()

    def _on_leave(self, _event) -> None:
        self._hover = False
        self._pressed = False
        self.configure(cursor="arrow")
        self._redraw()

    def _on_press(self, _event) -> None:
        if not self._enabled:
            return
        self.focus_set()
        self._pressed = True
        self._redraw()

    def _on_release(self, event) -> None:
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        inside = (
            0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        )
        self._redraw()
        if was_pressed and inside:
            self._flash_then_invoke()

    def _on_space_key(self, _event) -> str:
        if self._enabled:
            self._flash_then_invoke()
        return "break"

    def _flash_then_invoke(self) -> None:
        """Brief press-color pulse, then run the command."""
        if not self._enabled:
            return
        self._pressed = True
        self._redraw()

        def _done() -> None:
            self._pressed = False
            self._redraw()
            if self._command is not None:
                self._command()

        self.after(70, _done)

    def _current_fill(self) -> str:
        if not self._enabled:
            return "#252930"
        if self._pressed:
            return self._fill_press
        if self._hover:
            return self._fill_hover
        return self._fill

    def _current_fg(self) -> str:
        if not self._enabled:
            return "#6b7280"
        if self._pressed:
            return self._fg_press
        return self._fg

    def _redraw(self) -> None:
        self.delete("all")
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        pad = 1
        y_off = 1 if self._pressed and self._enabled else 0
        fill = self._current_fill()
        outline = self._fill_press if (self._hover and self._enabled and not self._pressed) else self._outline
        points = _rounded_rect_points(pad, pad + y_off, w - pad, h - pad + y_off, self._radius)
        self.create_polygon(
            points,
            smooth=True,
            fill=fill,
            outline=outline,
            width=1,
        )
        self.create_text(
            w / 2,
            h / 2 + y_off,
            text=self._text,
            fill=self._current_fg(),
            font=self._font,
        )


class RoundedButton(_RoundedWidget):
    """Primary toolbar button with hover + press feedback."""

    def __init__(self, master, text: str = "", command=None, **kwargs) -> None:
        accent = kwargs.pop("accent", False)
        danger = kwargs.pop("danger", False)
        if accent:
            kwargs.setdefault("fill", "#2f6fed")
            kwargs.setdefault("fill_hover", "#3b7cf5")
            kwargs.setdefault("fill_press", "#8eb6ff")
            kwargs.setdefault("fg", "#f4f7ff")
            kwargs.setdefault("fg_press", "#0d1117")
            kwargs.setdefault("outline", "#2f6fed")
            kwargs.setdefault("bold", True)
        elif danger:
            kwargs.setdefault("fill", "#3a2a2e")
            kwargs.setdefault("fill_hover", "#4a3339")
            kwargs.setdefault("fill_press", "#e57373")
            kwargs.setdefault("fg", "#f0d6d8")
            kwargs.setdefault("fg_press", "#1a0d0f")
            kwargs.setdefault("outline", "#5a3a42")
        super().__init__(master, text=text, command=command, **kwargs)


class RoundedToggle(_RoundedWidget):
    """Pill toggle used for 只看保留."""

    def __init__(
        self,
        master,
        text: str,
        variable: tk.BooleanVar,
        command=None,
        **kwargs,
    ) -> None:
        self._variable = variable
        self._user_command = command
        super().__init__(master, text=text, command=self._toggle, **kwargs)
        self._variable.trace_add("write", lambda *_: self._redraw())

    def _toggle(self) -> None:
        self._variable.set(not self._variable.get())
        if self._user_command is not None:
            self._user_command()

    def _current_fill(self) -> str:
        if not self._enabled:
            return "#252930"
        if self._variable.get():
            if self._pressed:
                return "#8eb6ff"
            if self._hover:
                return "#3b7cf5"
            return "#2f6fed"
        return super()._current_fill()

    def _current_fg(self) -> str:
        if self._enabled and self._variable.get() and not self._pressed:
            return "#f4f7ff"
        return super()._current_fg()
