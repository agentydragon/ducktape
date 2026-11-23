import pytest_bazel
from django.template import Context, Engine

from inventree_utils.rai_plugin.templatetags.custom_tags import ParametersProcessor


def test_label_tag_displays_and_consumes_parameters():
    engine = Engine(libraries={"custom_tags": "inventree_utils.rai_plugin.templatetags.custom_tags"})
    label = engine.from_string(
        "{% load custom_tags %}{% parameters_processor as pp %}"
        "{{ pp.max_output_current }}|"
        "{{ pp.forward_voltage_at_current }}|"
        "{% for item in pp.remaining_items %}{{ item }}|{% endfor %}"
    )
    parameters = {
        "Reverse leakage current @ voltage (I_R)": "2 µA @ 5 V",
        "Other": "foo < bar",
        "Resistance": "1 kΩ",
        "Tolerance": "1%",
        "Maximum output current": "1 A",
        "Forward voltage @ current": "0.7 V @ 1 A",
    }
    html = label.render(Context({"part": None, "parameters": parameters}))
    assert html == "I<sub>out</sub>&leq;1A|V<sub>F</sub>=0.7V@1A|1kΩ±1%|I<sub>R</sub>=2µA@5V|Other: foo &lt; bar|"


def test_input_voltage_range():
    parameters: dict[str, str] = {}
    pp = ParametersProcessor(part=None, parameters=parameters)
    assert pp.input_voltage_range is None


if __name__ == "__main__":
    pytest_bazel.main()
