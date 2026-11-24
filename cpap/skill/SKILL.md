---
name: cpap
description: >
  Analyze CPAP sleep therapy data from the user's ResMed AirSense 11.
  Read daily summaries (AHI, leak, pressure, compliance) and per-session
  waveforms from EDF files in the private cpap-data Forgejo git repo. Use
  when user asks about sleep quality, CPAP data, AHI, therapy compliance,
  or sleep analysis.
---

# CPAP Data Analysis

Analyze ResMed AirSense 11 AutoSet CPAP data synced daily from an ez Share
WiFi SD card into the private Forgejo repo `cpap-data/cpap-data`.

## Data access

Clone `https://git.allegedly.works/cpap-data/cpap-data.git` (the secret's
`repo_url` key) with the read-only `cpap-data-reader` credentials from the
`cpap-data-git-read` Kubernetes secret (keys: `username`, `password`,
`repo_url`):

- **Claude Code web**: read the reflected copy in `claude-sandbox` through the
  connected Kubernetes MCP path: `kubectl -n claude-sandbox get secret cpap-data-git-read`.
- **Laptops** (admin kubeconfig): `kubectl -n cpap-sync get secret cpap-data-git-read`.

```bash
USERNAME=$(kubectl -n claude-sandbox get secret cpap-data-git-read -o jsonpath='{.data.username}' | base64 -d)
PASSWORD=$(kubectl -n claude-sandbox get secret cpap-data-git-read -o jsonpath='{.data.password}' | base64 -d)

# Credentials via env config, not argv/URL (no leak into process listings):
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Basic $(printf '%s:%s' "$USERNAME" "$PASSWORD" | base64 -w0)"

# Fetch only the daily summary's blob from the multi-GB archive.
cpap_archive=/tmp/agents/YYYY_MM_DD/cpap-analysis/checkout
mkdir -p "$(dirname "$cpap_archive")"
git clone --filter=blob:none --no-checkout https://git.allegedly.works/cpap-data/cpap-data.git "$cpap_archive"
git -C "$cpap_archive" checkout main -- STR.EDF
```

Checking out `DATALOG/<date>/` directories the same way fetches only those
nights' blobs.

### Directory structure on the card

```text
/                        Root of the SD card
├── STR.EDF              Daily summary (one record per day, 78 signals)
├── Identification.json  Device serial, model, firmware
├── SETTINGS/            Device configuration snapshots
└── DATALOG/
    └── YYYYMMDD/        One directory per calendar date
        ├── *_CSL.edf    Session log (mask on/off events)
        ├── *_EVE.edf    Respiratory events (apneas, hypopneas, flow limitations)
        ├── *_BRP.edf    Flow and mask-pressure waveforms (25 Hz)
        ├── *_PLD.edf    Therapy metrics (0.5 Hz: pressure, leak, respiratory rate, etc.)
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

## Daily summary

Use a Python environment with NumPy and pandas. From the directory containing
this skill's `SKILL.md`, run the packaged parser against the checked-out file.

```bash
python3 examples/parse_str_edf.py "$cpap_archive/STR.EDF" --days 14
```

The report prints JSON records for the last 14 nights with positive usage, sorted by date.
It retains single-sample signal labels and omits multi-sample fields such as
`MaskOn` and `MaskOff`. The repository's parser test generates its own synthetic
`STR.EDF`.

## DATALOG waveforms

Fetch the desired date directory, then use PyEDFlib with NumPy to read the signals
and their sample frequencies.

```python
from pyedflib import highlevel

signals, signal_headers, header = highlevel.read_edf("/path/to/checkout/DATALOG/YYYYMMDD/file.edf")
for signal, signal_header in zip(signals, signal_headers, strict=True):
    print(f"{signal_header['label']}: {len(signal)} samples @ {signal_header['sample_frequency']} Hz")
```

Use the reader directly for now. The packaged `examples/read_waveforms.py` expects
`Duration` and `file_duration` in the header dictionary. PyEDFlib's reader exposes
[file duration separately](https://pyedflib.readthedocs.io/en/latest/ref/edfreader.html#pyedflib.EdfReader.getFileDuration).

## Other tools

- **OSCAR** (Open Source CPAP Analysis Reporter): Desktop GUI for ResMed data analysis.
  The gold standard for CPAP data visualization. https://www.sleepfiles.com/OSCAR/
- **oscar-etl** (`pip install oscar-etl`): Python ETL for ResMed EDF files. Extracts
  7 signals (pressure, leak, respiratory rate, tidal volume, minute ventilation, snore,
  flow limitation). Auto-segments by mask-on periods.
- **edf-importer** (https://github.com/tedpearson/edf-importer): Imports ResMed
  AirSense 11 EDF files to InfluxDB/VictoriaMetrics for Grafana dashboards.
- **edfio** (`pip install edfio`): Modern pure-Python EDF reader, alternative to pyedflib.
