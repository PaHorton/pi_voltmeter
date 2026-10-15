"""Raspberry Pi hardware: SSD1306 OLED (Adafruit 128x64 bonnet) and its buttons."""
from __future__ import annotations

import queue
from typing import List

from PIL import Image

from .config import ButtonsConfig, DisplayConfig
from .ui import Button


class Oled:
    def __init__(self, cfg: DisplayConfig = DisplayConfig()):
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306
        self._device = ssd1306(i2c(port=cfg.bus, address=cfg.address), width=128, height=64)

    def show(self, image: Image.Image) -> None:
        self._device.display(image)

    def close(self) -> None:
        self._device.cleanup()


class ButtonPad:
    """Collects presses from gpiozero callback threads into a queue."""

    def __init__(self, cfg: ButtonsConfig = ButtonsConfig()):
        from gpiozero import Button as GpioButton
        self._q: "queue.Queue[Button]" = queue.Queue()
        self._buttons = []
        for button in Button:
            pin = getattr(cfg, button.value)
            if pin is None:
                continue
            gb = GpioButton(pin, pull_up=True, bounce_time=0.03)
            gb.when_pressed = lambda b=button: self._q.put(b)
            self._buttons.append(gb)

    def drain(self) -> List[Button]:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        for b in self._buttons:
            b.close()
