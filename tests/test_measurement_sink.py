import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from voltmeter.measurement_sink import MeasurementSink
from voltmeter.measurements import Measurement


class FakeDb:
    def __init__(self):
        self.rows = {}
        self.statements = []
        self.batches = []
        self.fail_connect = False
        self.fail_commit_after_write = False
        self.connects = 0
        self.closed = 0
        self.started = None
        self.release = None

    def connect(self):
        self.connects += 1
        if self.fail_connect:
            raise ConnectionError("offline")
        return FakeConn(self)


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def executemany(self, sql, rows):
        self.db.statements.append(sql)
        self.db.batches.append(len(rows))
        if self.db.started:
            self.db.started.set()
            assert self.db.release.wait(5)
        self.rows.extend(rows)

    def commit(self):
        for row in self.rows:
            self.db.rows.setdefault(row[:5], row)
        self.rows.clear()
        if self.db.fail_commit_after_write:
            self.db.fail_commit_after_write = False
            raise ConnectionError("commit result lost")

    def close(self):
        self.db.closed += 1


def sample(n=0, **changes):
    measurement = Measurement(
        time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=n),
        logger_id="logger", system_id="system", instrument_id="instrument",
        sensor_id="sensor", native_value=1.25, native_unit="V",
        value=4.5, unit="Pa", quality="good", conversion="calibration-v1",
    )
    return replace(measurement, **changes)


def make(db, **kwargs):
    options = dict(batch_size=3, max_buffer=20, flush_interval_s=0.02,
                   retry_initial_s=0.02, retry_max_s=0.04)
    options.update(kwargs)
    return MeasurementSink(db.connect, **options)


def wait_for(predicate):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_stores_all_fields_and_quality_without_initializing_schema():
    db = FakeDb()
    sink = make(db, batch_size=10, flush_interval_s=60)
    measurements = [
        sample(),
        sample(1, native_value=None, value=None, quality="unavailable", conversion=None),
        sample(2, native_value=2.0, value=None, quality="invalid"),
    ]
    for measurement in measurements:
        sink.write(measurement)
    sink.close()
    assert list(db.rows.values()) == [
        (m.time, m.logger_id, m.system_id, m.instrument_id, m.sensor_id,
         m.native_value, m.native_unit, m.value, m.unit, m.quality, m.conversion)
        for m in measurements
    ]
    assert db.batches == [3]
    assert len(db.statements) == 1
    assert "ON CONFLICT (time, logger_id, system_id, instrument_id, sensor_id) DO NOTHING" in db.statements[0]
    assert db.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        sink.write(sample())


def test_ambiguous_commit_retries_idempotently(caplog):
    caplog.set_level(logging.INFO)
    db = FakeDb()
    db.fail_commit_after_write = True
    sink = make(db, batch_size=1)
    try:
        sink.write(sample())
        assert wait_for(lambda: db.connects >= 2 and sink.pending == 0
                        and "connection recovered" in caplog.text)
        assert len(db.rows) == 1
        assert db.batches == [1, 1]
    finally:
        sink.close()


def test_outage_overflow_and_recovery(caplog):
    db = FakeDb()
    db.fail_connect = True
    sink = make(db, batch_size=2, max_buffer=3)
    try:
        for n in range(5):
            sink.write(sample(n))
        assert wait_for(lambda: db.connects > 0)
        assert sink.pending == 3
        assert sink.dropped == 2
        assert "buffer full" in caplog.text
        db.fail_connect = False
        assert wait_for(lambda: sink.pending == 0)
        assert [row[0] for row in db.rows.values()] == [sample(n).time for n in (2, 3, 4)]
        assert db.batches == [2, 1]
    finally:
        sink.close()


def test_concurrent_overflow_does_not_remove_new_rows_after_commit():
    db = FakeDb()
    db.started, db.release = threading.Event(), threading.Event()
    sink = make(db, batch_size=2, max_buffer=2)
    try:
        sink.write(sample(0))
        sink.write(sample(1))
        assert db.started.wait(3)
        sink.write(sample(2))
        sink.write(sample(3))
        assert sink.pending == 2
        db.release.set()
        assert wait_for(lambda: sink.pending == 0)
        assert [row[0] for row in db.rows.values()] == [sample(n).time for n in range(4)]
        assert sink.dropped == 2
    finally:
        db.release.set()
        sink.close()


def test_close_reports_failed_final_flush(caplog):
    db = FakeDb()
    db.fail_connect = True
    sink = make(db, batch_size=10, flush_interval_s=60)
    sink.write(sample())
    with caplog.at_level(logging.WARNING):
        sink.close()
    assert sink.pending == 1
    assert "final flush failed; 1 rows lost" in caplog.text


@pytest.mark.parametrize("kwargs", [
    {"batch_size": 0}, {"max_buffer": 0}, {"max_buffer": True},
    {"flush_interval_s": float("nan")}, {"retry_initial_s": 0},
    {"retry_initial_s": 2, "retry_max_s": 1},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        make(FakeDb(), **kwargs)
