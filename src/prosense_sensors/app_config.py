from pathlib import Path

from pydoover import config
from pydoover.docker.modbus import ModbusConfig

from .prosense_driver import Variant


class TemperatureRange(config.Object):
    """One coloured band on the temperature radial gauge.

    Add as many of these as you need under "Temperature Ranges". When
    at least one range is configured the temperature element switches
    from a plain numeric display to a radial gauge.
    """

    label = config.String(
        "Label",
        name="label",
        default="",
        description="Optional label shown next to the range (e.g. 'Cold', 'Normal', 'Hot').",
    )
    range_min = config.Number(
        "Min",
        name="range_min",
        description="Lower bound (in the configured display unit).",
    )
    range_max = config.Number(
        "Max",
        name="range_max",
        description="Upper bound (in the configured display unit).",
    )
    colour = config.String(
        "Colour",
        name="colour",
        default="blue",
        description="HTML colour name (red, orange, yellow, green, blue, purple, grey) "
                    "or a hex string like #FF5733.",
    )

    def __init__(self):
        super().__init__("Range")


class ProSenseConfig(config.Schema):
    variant = config.Enum(
        "Sensor Variant",
        name="variant",
        choices=[v.value for v in Variant],
        default=Variant.TEMPERATURE.value,
        description="ProSense product family. Only 'temperature' is fully wired up; "
                    "the others are placeholders for future expansion of this app.",
    )

    # Default display name "Modbus Config" sanitises to JSON key "modbus_config".
    modbus_config = ModbusConfig()

    slave_id = config.Integer(
        "Slave ID",
        name="slave_id",
        default=1,
        description="Modbus unit / slave ID of the ProSense sensor (1..255).",
    )

    poll_interval_seconds = config.Number(
        "Poll Interval (seconds)",
        name="poll_interval_seconds",
        default=1.0,
        description="How often to read the sensor.",
    )

    display_unit = config.Enum(
        "Display Unit (temperature)",
        name="display_unit",
        choices=["celsius", "fahrenheit", "kelvin"],
        default="celsius",
        description="Unit used for the published temperature tag, regardless of "
                    "what the sensor itself is configured for. Only applies to "
                    "the temperature variant.",
    )

    no_comms_timeout_seconds = config.Integer(
        "No-Comms Timeout (seconds)",
        name="no_comms_timeout_seconds",
        default=30,
        description="Clear comms_ok after this many seconds without a successful read.",
    )

    over_temp_threshold = config.Number(
        "Over-Temperature Warning Threshold",
        name="over_temp_threshold",
        default=None,
        description="Optional. If set, an over-temperature warning is shown in the UI "
                    "whenever the published temperature is at or above this value. "
                    "Specified in the configured display_unit. Leave blank to disable.",
    )

    ui_enabled = config.Boolean(
        "Enable UI",
        name="ui_enabled",
        default=True,
        description="Show the sensor's UI tile on the device. Disable to hide all "
                    "of this app's UI elements (the tags still publish).",
    )

    temperature_ranges = config.Array(
        "Temperature Ranges",
        name="temperature_ranges",
        element=TemperatureRange(),
        description="Optional. If any ranges are defined the temperature element "
                    "is shown as a radial gauge with these coloured bands. "
                    "Leave empty for a plain numeric display.",
    )

    sim_app_key = config.Application(
        "Simulator App Key",
        name="sim_app_key",
        default="",
        description="Optional. If set, read sensor data from this simulator app's tags "
                    "instead of the Modbus bus. Leave blank for normal operation.",
    )


def export():
    ProSenseConfig.export(
        Path(__file__).parents[2] / "doover_config.json",
        "prosense_sensors",
    )


if __name__ == "__main__":
    export()
