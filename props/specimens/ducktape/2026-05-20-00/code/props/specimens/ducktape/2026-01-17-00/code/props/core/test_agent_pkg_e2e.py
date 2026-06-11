"""E2E test for agent building and running custom agent images.

Tests the full workflow of an agent creating its own variant:
1. Pull existing critic manifest via proxy HTTP API
2. Create custom agent.md content with random token (prevents cross-test interference)
3. Create new OCI layer with the custom content
4. Push manifest by digest via proxy HTTP API
5. Proxy automatically creates agent_definitions row
6. Run the newly created agent image
7. Verify new agent got the custom agent.md in its system message
8. Calling agent reads output of called agent via psql
"""

from __future__ import annotations

import secrets
import textwrap

import pytest
from hamcrest import assert_that

from agent_core_testing.responses import PlayGen, tool_roundtrip
from agent_core_testing.steps import exited_successfully, stdout_contains
from mcp_infra.naming import MCPMountPrefix
from openai_utils.model import ResponsesRequest
from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.db.models import AgentDefinition
from props.core.db.session import get_session
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.core.prompt_optimize.prompt_optimizer import RunCriticInput, RunCriticOutput, run_prompt_optimizer
from props.core.prompt_optimize.target_metric import TargetMetric
from props.testing.mocks import PropsMock


def make_po_builder_mock(random_token: str) -> PropsMock:
    """Mock PO agent that builds and pushes a custom critic variant.

    Uses Python requests library to interact with OCI registry via HTTP API.
    The workflow follows the design doc:
    1. Pull existing critic:latest manifest
    2. Create modified agent.md with random token
    3. Push manifest by digest (proxy writes agent_definitions row)
    4. (Future) Use psql to read the custom critic's output after it runs

    Args:
        random_token: Unique token to embed in agent.md (prevents cross-test interference)
    """

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request

        # Step 1: Pull existing critic manifest via proxy
        pull_script = textwrap.dedent("""
            import os
            import json
            import requests
            from requests.auth import HTTPBasicAuth

            auth = HTTPBasicAuth(os.environ['PGUSER'], os.environ['PGPASSWORD'])
            proxy_host = os.environ.get('PROPS_REGISTRY_PROXY_HOST', '127.0.0.1')
            proxy_port = os.environ.get('PROPS_REGISTRY_PROXY_PORT', '5051')
            proxy_url = f"http://{proxy_host}:{proxy_port}"

            # Get manifest for critic:latest
            manifest_url = f"{proxy_url}/v2/critic/manifests/latest"
            headers = {"Accept": "application/vnd.oci.image.manifest.v1+json"}
            resp = requests.get(manifest_url, headers=headers, auth=auth, timeout=10)
            resp.raise_for_status()

            manifest = resp.json()
            manifest_digest = resp.headers.get('Docker-Content-Digest')

            print(f"MANIFEST_DIGEST={manifest_digest}")
            print(f"LAYERS={len(manifest.get('layers', []))}")
            print(f"CONFIG_DIGEST={manifest.get('config', {}).get('digest')}")
        """)

        result = yield from m.docker_exec_roundtrip(["python3", "-c", pull_script])
        assert_that(result, exited_successfully())
        assert_that(result, stdout_contains("MANIFEST_DIGEST=sha256:"))
        assert_that(result, stdout_contains("LAYERS="))

        # Step 2-4: Create OCI layer with custom agent.md and push to registry
        # This is the full workflow: create tar, upload blob, create manifest, push manifest
        create_and_push_script = textwrap.dedent(f"""
            import os
            import json
            import hashlib
            import tarfile
            import gzip
            import tempfile
            import requests
            from requests.auth import HTTPBasicAuth
            from io import BytesIO

            auth = HTTPBasicAuth(os.environ['PGUSER'], os.environ['PGPASSWORD'])
            proxy_host = os.environ.get('PROPS_REGISTRY_PROXY_HOST', '127.0.0.1')
            proxy_port = os.environ.get('PROPS_REGISTRY_PROXY_PORT', '5051')
            proxy_url = f"http://{{proxy_host}}:{{proxy_port}}"

            # Step 2a: Create agent.md with random token
            agent_md_content = '''# Custom Critic Variant - {random_token}

You are a test custom critic with unique token: {random_token}

When reviewing code:
1. Always report exactly zero issues
2. Use message "Custom critic {random_token} completed review"

## Available Commands
- `critique submit <count> <message>`: Submit your critique
'''

            # Step 2b: Create tar.gz layer containing agent.md
            tar_buffer = BytesIO()
            with gzip.open(tar_buffer, 'wb') as gz:
                with tarfile.open(fileobj=gz, mode='w') as tar:
                    # Add agent.md to tar
                    info = tarfile.TarInfo(name='agent.md')
                    info.size = len(agent_md_content.encode('utf-8'))
                    tar.addfile(info, BytesIO(agent_md_content.encode('utf-8')))

            layer_blob = tar_buffer.getvalue()
            layer_digest = "sha256:" + hashlib.sha256(layer_blob).hexdigest()
            layer_size = len(layer_blob)

            print(f"LAYER_DIGEST={{layer_digest}}")
            print(f"LAYER_SIZE={{layer_size}}")

            # Step 3: Upload blob to registry via OCI Distribution API
            # POST to start upload
            upload_url = f"{{proxy_url}}/v2/critic/blobs/uploads/"
            resp = requests.post(upload_url, auth=auth, timeout=10)
            resp.raise_for_status()

            # Extract upload location from response
            upload_location = resp.headers.get('Location')
            if not upload_location.startswith('http'):
                # Relative URL, make absolute
                upload_location = f"{{proxy_url}}{{upload_location}}"

            print(f"UPLOAD_LOCATION={{upload_location}}")

            # PUT the blob content
            put_url = f"{{upload_location}}&digest={{layer_digest}}"
            headers = {{"Content-Type": "application/octet-stream"}}
            resp = requests.put(put_url, data=layer_blob, headers=headers, auth=auth, timeout=30)
            resp.raise_for_status()

            print(f"BLOB_UPLOADED={{resp.status_code}}")

            # Step 4: Create and push manifest referencing the new layer
            # In reality we'd pull the base manifest and add our layer
            # For this test, we'll create a minimal valid manifest
            manifest = {{
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {{
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": "sha256:abc123placeholder",
                    "size": 123
                }},
                "layers": [
                    {{
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": layer_digest,
                        "size": layer_size,
                        "annotations": {{
                            "dev.props.layer_type": "agent_definition"
                        }}
                    }}
                ],
                "annotations": {{
                    "org.opencontainers.image.created": "2026-01-13T00:00:00Z",
                    "dev.props.custom_agent": "true",
                    "dev.props.random_token": "{random_token}"
                }}
            }}

            # Calculate manifest digest
            manifest_json = json.dumps(manifest, separators=(',', ':'), sort_keys=True)
            manifest_digest = "sha256:" + hashlib.sha256(manifest_json.encode()).hexdigest()

            # Push manifest by digest (not by tag!)
            # This triggers proxy to write agent_definitions row
            manifest_url = f"{{proxy_url}}/v2/critic/manifests/{{manifest_digest}}"
            headers = {{"Content-Type": "application/vnd.oci.image.manifest.v1+json"}}

            resp = requests.put(
                manifest_url,
                data=manifest_json,
                headers=headers,
                auth=auth,
                timeout=10
            )
            resp.raise_for_status()

            print(f"MANIFEST_DIGEST={{manifest_digest}}")
            print(f"STATUS={{resp.status_code}}")
        """)

        result = yield from m.docker_exec_roundtrip(["python3", "-c", create_and_push_script])
        assert_that(result, exited_successfully())
        assert_that(result, stdout_contains("LAYER_DIGEST=sha256:"))
        assert_that(result, stdout_contains("BLOB_UPLOADED=201"))
        assert_that(result, stdout_contains("MANIFEST_DIGEST=sha256:"))
        assert_that(result, stdout_contains("STATUS=2"))  # 200 or 201

        # Extract manifest digest from output
        manifest_digest = None
        stdout_text = result.stdout if isinstance(result.stdout, str) else str(result.stdout)
        for line in stdout_text.split("\n"):
            if line.startswith("MANIFEST_DIGEST="):
                manifest_digest = line.split("=", 1)[1]
                break
        assert manifest_digest is not None, f"Failed to extract manifest digest from: {stdout_text}"

        # Step 5: Use run_critic MCP tool to launch the custom critic
        example_spec = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug="test-fixtures")

        run_critic_input = RunCriticInput(definition_id=manifest_digest, example=example_spec, max_turns=200)

        # Yield the MCP tool call
        call = m.mcp_tool_call(MCPMountPrefix("prompt_eval"), "run_critic", run_critic_input)
        run_critic_output: RunCriticOutput = yield from tool_roundtrip(call, RunCriticOutput)
        critic_run_id = run_critic_output.critic_run_id

        # Step 6: Use psql to query the custom critic's output
        query_result = yield from m.psql_roundtrip(
            f"SELECT status FROM agent_runs WHERE agent_run_id = '{critic_run_id}'"
        )
        assert_that(query_result, exited_successfully())
        assert_that(query_result, stdout_contains("COMPLETED"))

        # Complete the builder agent
        yield from m.docker_exec_roundtrip(["prompt-optimize-dev", "report-success"])

    return mock


def make_custom_critic_mock(random_token: str) -> PropsMock:
    """Mock for the custom critic that was just created.

    Args:
        random_token: Token to check for in system message
    """

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        request: ResponsesRequest = yield None  # First request with system message  # type: ignore[assignment]

        # Verify system message (instructions field) contains the custom agent.md with random token
        instructions = request.instructions or ""
        assert random_token in instructions, f"Token {random_token} not found in instructions: {instructions[:500]}"

        # Custom critic executes its behavior from agent.md
        yield from m.docker_exec_roundtrip(
            ["critique", "submit", "0", f"Custom critic {random_token} completed review"]
        )

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
@pytest.mark.slow
async def test_po_builds_custom_critic(synced_test_db, async_docker_client, noop_openai_client):
    """Test PO agent builds custom critic image via MCP tool integration.

    This is ONE integrated e2e test where:
    1. PO agent creates and pushes custom critic image with random token
    2. PO agent uses run_critic MCP tool to launch the custom critic
    3. Custom critic mock verifies system message contains the token
    4. PO agent queries critic output via psql
    5. Proxy automatically creates agent_definitions row

    The custom critic mock is provided as critic_client parameter, so when
    the PO's MCP tool call launches a critic, it uses our custom mock.
    """
    # Generate unique random token for this test run (prevents cross-test interference)
    random_token = secrets.token_hex(8)

    # Create mocks
    po_builder_mock = make_po_builder_mock(random_token)
    custom_critic_mock = make_custom_critic_mock(random_token)

    # Run PO agent with custom critic mock for nested runs
    # The PO agent will:
    # 1. Create and push custom critic image
    # 2. Use run_critic MCP tool to launch it (will use custom_critic_mock)
    # 3. Query the result via psql
    await run_prompt_optimizer(
        budget=1.0,
        optimizer_client=po_builder_mock,
        critic_client=custom_critic_mock,  # Used when MCP tool launches critics
        grader_client=noop_openai_client,
        docker_client=async_docker_client,
        target_metric=TargetMetric.WHOLE_REPO,
        db_config=synced_test_db,
    )


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_critic_cannot_push_images(test_registry, test_snapshot, all_files_scope):
    """Test that critic agents cannot push images to registry.

    Only PO/PI agents should have registry write access.
    Critic attempting to push should get 403 Forbidden.
    """

    @PropsMock.mock()
    def critic_tries_push(m: PropsMock) -> PlayGen:
        yield None  # First request

        # Critic tries to push a manifest - should fail with 403
        push_script = textwrap.dedent("""
            import os
            import json
            import requests
            from requests.auth import HTTPBasicAuth

            auth = HTTPBasicAuth(os.environ['PGUSER'], os.environ['PGPASSWORD'])
            proxy_host = os.environ.get('PROPS_REGISTRY_PROXY_HOST', '127.0.0.1')
            proxy_port = os.environ.get('PROPS_REGISTRY_PROXY_PORT', '5051')

            manifest = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:test", "size": 123}
            }

            push_url = f"http://{proxy_host}:{proxy_port}/v2/critic/manifests/sha256:test123"
            headers = {"Content-Type": "application/vnd.oci.image.manifest.v1+json"}

            resp = requests.put(
                push_url,
                data=json.dumps(manifest),
                headers=headers,
                auth=auth,
                timeout=10
            )

            print(f"STATUS={resp.status_code}")

            # Expect 403 Forbidden
            if resp.status_code == 403:
                print("FORBIDDEN_AS_EXPECTED")
            else:
                print(f"UNEXPECTED_STATUS: {resp.status_code}")
        """)

        result = yield from m.docker_exec_roundtrip(["python3", "-c", push_script])
        assert_that(result, exited_successfully())
        assert_that(result, stdout_contains("STATUS=403"))
        assert_that(result, stdout_contains("FORBIDDEN_AS_EXPECTED"))

        yield from m.docker_exec_roundtrip(["critic-dev", "report-success"])

    run_id = await test_registry.run_critic(
        image_ref=CRITIC_IMAGE_REF, example=all_files_scope, client=critic_tries_push, max_turns=20
    )

    assert run_id is not None

    # Verify no agent_definitions were created by critic
    with get_session() as session:
        defns = session.query(AgentDefinition).filter_by(created_by_agent_run_id=run_id).all()
        assert len(defns) == 0, "Critic should not be able to create agent definitions"
