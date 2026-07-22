from __future__ import annotations
import os
from typing import Optional

# moonraker.components.gpio requests GPIO lines via periphery.GPIO
# (a character device, eg. /dev/gpiochip0), rather than the libgpiod
# bindings used in older Moonraker releases.  This mock stands in for
# periphery.GPIO so tests never touch real hardware.

class MockGpioEvent:
    def __init__(self, edge: str) -> None:
        self.edge = edge
        self.timestamp = 0


class MockPeripheryGPIO:
    def __init__(self,
                 path: str,
                 line: int,
                 direction: str,
                 edge: str = "none",
                 bias: str = "default",
                 drive: str = "default",
                 inverted: bool = False,
                 label: Optional[str] = None
                 ) -> None:
        self.path = path
        self.line = line
        self.edge = edge
        self.bias = bias
        self.drive = drive
        self.inverted = inverted
        self.label = label
        self._value = direction == "high"
        self._closed = False
        self._read_fd, self._write_fd = os.pipe2(os.O_NONBLOCK)

    @property
    def fd(self) -> int:
        return self._read_fd

    def read(self) -> bool:
        return self._value

    def write(self, value: bool) -> None:
        self._value = bool(value)

    def read_event(self) -> MockGpioEvent:
        try:
            data = os.read(self._read_fd, 64)
        except Exception:
            data = b""
        if data:
            self._value = bool(data[-1])
        return MockGpioEvent("rising" if self._value else "falling")

    def simulate_edge(self, value: bool) -> None:
        os.write(self._write_fd, bytes([int(value)]))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except Exception:
                pass
