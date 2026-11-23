import json
from email import policy
from email.parser import BytesParser

import pytest
import pytest_bazel
import requests
from inventree.api import InvenTreeAPI

from inventree_utils.beautifier.upload_lcsc_images import upload_lcsc_images


def test_upload_confirmed_lcsc_image_as_multipart(monkeypatch: pytest.MonkeyPatch) -> None:
    host = "https://inventory.example"
    product_url = "https://www.lcsc.com/product-detail/C12345.html"
    image_url = "https://images.example/product.jpg"
    image_bytes = b"\xff\xd8synthetic LCSC image\xff\xd9"
    part = {"pk": 7, "name": "Resistor", "link": product_url, "thumbnail": None}
    json_responses = {
        f"{host}/api/": {"server": "InvenTree", "apiVersion": InvenTreeAPI.getMinApiVersion()},
        f"{host}/api/user/me/": {"username": "test"},
        f"{host}/api/part/": [part],
        f"{host}/api/company/part/": [{"pk": 9, "part": 7, "supplier": 2, "SKU": "C12345"}],
        f"{host}/api/company/": [{"pk": 2, "name": "LCSC"}],
    }
    confirmed = False
    uploads = []

    def confirm(prompt: str) -> str:
        nonlocal confirmed
        assert prompt == "Upload image to InvenTree? [y/n/a/q] "
        confirmed = True
        return "y"

    def send(_session: requests.Session, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        assert request.url is not None
        url = request.url.partition("?")[0]
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        if request.method == "PATCH":
            assert confirmed
            assert url == f"{host}/api/part/7/"
            content_type = request.headers["Content-Type"]
            assert content_type.startswith("multipart/form-data;")
            assert isinstance(request.body, bytes)
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + request.body
            )
            (image,) = message.iter_parts()
            assert image.get_param("name", header="Content-Disposition") == "image"
            uploads.append((image.get_filename(), image.get_payload(decode=True)))
            response._content = json.dumps(part).encode()
        else:
            assert request.method == "GET"
            if url == product_url:
                response._content = f'<meta property="og:image" content="{image_url}">'.encode()
            elif url == image_url:
                assert confirmed
                response.headers["Content-Type"] = "image/jpeg"
                response._content = image_bytes
            else:
                response._content = json.dumps(json_responses[url]).encode()
        return response

    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(requests.Session, "send", send)
    upload_lcsc_images(InvenTreeAPI(host, token="test-token"))

    assert uploads == [("lcsc.jpg", image_bytes)]


if __name__ == "__main__":
    pytest_bazel.main()
