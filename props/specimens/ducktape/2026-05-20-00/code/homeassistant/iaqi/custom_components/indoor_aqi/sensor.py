import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Indoor AQI borrowed from: https://atmotube.com/atmocube-support/indoor-air-quality-index-iaqi
# For each pollutant, linearly interpolate between breakpoints to get its IAQI
# subindex. They recommend using 1-minute averages.
#
# Breakpoint tables: sorted list of (concentration, IAQI).
# 100 = best (clean), 0 = worst (very polluted).


@dataclass(frozen=True)
class PollutantInfo:
    """Metadata for a pollutant recognised by this integration.

    Attributes
    ----------
    name
        Human-readable name, e.g. "CO₂".
    unit
        Unit of measurement that is expected from the underlying sensor.
    breakpoints
        List of pairs ``(concentration, iaqi)`` used to linearly interpolate
        the sub-index for this pollutant.  The points **must** be ordered by
        concentration ascending.  An IAQI of 100 represents perfectly clean
        air for that pollutant, 0 represents extremely polluted.
    """

    name: str
    unit: str
    breakpoints: list[tuple[float, int]]


# ---------------------------------------------------------------------------
# Pollutant definitions
# ---------------------------------------------------------------------------

# The breakpoint tables were previously stored in a separate global mapping.
# They have now been integrated directly into the PollutantInfo dataclass for
# better cohesion - every relevant bit of information about a pollutant is now
# located in a single place.

POLLUTANTS: dict[str, PollutantInfo] = {
    "co2": PollutantInfo(
        name="CO₂", unit="ppm", breakpoints=[(400, 100), (600, 80), (1000, 60), (1500, 40), (2500, 20), (4000, 0)]
    ),
    "voc": PollutantInfo(
        name="VOCs", unit="ppb", breakpoints=[(1, 100), (200, 80), (250, 60), (350, 40), (400, 20), (500, 0)]
    ),
    "nox": PollutantInfo(
        name="NOₓ", unit="ppb", breakpoints=[(1, 100), (50, 80), (100, 60), (300, 40), (350, 20), (500, 0)]
    ),
    "ch2o": PollutantInfo(
        name="Formaldehyde",
        unit="mg/m³",
        breakpoints=[(0, 100), (0.06, 80), (0.11, 60), (0.31, 40), (0.76, 20), (1.0, 0)],
    ),
    "pm1": PollutantInfo(
        name="PM1", unit="μg/m³", breakpoints=[(0, 100), (15, 80), (35, 60), (62, 40), (96, 20), (150, 0)]
    ),
    "pm25": PollutantInfo(
        name="PM2.5", unit="μg/m³", breakpoints=[(0, 100), (21, 80), (51, 60), (91, 40), (141, 20), (200, 0)]
    ),
    "pm10": PollutantInfo(
        name="PM10", unit="μg/m³", breakpoints=[(0, 100), (31, 80), (76, 60), (126, 40), (201, 20), (300, 0)]
    ),
    "co": PollutantInfo(
        name="CO", unit="ppm", breakpoints=[(0, 100), (1.8, 80), (8.8, 60), (10.1, 40), (15.1, 20), (30, 0)]
    ),
    "o3": PollutantInfo(
        name="O₃", unit="ppm", breakpoints=[(0, 100), (0.026, 80), (0.061, 60), (0.076, 40), (0.101, 20), (0.3, 0)]
    ),
}


def compute_iaqi(pollutant: str, c: float) -> float | None:
    """
    Given a pollutant name (e.g. 'co2') and measured concentration `c`,
    look up the breakpoints and do piecewise linear interpolation to get IAQI in 0..100.
    If c is below the first bracket => clamp to that bracket's IAQI.
    If c is above the last => clamp to last bracket's IAQI.
    If we can't find the pollutant => returns None.
    """
    pollutant_info = POLLUTANTS.get(pollutant.lower())
    if not pollutant_info:
        return None  # unknown pollutant

    bp = pollutant_info.breakpoints

    # If c is below the first bracket
    if c < bp[0][0]:
        return float(bp[0][1])

    # If c is above the last bracket
    if c > bp[-1][0]:
        return float(bp[-1][1])

    # Otherwise, find i such that bp[i][0] <= c <= bp[i+1][0]
    for idx in range(len(bp) - 1):
        c_lo, i_lo = bp[idx]
        c_hi, i_hi = bp[idx + 1]
        if c_lo <= c <= c_hi:
            # linear interpolation
            if c_hi == c_lo:
                return float(i_lo)
            ratio = (c - c_lo) / (c_hi - c_lo)
            return float(i_lo + (i_hi - i_lo) * ratio)

    return None  # fallback if something was out of the bracket array


def parse_timedelta(value) -> timedelta:
    """Parse an integer or HH:MM:SS string into a timedelta."""
    if isinstance(value, int):
        return timedelta(seconds=value)
    if isinstance(value, str):
        # try parse as integer seconds
        try:
            return timedelta(seconds=int(value))
        except ValueError:
            pass
        # else assume HH:MM:SS
        hh, mm, ss = value.split(":")
        return timedelta(hours=int(hh), minutes=int(mm), seconds=int(ss))

    raise ValueError(f"Invalid stale_time: {value}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """
    Called after __init__.py's async_setup_entry. We'll read the config from
    hass.data[DOMAIN]["yaml_config"], build one or more sensors, and add them.
    """
    integration_data = hass.data.get(DOMAIN, {})
    yaml_cfg = integration_data.get("yaml_config", {})

    # The user might define "monitors" as a list:
    # indoor_aqi:
    #   monitors:
    #     - name: "Rai's room AQI"
    #       unique_id: "rai_s_room_aqi"
    #       sensors:
    #         co2: sensor.xxxx
    #         pm25: sensor.yyyy
    #   stale_time: "3600"

    # Look for "monitors" or fallback to a single "sensors" block.
    monitors = yaml_cfg.get("monitors", [])
    # Single block fallback for sensors config
    if not monitors and "sensors" in yaml_cfg:
        monitors = [yaml_cfg]

    entities = []
    for m in monitors:
        name = m.get("name", "Indoor AQI")
        unique_id = m.get("unique_id")
        sensor_map = m.get("sensors", {})
        stale_str = m.get("stale_time", yaml_cfg.get("stale_time", "3600"))
        stale_time = parse_timedelta(stale_str)

        entities.append(
            IndoorAQISensor(hass=hass, name=name, unique_id=unique_id, sensor_map=sensor_map, stale_time=stale_time)
        )

    if not entities:
        _LOGGER.warning("No monitors found in YAML config, no IndoorAQI sensors created.")

    async_add_entities(entities, update_before_add=True)


class IndoorAQISensor(SensorEntity):
    """
    Each IndoorAQISensor references one set of pollutant sensors,
    calculates a single IAQI (0..100) = min(subindices),
    sets textual labels, etc.

    This sensor provides:
    1. Overall IAQI as the state (minimum of all pollutant indices)
    2. Individual IAQI components for each pollutant (as attributes with iaqi_ prefix)
    3. Raw pollutant values for reference (as attributes with raw_ prefix)
    4. Bottleneck pollutants - components with lowest IAQI values, ordered from worst to less bad

    This allows building dashboards that show not just the overall air quality,
    but also which specific pollutants are causing problems.

    'suggested_object_id' is optional
    """

    def __init__(self, hass, name, unique_id, sensor_map, stale_time: timedelta):
        self._hass = hass
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._sensor_map = sensor_map  # dict: pollutant -> entity_id
        self._stale_time = stale_time

        self._state: float | None = None  # final IAQI
        self._attrs: dict[str, Any] = {}
        self._icon = "mdi:cloud"

        # For tracking partial data logging
        # Set of sensors with errors in previous update
        self._previous_error_sensors: set[str] = set()
        # Last time we logged partial data
        self._last_log_time = datetime.now(UTC)

        # Make it numeric so that HA will plot it
        self._attr_native_unit_of_measurement = "IAQI"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def suggested_display_precision(self) -> int:
        return 0

    @property
    def native_value(self) -> float | None:
        return self._state

    @property
    def icon(self):
        return self._icon

    @property
    def extra_state_attributes(self):
        return self._attrs

    def update(self):
        now_utc = datetime.now(UTC)
        # Track sensors and their error types: {pollutant: error_type}
        sensor_errors = {}
        iaqi_components = {}
        raw_values = {}

        for pollutant, entity_id in self._sensor_map.items():
            if not entity_id:
                sensor_errors[pollutant] = "missing entity_id"
                continue

            s_obj = self._hass.states.get(entity_id)
            if not s_obj:
                sensor_errors[pollutant] = "no state object"
                continue

            raw = s_obj.state
            if raw in [STATE_UNKNOWN, STATE_UNAVAILABLE, None]:
                sensor_errors[pollutant] = "unavailable"
                continue

            # check staleness
            if (now_utc - s_obj.last_updated) > self._stale_time:
                sensor_errors[pollutant] = "stale"
                continue

            # parse float
            try:
                val = float(raw)
                raw_values[pollutant] = val  # Store the raw value
            except ValueError:
                sensor_errors[pollutant] = "not numeric"
                continue

            iaqi = compute_iaqi(pollutant, val)
            if iaqi is None:
                sensor_errors[pollutant] = "bracket unknown"
            else:
                # Store individual component IAQI
                iaqi_components[pollutant] = iaqi

        # Compute subindices from iaqi_components
        subindices = list(iaqi_components.values())

        # Find bottleneck components (those with lowest IAQI values)
        if subindices:
            overall = min(subindices)  # 0..100 (lowest=worst, highest=best)
            self._state = overall

            bottlenecks = []
            bottleneck_details = []

            # Sort components by IAQI value (ascending) and process those within 5 points of minimum
            # This ensures bottlenecks are ordered from worst to least bad
            for pollutant, value in sorted(iaqi_components.items(), key=lambda x: x[1]):
                if value <= overall + 5:  # Components within 5 points of minimum
                    bottlenecks.append(pollutant)

                    # Create human-readable detail with pollutant name, value and unit
                    if pollutant_info := POLLUTANTS.get(pollutant):
                        bottleneck_details.append(
                            f"{pollutant_info.name}: {raw_values[pollutant]} {pollutant_info.unit}"
                        )

            # Create a human-readable bottleneck string
            bottleneck_string = ", ".join(bottleneck_details)
        else:
            overall = None
            self._state = None
            bottlenecks = []
            bottleneck_string = ""

        if overall is None:
            label, color, icon = "Unknown", "grey", "help"
        elif overall > 80:
            label, color, icon = "Good", "green", "emoticon-happy"
        elif overall > 60:
            label, color, icon = "Moderate", "yellow", "emoticon-neutral"
        elif overall > 40:
            label, color, icon = "Polluted", "orange", "emoticon-sad"
        elif overall > 20:
            label, color, icon = "Very Polluted", "red", "emoticon-dead"
        else:
            label, color, icon = "Severely Polluted", "purple", "emoticon-devil"

        self._icon = f"mdi:{icon}"
        # Transform the *dict* into a *list[str]* for attributes - keep the
        # original mapping around for later use.
        sensor_errors_list = [f"{pollutant}: {error_type}" for pollutant, error_type in sensor_errors.items()]

        self._attrs = {
            "level": label,
            "color": color,
            "sensor_errors": sensor_errors_list,
            "subindex_count": len(subindices),
            # Add component IAQIs with iaqi_ prefix for each pollutant
            **{f"iaqi_{pollutant}": value for pollutant, value in iaqi_components.items()},
            # Add raw values with raw_ prefix for each pollutant
            **{f"raw_{pollutant}": value for pollutant, value in raw_values.items()},
            # Human-readable bottleneck string with pollutant names, values and units
            "bottleneck_string": bottleneck_string,
        }

        # Handle partial data logging with improved tracking
        if sensor_errors:
            # Get current set of sensors with errors
            current_error_sensors = set(sensor_errors.keys())

            # Calculate what's changed since last time
            new_errors = [f"{p} ({sensor_errors[p]})" for p in current_error_sensors - self._previous_error_sensors]
            new_ok = self._previous_error_sensors - current_error_sensors

            # Get current time for checking the hour threshold
            hour_passed = (now_utc - self._last_log_time) > timedelta(hours=1)

            # Log if there are changes or an hour has passed
            if new_errors or new_ok or hour_passed:
                # Format the log message
                log_parts = [f"{self.name} partial data: {sensor_errors}"]

                if new_errors:
                    log_parts.append(f"Newly unavailable: {', '.join(new_errors)}")

                if new_ok:
                    log_parts.append(f"Newly available: {', '.join(new_ok)}")

                _LOGGER.warning(" | ".join(log_parts))

                # Update the last log time
                self._last_log_time = now_utc

            # Save current errors for next comparison
            self._previous_error_sensors = current_error_sensors
