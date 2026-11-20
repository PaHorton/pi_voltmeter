import math

import pytest

from voltmeter.lakeshore218 import (
    LakeShore218,
    ProtocolError,
    ReadTimeoutError,
    ReadingStatus,
    SerialSettings,
)


def settings(**overrides):
    values = {
        "baudrate": 9600,
        "bytesize": 7,
        "parity": "O",
        "stopbits": 1,
        "timeout": 0.1,
    }
    values.update(overrides)
    return SerialSettings(**values)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.pending = bytearray()
        self.writes = []
        self.timeouts = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        if self.responses:
            self.pending.extend(self.responses.pop(0))
        return len(data)

    def read(self, size, timeout):
        self.timeouts.append(timeout)
        if not self.pending:
            return b""
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def close(self):
        self.closed = True


def driver(transport, input_number=3, serial_settings=None):
    return LakeShore218(
        "/dev/ttyUSB0",
        input_number,
        serial_settings or settings(),
        transport=transport,
    )


def test_read_kelvin_sends_only_documented_read_queries():
    transport = FakeTransport([b"0\r\n", b"+4.200\r\n"])
    result = driver(transport).read_kelvin()

    assert result.valid
    assert result.input_number == 3
    assert result.kelvin == pytest.approx(4.2)
    assert result.status == 0
    assert transport.writes == [b"RDGST? 3\r\n", b"KRDG? 3\r\n"]
    assert all(0 < timeout <= 0.1 for timeout in transport.timeouts)


@pytest.mark.parametrize("input_number", [1, 8])
def test_input_boundaries_are_accepted(input_number):
    transport = FakeTransport([b"0\r\n", b"+4.200\r\n"])
    result = driver(transport, input_number=input_number).read_kelvin()

    assert result.valid
    assert result.input_number == input_number
    assert transport.writes == [
        f"RDGST? {input_number}\r\n".encode("ascii"),
        f"KRDG? {input_number}\r\n".encode("ascii"),
    ]


def test_reading_status_is_explicit_and_invalidates_measurement():
    transport = FakeTransport([b"192\r\n", b"+12.345\r\n"])
    result = driver(transport).read_kelvin()

    assert not result.valid
    assert result.kelvin == pytest.approx(12.345)
    assert result.status_flags == (
        ReadingStatus.UNITS_UNDER_RANGE,
        ReadingStatus.UNITS_OVER_RANGE,
    )
    assert result.unknown_status_bits == 0
    assert "instrument status 192" in result.error


def test_invalid_reading_format_is_returned_explicitly():
    transport = FakeTransport([b"0\r\n", b"OVR\r\n"])
    result = driver(transport).read_kelvin()

    assert not result.valid
    assert result.kelvin is None
    assert result.raw_reading == "OVR"
    assert "invalid KRDG? reading format" in result.error


def test_unknown_status_bits_are_preserved():
    transport = FakeTransport([b"1\r\n", b"+1.000\r\n"])
    result = driver(transport).read_kelvin()

    assert not result.valid
    assert result.unknown_status_bits == 1
    assert "UNKNOWN_BITS_0x01" in result.error


def test_missing_response_raises_finite_timeout():
    result_transport = FakeTransport([b""])
    with pytest.raises(ReadTimeoutError):
        driver(result_transport, serial_settings=settings(timeout=0.01)).read_kelvin()
    assert result_transport.timeouts
    assert all(math.isfinite(timeout) and timeout > 0 for timeout in result_transport.timeouts)


@pytest.mark.parametrize("input_number", [0, 9, -1, 1.0, True])
def test_input_must_be_integer_1_through_8(input_number):
    with pytest.raises(ValueError, match="input_number"):
        driver(FakeTransport([]), input_number=input_number)


@pytest.mark.parametrize(
    "overrides",
    [
        {"baudrate": 2400},
        {"bytesize": 8},
        {"parity": "N"},
        {"stopbits": 2},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"timeout": float("nan")},
    ],
)
def test_serial_settings_must_match_supported_framing_and_finite_timeout(overrides):
    with pytest.raises(ValueError):
        settings(**overrides)


def test_injected_transport_does_not_import_optional_pyserial(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "serial":
            raise AssertionError("pyserial should not be imported for an injected transport")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    instrument = driver(FakeTransport([b"0\r\n", b"+1.000\r\n"]))
    assert instrument.read_kelvin().valid


def test_pyserial_is_opened_with_explicit_manual_settings(monkeypatch):
    import sys
    from types import SimpleNamespace

    received = {}

    class FakeSerial:
        def __init__(self, **kwargs):
            received.update(kwargs)

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=FakeSerial))
    LakeShore218(
        "/dev/ttyS1",
        8,
        settings(baudrate=1200, timeout=0.25),
    )

    assert received == {
        "port": "/dev/ttyS1",
        "baudrate": 1200,
        "bytesize": 7,
        "parity": "O",
        "stopbits": 1,
        "timeout": 0.25,
        "write_timeout": 0.25,
    }


def test_response_requires_documented_crlf_terminator():
    transport = FakeTransport([b"0\n"])
    with pytest.raises(ProtocolError, match="CR-LF"):
        driver(transport).read_kelvin()


def test_context_manager_closes_injected_transport():
    transport = FakeTransport([])
    with driver(transport):
        pass
    assert transport.closed
