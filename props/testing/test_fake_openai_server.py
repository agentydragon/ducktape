import httpx
import pytest
import pytest_bazel

from openai_utils.model import OpenAIModelProto, ResponsesRequest, ResponsesResult
from props.testing.fake_openai_server import FakeOpenAIServer
from util.testing.asgi import bind_http_server


class RaisingOpenAIModel(OpenAIModelProto):
    model = "raising-model"

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        raise RuntimeError("mock failure")


async def test_mock_error_is_raised_after_http_server_stops() -> None:
    fake_openai = FakeOpenAIServer({RaisingOpenAIModel.model: RaisingOpenAIModel()})

    with bind_http_server() as endpoint:
        async with endpoint.serve_in_loop(fake_openai.create_app()) as origin:
            async with httpx.AsyncClient(base_url=origin) as client:
                response = await client.post("/v1/responses", json={"model": RaisingOpenAIModel.model, "input": []})

            assert response.status_code == 500

    with pytest.raises(RuntimeError, match="mock failure"):
        fake_openai.check_errors()


if __name__ == "__main__":
    pytest_bazel.main()
