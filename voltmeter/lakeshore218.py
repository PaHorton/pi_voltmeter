"""Read-only serial access to a Lake Shore Model 218 temperature monitor."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from enum import IntFlag
from typing import Protocol


class LakeShore218Error(Exception):
    """Base error for Model 218 communication and protocol failures."""


class ReadTimeoutError(LakeShore218Error):
    """The instrument did not return a complete response before the timeout."""


class ProtocolError(LakeShore218Error):
    """The instrument returned a malformed or unexpected response."""


class TransportError(LakeShore218Error):
    """The serial transport failed."""


class ReadingStatus(IntFlag):
    TEMP_UNDER_RANGE = 16
    TEMP_OVER_RANGE = 32
    UNITS_UNDER_RANGE = 64
    UNITS_OVER_RANGE = 128


_KNOWN_STATUS_MASK = int(
    ReadingStatus.TEMP_UNDER_RANGE
    | ReadingStatus.TEMP_OVER_RANGE
    | ReadingStatus.UNITS_UNDER_RANGE
    | ReadingStatus.UNITS_OVER_RANGE
)
_READING_FORMAT = re.compile(r"[+-]\d+\.\d{3}\Z")
_STATUS_FORMAT = re.compile(r"\d+\Z")
_RESPONSE_LIMIT = 128
_MIN_COMMAND_INTERVAL = 0.05


@dataclass(frozen=True)
class SerialSettings:
    """Explicit Model 218 serial framing and finite read timeout.

    The manual specifies 7 data bits, odd parity, one stop bit, and baud rates
    of 300, 1200, or 9600.
    """

    baudrate: int
    bytesize: int
    parity: str
    stopbits: int
    timeout: float

    def __post_init__(self) -> None:
        if type(self.baudrate) is not int or self.baudrate not in (300, 1200, 9600):
            raise ValueError("baudrate must be one of 300, 1200, or 9600")
        if type(self.bytesize) is not int or self.bytesize != 7:
            raise ValueError("bytesize must be 7")
        if self.parity != "O":
            raise ValueError("parity must be odd ('O')")
        if type(self.stopbits) is not int or self.stopbits != 1:
            raise ValueError("stopbits must be 1")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number of seconds")


class Transport(Protocol):
    """Transport interface; reads must honor the supplied finite timeout."""

    def write(self, data: bytes) -> int | None: ...

    def read(self, size: int, timeout: float) -> bytes: ...

    def close(self) -> None: ...


class _PySerialTransport:
    def __init__(self, port: str, settings: SerialSettings) -> None:
        try:
            import serial
        except ImportError as exc:
            raise ImportError(
                "pyserial is required for a real serial port; install it with `pip install pyserial`"
            ) from exc

        self._serial = serial.Serial(
            port=port,
            baudrate=settings.baudrate,
            bytesize=settings.bytesize,
            parity=settings.parity,
            stopbits=settings.stopbits,
            timeout=settings.timeout,
            write_timeout=settings.timeout,
        )

    def write(self, data: bytes) -> int:
        return self._serial.write(data)

    def read(self, size: int, timeout: float) -> bytes:
        self._serial.timeout = timeout
        return self._serial.read(size)

    def close(self) -> None:
        self._serial.close()


@dataclass(frozen=True)
class Reading:
    """One Kelvin reading and its instrument-reported validity information."""

    input_number: int
    kelvin: float | None
    status: int
    status_flags: tuple[ReadingStatus, ...]
    unknown_status_bits: int
    raw_reading: str
    error: str | None

    @property
    def valid(self) -> bool:
        return self.kelvin is not None and self.status == 0 and self.error is None


class LakeShore218:
    """Read one configured input from a Model 218 using documented queries only.

    ``port``, ``input_number``, and all ``serial_settings`` are required even
    when a test or application injects its own transport. Injected transports
    implement ``write(bytes)``, ``read(size, timeout)``, and ``close()``.
    """

    def __init__(
        self,
        port: str,
        input_number: int,
        serial_settings: SerialSettings,
        *,
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(port, str) or not port.strip():
            raise ValueError("port must be a non-empty serial port name")
        if type(input_number) is not int or not 1 <= input_number <= 8:
            raise ValueError("input_number must be an integer from 1 through 8")
        if not isinstance(serial_settings, SerialSettings):
            raise TypeError("serial_settings must be an explicit SerialSettings instance")

        self.port = port
        self.input_number = input_number
        self.serial_settings = serial_settings
        self._transport = transport if transport is not None else _PySerialTransport(port, serial_settings)
        self._lock = threading.Lock()
        self._next_command_at = 0.0
        self._closed = False

    def read_kelvin(self) -> Reading:
        """Return the configured input's Kelvin value and reading status.

        A nonzero status or malformed numeric reading is returned explicitly
        as ``valid=False``; communication and framing failures raise errors.
        """
        with self._lock:
            status_text = self._query(f"RDGST? {self.input_number}")
            if not _STATUS_FORMAT.fullmatch(status_text):
                raise ProtocolError(f"invalid RDGST? response: {status_text!r}")
            status = int(status_text)
            if status > 255:
                raise ProtocolError(f"RDGST? status is outside the documented byte range: {status}")

            raw_reading = self._query(f"KRDG? {self.input_number}")
            flags = tuple(flag for flag in ReadingStatus if status & int(flag))
            unknown_bits = status & (0xFF ^ _KNOWN_STATUS_MASK)
            value: float | None = None
            error: str | None = None
            if not _READING_FORMAT.fullmatch(raw_reading):
                error = f"invalid KRDG? reading format: {raw_reading!r}"
            else:
                parsed_value = float(raw_reading)
                if math.isfinite(parsed_value):
                    value = parsed_value
                else:
                    error = f"non-finite KRDG? reading: {raw_reading!r}"

            if status:
                status_names = [flag.name for flag in flags]
                if unknown_bits:
                    status_names.append(f"UNKNOWN_BITS_0x{unknown_bits:02X}")
                status_error = f"instrument status {status} ({', '.join(status_names)})"
                error = f"{error}; {status_error}" if error else status_error

            return Reading(
                input_number=self.input_number,
                kelvin=value,
                status=status,
                status_flags=flags,
                unknown_status_bits=unknown_bits,
                raw_reading=raw_reading,
                error=error,
            )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._transport.close()
                self._closed = True

    def __enter__(self) -> LakeShore218:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _query(self, command: str) -> str:
        if self._closed:
            raise LakeShore218Error("driver is closed")
        wait = self._next_command_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        try:
            written = self._transport.write(command.encode("ascii") + b"\r\n")
            if written is not None and written != len(command) + 2:
                raise TransportError("serial transport performed a partial write")
            return self._read_response()
        except LakeShore218Error:
            raise
        except Exception as exc:
            raise TransportError("serial transport failed") from exc
        finally:
            self._next_command_at = time.monotonic() + _MIN_COMMAND_INTERVAL

    def _read_response(self) -> str:
        deadline = time.monotonic() + self.serial_settings.timeout
        response = bytearray()
        while len(response) < _RESPONSE_LIMIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadTimeoutError("timed out waiting for a Model 218 response")
            try:
                chunk = self._transport.read(1, remaining)
            except Exception as exc:
                raise TransportError("serial transport read failed") from exc
            if not chunk:
                raise ReadTimeoutError("timed out waiting for a Model 218 response")
            if len(chunk) != 1:
                raise ProtocolError("transport returned more data than requested")
            response.extend(chunk)
            if chunk == b"\n":
                if not response.endswith(b"\r\n"):
                    raise ProtocolError("response must end with the documented CR-LF terminator")
                try:
                    return response[:-2].decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ProtocolError("response contains non-ASCII data") from exc
        raise ProtocolError(f"response exceeded {_RESPONSE_LIMIT} bytes")
