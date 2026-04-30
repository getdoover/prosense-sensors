from pathlib import Path

from pydoover import ui

from .app_tags import ProSenseTags


class ProSenseUI(ui.UI):
    # Static defaults — what export-ui writes to doover_config.json and
    # what the device shows before any user config has been applied.
    # The runtime ``setup`` below mutates these based on config:
    #   - ui_enabled = False              → strip all UI elements
    #   - temperature_ranges non-empty    → swap in a radial gauge with bands
    temperature = ui.NumericVariable(
        "Temperature",
        value=ProSenseTags.temperature,
        name="temperature",
        precision=2,
        position=1,
    )
    comms_ok = ui.BooleanVariable(
        "Sensor Communicating",
        value=ProSenseTags.comms_ok,
        name="comms_ok",
        position=2,
    )
    # Warning indicators auto-hide via tag bindings: the WarningIndicator
    # is hidden whenever the bound tag is truthy, so we bind to the
    # *positive* state ("ok"). The application drives the tags.
    comms_warning = ui.WarningIndicator(
        "Sensor Not Communicating",
        name="comms_warning",
        hidden=ProSenseTags.comms_ok,
        position=10,
    )
    over_temp_warning = ui.WarningIndicator(
        "Over Temperature",
        name="over_temp_warning",
        hidden=ProSenseTags.temperature_ok,
        position=11,
    )

    async def setup(self):
        if self.config is None:
            return

        if not _config_value(self.config, "ui_enabled", True):
            for name in list(self._elements):
                self.remove_element(name)
            return

        ranges = list(_build_ranges(self.config))
        if not ranges:
            return

        self.remove_element("temperature")
        self.add_element(ui.NumericVariable(
            "Temperature",
            value=ProSenseTags.temperature,
            name="temperature",
            precision=2,
            position=1,
            ranges=ranges,
            form=ui.Widget.radial,
        ))


def _build_ranges(config):
    raw_ranges = getattr(config, "temperature_ranges", None)
    if raw_ranges is None:
        return
    for entry in raw_ranges.value:
        yield ui.Range(
            label=_config_value(entry, "label", "") or None,
            min_val=_config_value(entry, "range_min", None),
            max_val=_config_value(entry, "range_max", None),
            colour=ui.Colour.from_string(_config_value(entry, "colour", "blue") or "blue"),
        )


def _config_value(container, attr, default):
    """Read ``container.<attr>.value`` defensively.

    Lets us share helpers between the schema (where attrs are
    ConfigElement instances) and entries inside an Array (which expose
    attrs via __getattribute__ on the Object). Returns ``default`` if
    the attribute is missing, the .value getter raises (unset and no
    default), or the value is None.
    """
    elem = getattr(container, attr, None)
    if elem is None:
        return default
    try:
        value = elem.value
    except (ValueError, AttributeError):
        return default
    if value is None:
        return default
    return value


def export():
    """Write the static ui_schema for ``prosense_sensors`` into doover_config.json.

    pydoover's ``UI.export`` refuses to run when ``setup`` is overridden,
    on the grounds that the static export wouldn't reflect runtime
    mutations. That's fine for us: the class-level defaults already
    describe the no-config-yet state, and ``setup`` only adds polish
    (radial gauge, hidden UI). So we inline the export logic here and
    skip the is_static guard.
    """
    import json

    fp = Path(__file__).parents[2] / "doover_config.json"
    instance = ProSenseUI(None, None, None)
    schema = instance.to_schema(resolve_config=False)

    data = json.loads(fp.read_text()) if fp.exists() else {}
    data.setdefault("prosense_sensors", {})["ui_schema"] = schema
    fp.write_text(json.dumps(data, indent=4))


if __name__ == "__main__":
    export()
