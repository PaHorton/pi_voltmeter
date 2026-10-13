"""128x64 UI: renders to a PIL image, driven by button events. No hardware imports."""
from __future__ import annotations

import enum
import math
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from .settings import N_CHOICES, NUM_CHANNELS, PERIOD_CHOICES, RANGE_CHOICES, Settings
from .sinks import Reading

WIDTH, HEIGHT = 128, 64


class Button(enum.Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    SELECT = "select"  # joystick press
    A = "a"            # opens menu / selects
    B = "b"            # back


@dataclass
class Status:
    usb_present: bool = False
    logging_active: bool = False


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def format_value(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        return "ERR"
    return f"{value:.5g}"


def _cycle(options: Sequence, current, delta: int):
    try:
        i = options.index(current)
    except ValueError:
        i = 0
    return options[(i + delta) % len(options)]


class Ui:
    def __init__(self, settings: Settings, formula_names: Callable[[], List[str]],
                 on_change: Callable[[], None] = lambda: None):
        self.settings = settings
        self._formula_names = formula_names
        self._on_change = on_change
        self.in_menu = False
        self.cursor = 0
        self._toast: Optional[str] = None
        self._toast_until = 0.0
        self._small, self._big = _font(10), _font(20)
        self._items = self._build_items()

    def toast(self, text: str, seconds: float = 2.5) -> None:
        self._toast, self._toast_until = text, time.monotonic() + seconds

    def _build_items(self):
        s = self.settings
        items = []
        for ch in range(NUM_CHANNELS):
            items.append((
                f"Ch{ch}",
                lambda ch=ch: s.formulas[ch],
                lambda d, ch=ch: s.formulas.__setitem__(
                    ch, _cycle(self._formula_names(), s.formulas[ch], d)),
            ))
        items += [
            ("N rec", lambda: f"{s.n}s",
             lambda d: setattr(s, "n", _cycle(N_CHOICES, s.n, d))),
            ("Range", lambda: f"{s.range_v:g}V",
             lambda d: setattr(s, "range_v", _cycle(RANGE_CHOICES, s.range_v, d))),
            ("Display", lambda: f"{s.period_s:g}s",
             lambda d: setattr(s, "period_s", _cycle(PERIOD_CHOICES, s.period_s, d))),
            ("USB log", lambda: "ON" if s.usb_logging else "OFF",
             lambda d: setattr(s, "usb_logging", not s.usb_logging)),
        ]
        return items

    # ---- input -------------------------------------------------------
    def handle(self, button: Button) -> None:
        if not self.in_menu:
            if button in (Button.A, Button.SELECT):
                self.in_menu, self.cursor = True, 0
            return
        if button is Button.B:
            self.in_menu = False
        elif button is Button.UP:
            self.cursor = (self.cursor - 1) % len(self._items)
        elif button is Button.DOWN:
            self.cursor = (self.cursor + 1) % len(self._items)
        elif button in (Button.LEFT, Button.RIGHT, Button.A, Button.SELECT):
            delta = -1 if button is Button.LEFT else 1
            self._items[self.cursor][2](delta)
            self._on_change()

    # ---- drawing -----------------------------------------------------
    def render(self, reading: Optional[Reading], status: Status) -> Image.Image:
        img = Image.new("1", (WIDTH, HEIGHT), 0)
        draw = ImageDraw.Draw(img)
        if self.in_menu:
            self._draw_menu(draw)
        else:
            self._draw_main(draw, reading, status)
        if self._toast and time.monotonic() < self._toast_until:
            draw.rectangle((0, HEIGHT - 12, WIDTH, HEIGHT), fill=1)
            draw.text((2, HEIGHT - 12), self._toast[:21], font=self._small, fill=0)
        return img

    def _draw_main(self, draw, reading: Optional[Reading], status: Status) -> None:
        tag = "REC" if status.logging_active else ("USB" if status.usb_present else "")
        for ch in range(NUM_CHANNELS):
            y = ch * 32
            draw.text((0, y), f"CH{ch} {self.settings.formulas[ch]}"[:16],
                      font=self._small, fill=1)
            if ch == 0 and tag:
                draw.text((WIDTH - 20, y), tag, font=self._small, fill=1)
            if reading is None:
                text = "--"
            else:
                c = reading.channels[ch]
                text = f"{format_value(c.value)} {c.unit}"
            draw.text((0, y + 10), text, font=self._big, fill=1)

    def _draw_menu(self, draw) -> None:
        rows, row_h = 6, 10
        top = max(0, self.cursor - rows + 1)
        for i, (label, get, _set) in enumerate(self._items[top:top + rows]):
            y = i * row_h
            selected = top + i == self.cursor
            if selected:
                draw.rectangle((0, y, WIDTH, y + row_h - 1), fill=1)
            draw.text((2, y), f"{label}: {get()}"[:21], font=self._small,
                      fill=0 if selected else 1)
