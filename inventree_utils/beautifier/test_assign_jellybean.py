from unittest.mock import Mock

import pytest_bazel
from inventree.api import InvenTreeAPI
from inventree.base import ParameterTemplate

from inventree_utils.beautifier.assign_jellybean import get_parts_without_parameter, parse_file_lines


def test_parse_file_lines():
    lines = ("# comment", "   # also comment", "foo bar baz", "a b c")
    assert list(parse_file_lines(lines)) == [["foo", "bar", "baz"], ["a", "b", "c"]]


def test_only_assigned_parts_are_excluded():
    api = Mock(spec=InvenTreeAPI, api_version=500)
    template = ParameterTemplate(api, data={"pk": 7})
    parameters = [
        {"pk": 5, "template": 7, "model_type": "part", "model_id": 1},
        {"pk": 6, "template": 7, "model_type": "stockitem", "model_id": 2},
    ]

    def get(url, params):
        if url == "part/":
            return [{"pk": 1}, {"pk": 2}]
        assert url == "parameter/"
        return [row for row in parameters if all(row[key] == value for key, value in params.items() if key in row)]

    api.get.side_effect = get
    assert [part.pk for part in get_parts_without_parameter(api, template)] == [2]


if __name__ == "__main__":
    pytest_bazel.main()
