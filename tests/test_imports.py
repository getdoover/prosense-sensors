"""Smoke tests for the prosense_sensors app.

Validates imports, schema well-formedness, Tags/UI subclassing, and that
the config/UI export entry points run end-to-end.
"""

import json

from pydoover.config import Schema
from pydoover.tags import Tags
from pydoover.ui import UI


def test_import_app():
    from prosense_sensors.application import ProSenseApplication
    assert ProSenseApplication.config_cls is not None
    assert ProSenseApplication.tags_cls is not None
    assert ProSenseApplication.ui_cls is not None


def test_config_schema():
    from prosense_sensors.app_config import ProSenseConfig
    assert issubclass(ProSenseConfig, Schema)

    schema = ProSenseConfig.to_schema()
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    for key in (
        "variant",
        "modbus_config",
        "slave_id",
        "poll_interval_seconds",
        "display_unit",
        "no_comms_timeout_seconds",
    ):
        assert key in schema["properties"], f"{key} missing from config schema"


def test_tags():
    from prosense_sensors.app_tags import ProSenseTags
    assert issubclass(ProSenseTags, Tags)


def test_ui():
    from prosense_sensors.app_ui import ProSenseUI
    assert issubclass(ProSenseUI, UI)


def test_config_export(tmp_path):
    from prosense_sensors.app_config import ProSenseConfig

    fp = tmp_path / "doover_config.json"
    ProSenseConfig.export(fp, "prosense_sensors")

    data = json.loads(fp.read_text())
    assert "prosense_sensors" in data
    assert "config_schema" in data["prosense_sensors"]


def test_ui_export(tmp_path, monkeypatch):
    # Our export() writes to a fixed path under the repo; redirect it to
    # tmp_path by monkey-patching __file__-based path resolution.
    from prosense_sensors import app_ui

    fp = tmp_path / "doover_config.json"
    monkeypatch.setattr(app_ui, "__file__", str(tmp_path / "src" / "prosense_sensors" / "app_ui.py"))
    (tmp_path / "src" / "prosense_sensors").mkdir(parents=True)

    app_ui.export()

    data = json.loads(fp.read_text())
    assert "ui_schema" in data["prosense_sensors"]
    assert data["prosense_sensors"]["ui_schema"]["type"] == "uiApplication"
    assert "temperature" in data["prosense_sensors"]["ui_schema"]["children"]


def _fresh_config(**overrides):
    """Build a ProSenseConfig with explicit defaults applied.

    pydoover stores config elements at class level, so values mutate
    across test boundaries. We always explicitly load_data here so the
    tests don't depend on previous-test state.
    """
    from prosense_sensors.app_config import ProSenseConfig

    cfg = ProSenseConfig()
    cfg.ui_enabled.load_data(overrides.get("ui_enabled", True))
    cfg.temperature_ranges.load_data(overrides.get("temperature_ranges", []))
    return cfg


def test_ui_setup_hides_elements_when_disabled():
    """When ui_enabled is False, setup() should strip every element."""
    import asyncio

    from prosense_sensors.app_ui import ProSenseUI

    cfg = _fresh_config(ui_enabled=False)
    ui_inst = ProSenseUI(cfg, None, "test")

    assert "temperature" in ui_inst._elements
    asyncio.run(ui_inst.setup())
    assert ui_inst._elements == {}


def test_ui_setup_promotes_to_radial_gauge_with_ranges():
    """When ranges are configured, the temperature element becomes a radial gauge."""
    import asyncio

    from pydoover import ui

    from prosense_sensors.app_ui import ProSenseUI

    cfg = _fresh_config(temperature_ranges=[
        {"label": "Cold", "range_min": -20, "range_max": 0, "colour": "blue"},
        {"label": "Normal", "range_min": 0, "range_max": 30, "colour": "green"},
        {"label": "Hot", "range_min": 30, "range_max": 60, "colour": "red"},
    ])
    ui_inst = ProSenseUI(cfg, None, "test")
    asyncio.run(ui_inst.setup())

    temp = ui_inst._elements["temperature"]
    assert isinstance(temp, ui.NumericVariable)
    assert temp.form == ui.Widget.radial
    assert temp.ranges is not None
    assert len(temp.ranges) == 3
    assert temp.ranges[0].label == "Cold"
    assert temp.ranges[0].min == -20
    assert temp.ranges[0].max == 0
    assert temp.ranges[2].colour == "red"


def test_ui_setup_no_ranges_keeps_linear_default():
    """No ranges configured → static defaults stay (no form attribute set)."""
    import asyncio

    from pydoover import ui
    from pydoover.ui.misc import NotSet

    from prosense_sensors.app_ui import ProSenseUI

    cfg = _fresh_config()
    ui_inst = ProSenseUI(cfg, None, "test")
    asyncio.run(ui_inst.setup())

    temp = ui_inst._elements["temperature"]
    assert isinstance(temp, ui.NumericVariable)
    assert temp.form is NotSet
    assert "comms_ok" in ui_inst._elements


def test_warning_indicators_present_in_static_schema():
    """Both warning indicators must be in the exported schema with hidden bound to tags."""
    from prosense_sensors.app_ui import ProSenseUI

    instance = ProSenseUI(None, None, None)
    schema = instance.to_schema(resolve_config=False)
    children = schema["children"]

    assert "comms_warning" in children
    assert "over_temp_warning" in children
    assert children["comms_warning"]["type"] == "uiWarningIndicator"
    assert children["over_temp_warning"]["type"] == "uiWarningIndicator"

    # hidden should be a tag-reference string (not the literal True/False)
    assert "$tag." in children["comms_warning"]["hidden"]
    assert "comms_ok" in children["comms_warning"]["hidden"]
    assert "$tag." in children["over_temp_warning"]["hidden"]
    assert "temperature_ok" in children["over_temp_warning"]["hidden"]


def test_is_temperature_ok_below_threshold():
    """No threshold = always ok; below = ok; at/above = not ok."""
    from prosense_sensors.app_config import ProSenseConfig
    from prosense_sensors.application import ProSenseApplication

    cfg = ProSenseConfig()
    cfg.over_temp_threshold.load_data(None)
    app = ProSenseApplication.__new__(ProSenseApplication)
    app.config = cfg
    assert app._is_temperature_ok(999.0) is True

    cfg.over_temp_threshold.load_data(50.0)
    assert app._is_temperature_ok(49.9) is True
    assert app._is_temperature_ok(50.0) is False
    assert app._is_temperature_ok(75.0) is False
