import sys
from pathlib import Path

import pytest_bazel

sys.path.append(str(Path(__file__).parent))
from inventree_utils.rai_plugin.templatetags.custom_tags import ParametersProcessor


def test_parameters_processor():
    parameters = {"Resistance": "1 kΩ", "Tolerance": "1%"}
    pp = ParametersProcessor(part=None, parameters=parameters)
    assert pp.show_value == "1kΩ±1%"


def test_remaining():
    parameters = {"Resistance": "1 kΩ", "Tolerance": "1%", "Other": "foo", "Maximum output current": "1 A"}
    pp = ParametersProcessor(part=None, parameters=parameters)
    assert pp.max_output_current == "I<sub>out</sub>&leq;1A"
    assert set(pp.remaining_items) == {"1kΩ±1%", "Other: foo"}


def test_input_voltage_range():
    parameters: dict[str, str] = {}
    pp = ParametersProcessor(part=None, parameters=parameters)
    assert pp.input_voltage_range is None


if __name__ == "__main__":
    pytest_bazel.main()
