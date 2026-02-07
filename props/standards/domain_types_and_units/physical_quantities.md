---
title: Physical quantities use explicit units or typed unit systems
kind: outcome
---

Variables representing physical quantities (distance, temperature, speed, angles, etc.) must have unambiguous units.
Prefer rich, domain-appropriate types or unit libraries, or at least encode units in names.
Maintain a single consistent internal unit per quantity to avoid drift.

## Acceptance criteria (checklist)

- Use typed units where practical
  - Python: prefer a unit library (for example, Pint) or typed wrappers; [use `datetime` types for time/duration](time.md)
  - Go: define small newtypes (for example, `type Meters float64`) or structs with methods; avoid untyped `float64` for mixed units
- If primitives are used, names explicitly include units - e.g.: `user_height_cm`, `speed_mph`, `roll_degrees`.
- Choose one canonical internal unit per quantity (for example, meters for length, m/s for speed)
  - Convert inputs to canonical units at the boundary; convert back on output
- Logging/UI/docs include units (derive labels from the same source of truth where possible)
- Validate and normalize at boundaries (reject unknown units, handle case/spacing)
- Adoption guidance: if your codebase starts accumulating multiple physical quantities and conversions, adopt a unit library (for example, Pint) to prevent errors and centralize conversions

## Positive examples

If code does not handle many physical quantities, OK to just use suffixes to mark quantities with units:

```python
from dataclasses import dataclass

@dataclass
class ThermostatReading:
    temperature_celsius: float

def adjust(target_celsius: float, current_celsius: float) -> float:
    error_celsius = target_celsius - current_celsius
    return error_celsius
```

Once writing unit-heavier code (e.g., physics), use a unit library:

```python
from pint import UnitRegistry
ureg = UnitRegistry()
Q_ = ureg.Quantity

@dataclass
class Pose:
    x: Q_  # meters
    y: Q_

MAX_STEP: Q_ = 0.25 * ureg.meter

def step(pos: Pose, dx_cm: float, dy_cm: float) -> Pose:
    # Convert at boundary; internals remain meters
    dx = (dx_cm * ureg.centimeter).to(ureg.meter)
    dy = (dy_cm * ureg.centimeter).to(ureg.meter)
    new = Pose(pos.x + dx, pos.y + dy)
    return new if (abs(dx) <= MAX_STEP and abs(dy) <= MAX_STEP) else pos
```

```go
// Go — dedicated type clarifies units
package physics

type Meters float64

type Pose struct{ X, Y Meters }

const MaxStep Meters = 0.25

func Step(p Pose, dxCM, dyCM float64) Pose {
    dx := Meters(dxCM / 100.0) // convert cm -> m at boundary
    dy := Meters(dyCM / 100.0)
    newP := Pose{p.X + dx, p.Y + dy}
    if abs(dx) <= MaxStep && abs(dy) <= MaxStep { return newP }
    return p
}
```

## Negative examples

Ambiguous units and mixed arithmetic:

```python
speed = 3.6   # km/h? m/s? mph?
step = 25     # cm? m?
position = position + step  # *boom* - nothing prevents unit logic error
```

## Exceptions

- Protocol/file format boundaries that mandate specific units (for example, Fahrenheit, centimeters) may use those units at the edge; convert immediately to canonical units internally
- Short-lived locals immediately involved in a conversion expression may omit suffixes when unit is obvious and enforced by surrounding typed context

## Guidance

- Prefer SI base units internally (meters, seconds, kilograms, celsius/kelvin) and well-known derived units where conventional (m/s)
- Centralize conversions behind helpers to avoid copy/paste and drift
- If dealing with many physical quantities, adopt a unit library (for example, Pint) to make unit errors unrepresentable
