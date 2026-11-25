"""Unit tests for classic_protocol.py (no Home Assistant needed).

Run: python3 -m pytest tests/  or  python3 tests/test_classic_protocol.py
Values come from the device diagnostics of 2026-11-25.
"""

from datetime import datetime, time
import importlib.util
import sys
from pathlib import Path

_PATH = (
    Path(__file__).parent.parent
    / "custom_components/gardena_bluetooth/classic_protocol.py"
)
_SPEC = importlib.util.spec_from_file_location("classic_protocol", _PATH)
cp = importlib.util.module_from_spec(_SPEC)
sys.modules["classic_protocol"] = cp
_SPEC.loader.exec_module(cp)

H = bytes.fromhex


def slot3_raw():
    return {
        1: H("647d0000"),
        2: H("3c000000"),
        3: H("7f"),
        4: H("100f"),
        5: H("01"),
        6: H("0000"),
    }


def test_uuids():
    assert cp.char_uuid(1, 1) == "98bd0c11-0b0e-421a-84e5-ddbf75dc6de4"
    assert cp.char_uuid(3, 6) == "98bd0c36-0b0e-421a-84e5-ddbf75dc6de4"
    assert cp.service_uuid(2) == "98bd0c20-0b0e-421a-84e5-ddbf75dc6de4"


def test_is_classic():
    uuids = {cp.VALVE_STATE_UUID, cp.char_uuid(1, 1)}
    assert cp.is_classic("WATER_COMPUTER", uuids)
    assert not cp.is_classic("PUMP", uuids)
    assert not cp.is_classic("WATER_COMPUTER", uuids | {"98bda001" + cp.BASE})
    assert not cp.is_classic("WATER_COMPUTER", {cp.VALVE_STATE_UUID})


def test_classic_slots():
    uuids = {u for s in (1, 2, 3) for u in cp.slot_uuids(s)} | {cp.char_uuid(4, 1)}
    assert cp.classic_slots(uuids) == [1, 2, 3]


def test_decode_slot():
    s = cp.Schedule.decode(3, slot3_raw())
    assert s.start == time(8, 55)
    assert s.duration == 60
    assert s.weekdays == frozenset(cp.WEEKDAYS)
    assert s.active and s.enabled and s.valve_linked and not s.use_sensor
    assert cp.Schedule.decode(3, {**slot3_raw(), 5: None}) is None


def test_roundtrip_and_plan_match_app():
    s = cp.Schedule.decode(3, slot3_raw())
    assert s.raw() == slot3_raw()
    plan = s.write_plan()
    assert [o for o, _ in plan] == [5, 1, 2, 3, 6, 4, 5]
    assert plan[0][1] == b"\x00" and plan[-1][1] == b"\x01"
    off = s.updated(active=False).write_plan()
    assert [o for o, _ in off] == [5, 1, 2, 3, 6, 4]


def test_wednesday_mask_like_app():
    assert cp.mask_from_weekdays(["wed"]) == 0x04
    assert cp.weekdays_from_mask(0x04) == frozenset({"wed"})


def test_validate():
    s = cp.Schedule.empty(1)
    assert s.validate() is None
    assert s.updated(active=True).validate() is not None
    assert s.updated(active=True, duration=60, weekdays=["mon"]).validate() is None
    assert s.updated(duration=86400).validate() is not None


def test_history_from_device():
    entries = cp.decode_history(
        H("9b9a066ba0a2066b"), H("0200"), H("030000003c000000"), 2
    )
    assert [e.status for e in entries] == ["partially_watered", "watered"]
    assert entries[0].reason == "manual"
    assert entries[1].end == datetime(2026, 11, 25, 8, 56, 0)
    assert entries[1].start == datetime(2026, 11, 25, 8, 55, 0)
    assert (
        cp.decode_history(H("9b9a066b"), H("08"), H("00000000"))[0].status == "skipped"
    )
    assert cp.decode_history(None, None, None) == []


def test_next_run():
    s3 = cp.Schedule.decode(3, slot3_raw())
    s1 = s3.updated().__class__(
        1, time(0, 25), 60, frozenset(cp.WEEKDAYS), True, False, True
    )
    now = datetime(2026, 11, 25, 8, 57)  # Wednesday
    when, sched = cp.next_run([s3, s1], now)
    assert when == datetime(2026, 11, 26, 0, 25) and sched.slot == 1
    wed_only = s3.updated(weekdays=["wed"])
    when, _ = cp.next_run([wed_only], now)
    assert when == datetime(2026, 12, 2, 8, 55)
    assert cp.next_run([s3.updated(active=False)], now) is None


def test_seasonal_and_activation():
    assert cp.reduction_from_raw(0) == 100
    assert cp.reduction_from_raw(100) == 0
    assert cp.raw_from_reduction(0) == 100
    assert cp.raw_from_reduction(30) == 70
    assert cp.activation_reason(False, 1) == "none"
    assert cp.activation_reason(True, 0) == "manual"
    assert cp.activation_reason(True, 1) == "schedule"
    assert cp.activation_reason(None, 1) is None


if __name__ == "__main__":
    for name, func in list(globals().items()):
        if name.startswith("test_"):
            func()
    print("ok")
