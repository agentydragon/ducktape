# Indoor Air Quality Index (IAQI) Component for Home Assistant

Calculates an Indoor Air Quality Index (IAQI) from multiple air quality sensors. IAQI ranges from 0-100, where 100 is excellent air and 0 is severely polluted.

## Features

- **Overall IAQI calculation**: Combines multiple air quality sensors into a single 0-100 index
- **Individual component tracking**: Exposes IAQI value for each pollutant separately
- **Bottleneck identification**: Identifies which pollutants are causing poor air quality
- **Human-readable descriptions**: Easy-to-understand descriptions of air quality issues
- **Raw value tracking**: Stores raw sensor values for context

## Installation

1. Copy `custom_components/indoor_aqi` directory to your Home Assistant configuration
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

`dashboard_examples.yaml` has examples of how to display IAQI in dashboards.

### Configuration

| Option       | Description                                                                                 |
| ------------ | ------------------------------------------------------------------------------------------- |
| `monitors`   | List of monitor configurations - each "monitor" is 1 set of sensors aggregated into an IAQI |
| `name`       | Display name for the aggregated IAQI sensor                                                 |
| `unique_id`  | Unique identifier for the aggregated IAQI sensor (optional but recommended)                 |
| `sensors`    | Map of pollutant type to entity_id of sensors that are aggregated into IAQI                 |
| `stale_time` | How long before sensor readings are considered stale (optional, default: 3600s)             |

### Supported Pollutant Types

|------|-------------|
| `co2` | Carbon Dioxide |
| `voc` | Volatile Organic Compounds |
| `pm1` | Particulate Matter 1μm |
| `pm10` | Particulate Matter 10μm |
| `pm25` | Particulate Matter 2.5μm |
| `nox` | Nitrogen Oxides |
| `co` | Carbon Monoxide |
| `o3` | Ozone |
| `ch2o` | Formaldehyde |

## How it Works

IAQI is calculated using the approach described by [Atmotube](https://atmotube.com/atmocube-support/indoor-air-quality-index-iaqi):

For each pollutant, calculate a sub-index from 0-100 by linear interpolation between defined breakpoints
Overall IAQI is the minimum (worst) of all sub-indices.
Highlighted "bottleneck" pollutants are those with IAQI component values close to the minimum.

## Attributes

The sensor exposes the following attributes:

| Attribute           | Description                                                               |
| ------------------- | ------------------------------------------------------------------------- |
| `level`             | Text description of air quality level                                     |
| `color`             | Color representing the quality level (green, yellow, orange, red, purple) |
| `bottlenecks`       | List of pollutants causing poor air quality (worst first)                 |
| `bottleneck_string` | Human-readable description of bottlenecks with values and units           |
| `iaqi_[pollutant]`  | Individual IAQI value for each pollutant                                  |
| `raw_[pollutant]`   | Raw sensor value for each pollutant                                       |
| `subindex_count`    | Number of valid pollutant readings                                        |
| `sensor_errors`     | List of any sensor errors encountered                                     |

## Development & Testing

See the repository root AGENTS.md for the standard Bazel workflow.

```bash
bazel build //homeassistant/iaqi/...
bazel test //homeassistant/iaqi/...
bazel build --config=check //homeassistant/iaqi/...  # lint + typecheck
```
