"""Bridge MQTT report parser tests."""

from __future__ import annotations

from bridge.parser import PrinterAccumulator


def test_apply_partial_reports_accumulates():
    acc = PrinterAccumulator(slug="P1", serial="ABC")
    acc.apply({"gcode_state": "RUNNING", "mc_percent": 5})
    acc.apply({"mc_percent": 12, "subtask_name": "sub-12345678-x.3mf"})
    snap = acc.snapshot()
    assert snap["status"] == "PRINTING"
    assert snap["percent"] == 12
    assert snap["current_file"] == "sub-12345678-x.3mf"


def test_gcode_state_normalization():
    acc = PrinterAccumulator(slug="P1", serial="ABC")
    for bambu, ours in [
        ("IDLE", "IDLE"),
        ("PREPARE", "PREPARING"),
        ("RUNNING", "PRINTING"),
        ("PAUSE", "PAUSED"),
        ("FINISH", "FINISHED"),
        ("FAILED", "FAILED"),
    ]:
        acc.apply({"gcode_state": bambu})
        assert acc.snapshot()["status"] == ours


def test_wifi_signal_parsed_from_dbm_string():
    acc = PrinterAccumulator(slug="P1", serial="ABC")
    acc.apply({"wifi_signal": "-52dBm"})
    assert acc.snapshot()["wifi_signal"] == -52


def test_temps_parsed_as_floats():
    acc = PrinterAccumulator(slug="P1", serial="ABC")
    acc.apply(
        {
            "nozzle_temper": 215.4,
            "nozzle_target_temper": 215.0,
            "bed_temper": 60.1,
            "bed_target_temper": 60.0,
        }
    )
    snap = acc.snapshot()
    assert snap["nozzle_temp"] == 215.4
    assert snap["bed_target"] == 60.0


def test_unknown_gcode_state_defaults_to_idle():
    acc = PrinterAccumulator(slug="P1", serial="ABC")
    acc.apply({"gcode_state": "WHATEVER"})
    assert acc.snapshot()["status"] == "IDLE"


def test_non_dict_input_ignored():
    acc = PrinterAccumulator(slug="P1", serial="ABC")
    acc.apply("not a dict")  # type: ignore[arg-type]
    snap = acc.snapshot()
    assert snap["percent"] is None


def test_layer_progress():
    acc = PrinterAccumulator(slug="P1", serial="ABC")
    acc.apply({"layer_num": 42, "total_layer_num": 100})
    snap = acc.snapshot()
    assert snap["layer"] == 42
    assert snap["total_layers"] == 100
