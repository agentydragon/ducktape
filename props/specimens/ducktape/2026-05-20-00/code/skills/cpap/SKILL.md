---
name: cpap
description: >
  Analyze CPAP sleep therapy data from the user's ResMed AirSense 11.
  Read daily summaries (AHI, leak, pressure, compliance) and per-session
  waveforms from EDF files served via WebDAV. Use when user asks about
  sleep quality, CPAP data, AHI, therapy compliance, or sleep analysis.
---

# CPAP Data Analysis

Analyze ResMed AirSense 11 AutoSet CPAP data synced daily from an ez Share
WiFi SD card to a cluster PVC, served read-only via WebDAV.

## Data access

CPAP data is served via WebDAV at `https://cpap.allegedly.works/` with HTTP
Basic Auth. Credentials are in the SOPS-encrypted secret at
`cluster/k8s/cpap-sync/webdav-auth.sops.yaml` (keys: `username`, `password`).

To get credentials:

```bash
sops -d cluster/k8s/cpap-sync/webdav-auth.sops.yaml
```

From a pod in `claude-sandbox`, the internal URL is
`http://cpap-webdav.cpap-sync.svc.cluster.local:8080/`.

### Directory structure on the card

```
/                        Root of the SD card
├── STR.EDF              Daily summary (one record per day, 78 signals)
├── Identification.json  Device serial, model, firmware
├── SETTINGS/            Device configuration snapshots
└── DATALOG/
    └── YYYYMMDD/        One directory per calendar date
        ├── *_CSL.edf    Session log (mask on/off events)
        ├── *_EVE.edf    Respiratory events (apneas, hypopneas, flow limitations)
        ├── *_BRP.edf    Breath-by-breath metrics (pressure, flow, leak per breath)
        ├── *_PLD.edf    High-resolution waveforms (~25 Hz: pressure, leak, flow)
        └── *_SA2.edf    SpO2 + pulse rate (if oximeter connected)
```

Note: filenames on the card use 8.3 short names (e.g., `202604~1.EDF`).
The long names above come from the card's XML API `<name>` field.

## EDF format overview

EDF (European Data Format) is a simple binary format:

1. **Main header** (256 bytes): version, patient, recording info, start date/time,
   number of data records, record duration, number of signals.
2. **Signal headers** (256 bytes per signal): label, units, physical/digital min/max,
   samples per record.
3. **Data records**: interleaved int16 samples for each signal.

Physical value from digital: `phys_min + (digital - dig_min) * (phys_max - phys_min) / (dig_max - dig_min)`

### STR.EDF signals (daily summary)

The `Date` signal stores days since Unix epoch (1970-01-01). Key signals:

| Signal                 | Unit            | Description                            |
| ---------------------- | --------------- | -------------------------------------- |
| `Date`                 | days from epoch | Calendar date                          |
| `Duration`             | minutes         | Total therapy time                     |
| `AHI`                  | events/hr       | Apnea-Hypopnea Index (total)           |
| `HI`                   | events/hr       | Hypopnea Index                         |
| `OAI`                  | events/hr       | Obstructive Apnea Index                |
| `CAI`                  | events/hr       | Central Apnea Index                    |
| `MaskPress.50` / `.95` | cmH2O           | Mask pressure median / 95th percentile |
| `Leak.50` / `.95`      | L/s             | Leak rate (multiply by 60 for L/min)   |
| `RespRate.50`          | bpm             | Respiratory rate median                |
| `TidVol.50`            | L               | Tidal volume median                    |
| `SpO2.50`              | %               | Blood oxygen median (-1 = no oximeter) |
| `CSR`                  | minutes         | Cheyne-Stokes respiration duration     |
| `MaskOn` / `MaskOff`   | minutes         | Mask on/off times (up to 20 per day)   |

### Clinical thresholds

| Metric     | Normal             | Mild  | Moderate | Severe      |
| ---------- | ------------------ | ----- | -------- | ----------- |
| AHI        | <5                 | 5-15  | 15-30    | >30         |
| Compliance | ≥4h on ≥70% nights | —     | —        | <4h or <70% |
| Leak 95th  | <24 L/min          | 24-36 | >36      | —           |

## Python libraries

### stdlib parsing (no dependencies)

For `STR.EDF` parsing, stdlib `struct` + `xml.etree.ElementTree` is sufficient.
See `examples/parse_str_edf.py` for a complete implementation.

### pyedflib (recommended for waveforms)

```bash
pip install pyedflib  # depends on numpy only
```

```python
from pyedflib import highlevel

signals, signal_headers, header = highlevel.read_edf("DATALOG/20260418/file.edf")
for i, sh in enumerate(signal_headers):
    print(f"{sh['label']}: {len(signals[i])} samples @ {sh['sample_frequency']} Hz")
```

### Other tools

- **OSCAR** (Open Source CPAP Analysis Reporter): Desktop GUI for ResMed data analysis.
  The gold standard for CPAP data visualization. https://www.sleepfiles.com/OSCAR/
- **oscar-etl** (`pip install oscar-etl`): Python ETL for ResMed EDF files. Extracts
  7 signals (pressure, leak, respiratory rate, tidal volume, minute ventilation, snore,
  flow limitation). Auto-segments by mask-on periods.
- **edf-importer** (https://github.com/tedpearson/edf-importer): Imports ResMed
  AirSense 11 EDF files to InfluxDB/VictoriaMetrics for Grafana dashboards.
- **edfio** (`pip install edfio`): Modern pure-Python EDF reader, alternative to pyedflib.

## Recipes

### Parse STR.EDF daily summary

See `examples/parse_str_edf.py`. This is a standalone stdlib-only script that reads
`STR.EDF` and outputs a nightly summary table with AHI, usage, pressure, leaks,
respiratory rate, and compliance stats.

Usage:

```bash
# Download STR.EDF from WebDAV and analyze last 14 days
curl -s -u "$USER:$PASS" https://cpap.allegedly.works/STR.EDF -o /tmp/STR.EDF
python3 examples/parse_str_edf.py /tmp/STR.EDF --days 14
```

### Read DATALOG waveforms with pyedflib

See `examples/read_waveforms.py`. Reads a DATALOG session's BRP/PLD/EVE files and
prints signal summaries (min, max, mean, duration).

### Scaffolding for tests

All recipes assume:

- Python 3.11+
- `pyedflib` available (for waveform recipes only; STR.EDF parsing is stdlib-only)
- EDF files accessible locally (downloaded from WebDAV or passed as arguments)

Test fixtures use the public EDF test file from
https://www.teuniz.net/edf_bdf_testfiles/test_generator_2_edfplus.zip (2.7 MB,
12 signals, 600 records at 1s duration). This validates format parsing without
requiring real CPAP data.
