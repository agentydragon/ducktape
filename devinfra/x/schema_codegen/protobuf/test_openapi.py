import json

import pytest_bazel

from util.bazel.runfiles import get_required_path, own_repo_rlocation


def test_generated_openapi2_describes_the_proto_http_route() -> None:
    document = json.loads(
        get_required_path(
            own_repo_rlocation("devinfra/x/schema_codegen/protobuf/sample_openapi.swagger.json")
        ).read_text()
    )

    operation = document["paths"]["/v1/samples/{sampleId}"]["get"]
    response_ref = operation["responses"]["200"]["schema"]["$ref"]
    assert response_ref.endswith("Sample")
    assert any(parameter["name"] == "sampleId" and parameter["in"] == "path" for parameter in operation["parameters"])

    sample = document["definitions"][response_ref.rsplit("/", maxsplit=1)[-1]]
    sequence = sample["properties"]["sequence"]
    assert sequence["type"] == "string"
    assert sequence["format"] == "int64"
    assert sample["properties"]["observedAt"] == {"type": "string", "format": "date-time"}
    assert sample["properties"]["metadata"]["$ref"].endswith("CommonMetadata")


if __name__ == "__main__":
    pytest_bazel.main()
