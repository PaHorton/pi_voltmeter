import math
import time
from datetime import datetime, timezone

import pytest

from voltmeter.config import DatabaseConfig
from voltmeter.sinks import ChannelReading, Reading
from voltmeter.tigerdata import TigerDataSink, build_sink


class FakeDb:
    def __init__(self):
        self.rows = []
        self.statements = []
        self.fail = False
        self.connects = 0


class FakeConn:
    def __init__(self, db):
        self.db = db
        self._pending = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.db.rows.extend(self._pending)
        self._pending = []

    def rollback(self):
        self._pending = []

    def close(self):
        pass


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        if self.conn.db.fail:
            raise ConnectionError("down")
        self.conn.db.statements.append(sql)

    def executemany(self, sql, rows):
        if self.conn.db.fail:
            raise ConnectionError("down")
        self.conn._pending.extend(rows)


def make(db, **kw):
    cfg = DatabaseConfig(enabled=True, host="h", batch_size=kw.pop("batch_size", 4),
                         flush_interval_s=0.05, **kw)

    def connect():
        db.connects += 1
        if db.fail:
            raise ConnectionError("down")
        return FakeConn(db)

    return TigerDataSink(cfg, connect, "pi-1")


def reading(v=1.0, value=100.0):
    ch = ChannelReading(v, value, "degC", "lm35")
    return Reading(datetime.now(timezone.utc), [ch, ch])


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_rows_inserted_one_per_channel_and_schema_created():
    db = FakeDb()
    sink = make(db)
    sink.write(reading())
    assert wait_for(lambda: len(db.rows) == 2)
    sink.close()
    assert db.rows[0][1:] == ("pi-1", 0, 1.0, 100.0, "degC", "lm35")
    assert db.rows[1][2] == 1
    assert any("CREATE TABLE IF NOT EXISTS readings" in s for s in db.statements)
    assert any("create_hypertable" in s for s in db.statements)


def test_nan_becomes_null():
    db = FakeDb()
    sink = make(db)
    sink.write(reading(value=math.nan))
    assert wait_for(lambda: len(db.rows) == 2)
    sink.close()
    assert db.rows[0][4] is None


def test_outage_buffers_then_recovers_without_loss():
    db = FakeDb()
    db.fail = True
    sink = make(db)
    for _ in range(5):
        sink.write(reading())
    assert wait_for(lambda: db.connects >= 1)
    assert sink.pending == 10 and not db.rows
    db.fail = False
    assert wait_for(lambda: len(db.rows) == 10, timeout=10)
    sink.close()
    assert sink.pending == 0


def test_buffer_overflow_drops_oldest():
    db = FakeDb()
    db.fail = True
    sink = make(db, max_buffer=6)
    for _ in range(5):
        sink.write(reading())
    assert sink.pending == 6
    sink.close()


def test_close_flushes_remaining():
    db = FakeDb()
    sink = make(db, batch_size=100)
    sink.write(reading())
    sink.close()
    assert len(db.rows) == 2


def test_rejects_bad_table_name():
    with pytest.raises(ValueError):
        TigerDataSink(DatabaseConfig(table="x; drop table y"), lambda: None, "d")


def test_build_sink_validation(monkeypatch):
    with pytest.raises(ValueError, match="host"):
        build_sink(DatabaseConfig(enabled=True))
    monkeypatch.delenv("VOLTMETER_DB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="VOLTMETER_DB_PASSWORD"):
        build_sink(DatabaseConfig(enabled=True, host="h"))
