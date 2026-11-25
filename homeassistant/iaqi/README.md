# Indoor Air Quality Index (IAQI) Component for Home Assistant

Calculates an Indoor Air Quality Index (IAQI) from multiple air quality sensors. IAQI ranges from 0-100, where 100 is excellent air and 0 is severely polluted.

Uses the [Atmotube IAQI method](https://atmotube.com/atmocube-support/indoor-air-quality-index-iaqi): each pollutant is linearly interpolated between local breakpoints, and the aggregate is the lowest subindex. The local pollutant keys, units, and breakpoints are defined in [`sensor.py`](custom_components/indoor_aqi/sensor.py).

## Installation

1. Copy `custom_components/indoor_aqi` to `<config>/custom_components/indoor_aqi`, where
   `<config>` is your Home Assistant configuration directory.
2. Add to your `configuration.yaml`:

   ```yaml
   indoor_aqi:
     monitors:
       - name: "Living Room AQI"
         unique_id: "living_room_aqi" # Optional but recommended
         sensors:
           co2: sensor.living_room_co2
           pm25: sensor.living_room_pm25
           voc: sensor.living_room_voc

       - name: "Bedroom AQI"
         unique_id: "bedroom_aqi"
         sensors:
           co2: sensor.bedroom_co2
           pm25: sensor.bedroom_pm25

     # Optional: set how long before a sensor reading is considered "stale"
     stale_time: "1:00:00" # Format: HH:MM:SS or seconds (default: 3600)
   ```

3. Restart Home Assistant.

The `indoor_aqi` block at the top of the [retained room package](../packages/rai/sensors.yaml)
records the legacy monitor ID and sensor mappings. Copy just that block to reproduce the
room monitor on a compatible instance. Each monitor can override the top-level `stale_time`.

[`dashboard_examples.yaml`](dashboard_examples.yaml) shows dashboard use, including `bottleneck_string` (components within 5 points of the minimum, with values and units) and per-pollutant `iaqi_*` attributes; [`sensor.py`](custom_components/indoor_aqi/sensor.py) owns the complete emitted attribute set.

## Development & Testing

The Home Assistant pytest plugin installs a `custom_components` namespace rooted in its own
test configuration. The [fixture](custom_components/indoor_aqi/conftest.py) adds the integration
directory from this checkout so Home Assistant can discover `indoor_aqi` under Bazel.

```sh
bbr test //homeassistant/iaqi/...
```
