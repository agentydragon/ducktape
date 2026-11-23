import functools
import logging
from typing import ClassVar

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString

register = template.Library()
logger = logging.getLogger(__name__)


def shorten(value):
    """
    Shortens a physical value.

    '5 V' -> '5V'
    """
    if value is None:
        return None
    return value.replace(" ", "")


def prefix(x):
    return lambda y: x + y


def remove_prefix(x):
    return lambda y: y.removeprefix(x)


def fmt_range(min, max):
    if min and not max:
        return f"&geq;{min}"
    if max and not min:
        return f"&leq;{max}"
    if min and max:
        return f"&in;[{min};{max}]"
    return None


def apply(value, *fns):
    for fn in fns:
        if not value:
            break
        value = fn(value)
    return value


def fmt_quantity_at(value, symbol):
    value = shorten(value)
    if not value:
        return None
    quantity, condition = value.split("@")
    return f"{symbol}={quantity}@{condition}"


class ParameterDisplay(property):
    def __init__(self, names, formatter):
        self.names = names

        @functools.wraps(formatter)
        def display(processor):
            out = formatter(processor, *(processor.get(name) for name in names))
            return SafeString(out) if out is not None else None

        super().__init__(display)

    def __set_name__(self, owner, _name):
        owner._displayers.append(self)


def displayer(*names):
    def decorator(formatter):
        return ParameterDisplay(names, formatter)

    return decorator


class ParametersProcessor:
    _displayers: ClassVar[list[ParameterDisplay]] = []

    def __init__(self, part, parameters):
        self.part = part
        self.parameters = {k: v for k, v in parameters.items() if v is not None}
        self.used = set()

    def get(self, x):
        if not self.parameters.get(x):
            return None
        self.used.add(x)
        return escape(self.parameters[x])

    @property
    def any_remaining(self):
        return bool(set(self.parameters) - self.used)

    @property
    def remaining_items(self):
        remaining_keys = set(self.parameters) - self.used
        for display in self._displayers:
            if any(name in remaining_keys for name in display.names):
                yield display.__get__(self)

        remaining_keys = set(self.parameters) - self.used
        for key in self.parameters:
            if key in remaining_keys and key not in ("Bought for", "Marking"):
                yield SafeString(f"{key}: {self.get(key)}")

    @displayer("Package")
    def package(self, val):
        return apply(val, remove_prefix("SMD "))

    @displayer("Power rating")
    def show_power_rating(self, val):
        return apply(val, shorten, prefix("P&lt;"))

    @displayer("Collector-emitter voltage (V_CEO)")
    def collector_emitter_voltage(self, val):
        return apply(val, shorten, prefix("V<sub>CEO</sub>="))

    @displayer("Resistance", "Tolerance")
    def show_value(self, resistance, tolerance):
        val = apply(resistance, shorten)
        tol = apply(tolerance, prefix("±"))
        return val + tol

    @property
    def heading(self):
        return self.get("Jellybean P/N") or self.part.name

    def under_category(self, name):
        category = self.part.category
        while category:
            if category.name == name:
                return True
            category = category.parent
        return False

    @property
    def is_ldo(self):
        return self.under_category("Linear LDO voltage regulators")

    @property
    def is_bjt(self):
        return self.under_category("BJT")

    @property
    def is_ic(self):
        return self.under_category("Integrated circuits")

    @property
    def is_resistor(self):
        return self.under_category("Resistors")

    @property
    def is_resistor_network(self):
        return self.under_category("Resistor networks")

    @property
    def is_diode(self):
        return self.under_category("Diodes")

    @displayer("Minimum input voltage", "Maximum input voltage")
    def input_voltage_range(self, min, max):
        min, max = shorten(min), shorten(max)
        return apply(fmt_range(min, max), prefix("V<sub>in</sub>"))

    @displayer("Reverse voltage")
    def reverse_voltage(self, val):
        # Or "V_rev"
        return apply(val, shorten, prefix("V<sub>R</sub>="))

    @displayer("Power dissipation (Pd)")
    def power_dissipation(self, val):
        return apply(val, shorten, prefix("P<sub>d</sub>="))

    @displayer("Forward voltage @ current")
    def forward_voltage_at_current(self, val):
        return fmt_quantity_at(val, "V<sub>F</sub>")

    @displayer("Average rectified forward current (I_F)")
    def average_rectified_forward_current(self, val):
        return apply(val, shorten, prefix("I<sub>F</sub>="))

    @displayer("Reverse leakage current @ voltage (I_R)")
    def reverse_leakage_current(self, val):
        return fmt_quantity_at(val, "I<sub>R</sub>")

    @displayer("Minimum supply voltage", "Maximum supply voltage")
    def supply_voltage_range(self, min, max):
        min, max = shorten(min), shorten(max)
        return apply(fmt_range(min, max), prefix("V<sub>sup</sub>"))

    @displayer("Maximum output current")
    def max_output_current(self, val):
        return apply(fmt_range(None, shorten(val)), prefix("I<sub>out</sub>"))

    @displayer("Output voltage")
    def output_voltage(self, val):
        # TODO: voltage can also here be written as 'adjustable 1.2-37 V',
        # 'adjustable 0.8~5.5V'
        return shorten(val).replace("adjustable", "adj")

    @displayer("BJT type")
    def bjt_type(self, val):
        return val

    @displayer("Function")
    def function(self, val):
        return val

    @displayer("Jellybean P/N")
    def jellybean_pn(self, val):
        return val


@register.simple_tag(takes_context=True)
def parameters_processor(context):
    return ParametersProcessor(context["part"], context["parameters"])
