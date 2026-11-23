from unittest.mock import Mock, call

import pytest_bazel
from inventree.api import InvenTreeAPI
from inventree.base import ParameterTemplate
from inventree.part import Part

from inventree_utils.samplebooks_import import import_samplebooks2 as importer
from inventree_utils.samplebooks_import.samplebooks_parts_data import Capacitor, pf


def test_created_parameters_match_on_reimport(capsys):
    api = Mock(spec=InvenTreeAPI, api_version=500, api_url="https://inventory.invalid/api/")
    cap = Capacitor(
        package="0402", capacitance=100 * pf, tolerance=None, dielectric="X7R", voltage_rating=6.3, description=None
    )
    template_names = ["Package", "Capacitance", "Dielectric", "Voltage rating"]
    templates = {
        name: ParameterTemplate(api, data={"pk": pk, "choices": ""}) for pk, name in enumerate(template_names, start=1)
    }
    # The category supplied its own package parameter when the part was created.
    package = {"pk": 81, "model_type": "part", "model_id": 20, "template": 1, "data": ""}
    api.get.return_value = [package]
    api.post.side_effect = lambda url, data: {"pk": 20, **data}
    api.patch.return_value = {**package, "data": "SMD 0402"}
    created = importer.create_part_in_inventree(api, cap, templates)

    api.get.assert_called_once_with(
        url="parameter/", params={"model_type": "part", "model_id": 20, "limit": 0, "raise_error": True}
    )
    api.patch.assert_called_once()
    endpoint, patch_data = api.patch.call_args.args
    assert InvenTreeAPI.constructApiUrl(api, endpoint) == "https://inventory.invalid/api/parameter/81/"
    assert patch_data == {"data": "SMD 0402"}
    rows = [
        {"model_type": "part", "model_id": 20, "template": pk, "data": value}
        for pk, value in [(2, "100 pF"), (3, "X7R"), (4, "6.3 V")]
    ]
    assert api.post.call_args_list[1:] == [call("parameter/", row) for row in rows]

    part_data = {"pk": 20, "name": created.name, "category": created.category, "description": "edited by owner"}
    parameters = [api.patch.return_value, *rows, {"template": 10, "data": "operator value"}]
    assert importer.part_matches_in_db(Part(api, data={**part_data, "parameters": parameters}), cap, templates)
    rows[-1]["data"] = "6 V"
    assert not importer.part_matches_in_db(Part(api, data={**part_data, "parameters": parameters}), cap, templates)
    assert "[Voltage rating] differs" in capsys.readouterr().out


if __name__ == "__main__":
    pytest_bazel.main()
