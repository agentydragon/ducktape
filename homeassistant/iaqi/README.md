# Indoor Air Quality Index (IAQI) Component for Home Assistant

Calculates an Indoor Air Quality Index (IAQI) from multiple air quality sensors. IAQI ranges from 0-100, where 100 is excellent air and 0 is severely polluted.

Uses the [Atmotube IAQI method](https://atmotube.com/atmocube-support/indoor-air-quality-index-iaqi): each pollutant is linearly interpolated between local breakpoints, and the aggregate is the lowest subindex. The local pollutant keys, units, and breakpoints are defined in [`sensor.py`](custom_components/indoor_aqi/sensor.py).

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

[`configuration_snippet.yaml`](configuration_snippet.yaml) has the current local example. [`dashboard_examples.yaml`](dashboard_examples.yaml) shows dashboard use, including `bottleneck_string` (components within 5 points of the minimum, with values and units) and per-pollutant `iaqi_*` attributes; [`sensor.py`](custom_components/indoor_aqi/sensor.py) owns the complete emitted attribute set.

## Development & Testing

### Test Setup with pytest_homeassistant_custom_component

The tests use `pytest_homeassistant_custom_component` for Home Assistant fixtures (`hass`, `enable_custom_integrations`, etc.). Running these tests under Bazel requires two workarounds in `conftest.py`:

**1. Custom component discovery path**

The plugin includes its own `custom_components` namespace package. When it loads, `custom_components.__path__` only points to the plugin's `testing_config/custom_components/` directory. HA's loader uses this path to discover integrations, so without intervention, it can't find `indoor_aqi`.

Fix: The `auto_enable_custom_integrations` fixture adds our `custom_components` directory to `custom_components.__path__`:

```python
cc_path = str(config_dir / "custom_components")
if cc_path not in custom_components.__path__:
    custom_components.__path__.insert(0, cc_path)
```

**2. Recorder patching order**

The plugin patches HA's recorder module at load time and asserts that `homeassistant.components.recorder.util` isn't already imported. Some HA modules (`homeassistant.setup`, `homeassistant.config_entries`) trigger recorder loading as a side effect.

If test files have top-level imports of these modules, they execute during pytest collection (before the plugin loads), causing the assertion to fail.

Fix: Use lazy imports inside test functions for heavyweight HA modules:

```python
async def test_setup_component(hass):
    # Import inside function, not at module level
    from homeassistant.setup import async_setup_component  # noqa: PLC0415
    ...
```

Lightweight imports like `from homeassistant.const import STATE_UNAVAILABLE` are safe at module level.
