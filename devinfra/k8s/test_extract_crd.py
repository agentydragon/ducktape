import textwrap

import pytest
import pytest_bazel
import yaml

from devinfra.k8s.extract_crd import _remove_path, go_raw_string, wrap_schema

# Shape of kubevirt's validations_generated.go / CDI's crds_generated.go.
_GO_SOURCE = textwrap.dedent(
    """\
    package components

    var CRDsValidation map[string]string = map[string]string{
    \t"virtualmachine": `openAPIV3Schema:
      properties:
        spec:
          type: object
      type: object
    `,
    \t"virtualmachineinstance": `openAPIV3Schema:
      type: object
    `,
    }
    """
)


def test_remove_path_supports_mappings_and_array_indexes() -> None:
    document = {"versions": [{"schema": {"properties": {"provider": {"keepersecurity": {"enabled": True}}}}}]}

    _remove_path(document, "versions.0.schema.properties.provider.keepersecurity")

    assert document == {"versions": [{"schema": {"properties": {"provider": {}}}}]}


def test_go_raw_string_takes_only_the_keyed_value() -> None:
    assert yaml.safe_load(go_raw_string(_GO_SOURCE, "virtualmachine")) == {
        "openAPIV3Schema": {"properties": {"spec": {"type": "object"}}, "type": "object"}
    }


def test_go_raw_string_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="'virtualmachinepool'"):
        go_raw_string(_GO_SOURCE, "virtualmachinepool")


def test_wrap_schema_splits_name_into_plural_and_group() -> None:
    schema = {"openAPIV3Schema": {"type": "object"}}

    crd = wrap_schema(schema, name="virtualmachines.kubevirt.io", kind="VirtualMachine", version="v1")

    assert crd["spec"] == {
        "group": "kubevirt.io",
        "names": {
            "kind": "VirtualMachine",
            "listKind": "VirtualMachineList",
            "plural": "virtualmachines",
            "singular": "virtualmachine",
        },
        "scope": "Namespaced",
        "versions": [{"name": "v1", "served": True, "storage": True, "schema": schema}],
    }


if __name__ == "__main__":
    pytest_bazel.main()
