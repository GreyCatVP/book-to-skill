import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError

import pytest

from book_to_skill.parsers import docling_remote as remote


def invoke(tmp_path, monkeypatch, result=None, error=None, **kwargs):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsynthetic")
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            if error:
                raise error
            return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(remote, "build_opener", lambda *_: Opener())
    options = remote.RemoteDoclingOptions("https://docling.example/proxy/", **kwargs)
    return remote.extract_with_remote_docling(str(pdf), options), calls


def test_v1_request_and_markdown_contract(tmp_path, monkeypatch):
    text, calls = invoke(
        tmp_path, monkeypatch,
        {"status": "success", "document": {"md_content": "# Book\n\n| A | B |"}},
        timeout=42, api_key="test-secret",
    )
    assert text == "# Book\n\n| A | B |"
    request, timeout = calls[0]
    assert request.full_url == "https://docling.example/proxy/v1/convert/source"
    assert request.method == "POST"
    assert request.get_header("X-api-key") == "test-secret"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 42
    body = json.loads(request.data)
    source, = body["sources"]
    assert source["kind"] == "file" and source["filename"] == "book.pdf"
    assert base64.b64decode(source["base64_string"]) == b"%PDF-1.4\nsynthetic"
    assert body["options"] == {
        "to_formats": ["md"], "do_ocr": False,
        "do_table_structure": True, "image_export_mode": "placeholder",
    }


@pytest.mark.parametrize("result", [
    {}, [], {"status": "failure"},
    {"status": "partial_success", "document": {"md_content": "incomplete"}},
    {"status": "success", "document": None},
    {"status": "success", "document": {"md_content": " "}},
    {"status": "success", "document": {"md_content": 123}},
])
def test_rejects_failed_partial_or_empty_results(tmp_path, monkeypatch, result):
    with pytest.raises(remote.RemoteDoclingError):
        invoke(tmp_path, monkeypatch, result)


@pytest.mark.parametrize("error, message", [
    (HTTPError("https://secret", 401, "private", {}, io.BytesIO(b"private")), "HTTP 401"),
    (URLError("private credentials"), "connection failed or timed out"),
    (TimeoutError("private credentials"), "connection failed or timed out"),
])
def test_safe_network_errors(tmp_path, monkeypatch, error, message):
    with pytest.raises(remote.RemoteDoclingError, match=message) as exc:
        invoke(tmp_path, monkeypatch, error=error)
    assert "private" not in str(exc.value)


def test_invalid_json(tmp_path, monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            return io.BytesIO(b"not JSON: private server response")

    monkeypatch.setattr(remote, "build_opener", lambda *_: Opener())
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"synthetic")
    with pytest.raises(remote.RemoteDoclingError, match="invalid JSON"):
        remote.extract_with_remote_docling(str(pdf), remote.RemoteDoclingOptions("http://localhost:5001"))


@pytest.mark.parametrize("url", [
    "file:///tmp/docling", "https://", "https://user:secret@host",
    "https://host?secret=x", "https://host#fragment", "http://host:bad",
    "http://host:0", "http://ho st", "http://host\n",
])
def test_rejects_invalid_base_urls(url):
    with pytest.raises(ValueError, match="Docling URL"):
        remote.RemoteDoclingOptions(url)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout"):
        remote.RemoteDoclingOptions("https://host", timeout=timeout)


def test_api_key_is_not_in_repr_and_rejects_header_injection():
    assert "secret" not in repr(remote.RemoteDoclingOptions("https://host", api_key="secret"))
    with pytest.raises(ValueError, match="API key"):
        remote.RemoteDoclingOptions("https://host", api_key="secret\r\nHeader: value")


def test_does_not_follow_redirects():
    assert remote._NoRedirects().redirect_request(None, None, 302, "", {}, "https://other") is None


@pytest.mark.parametrize("redirect", [False, True])
def test_real_http_upload_and_redirect_refusal(tmp_path, redirect):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            if redirect:
                self.send_response(307)
                self.send_header("Location", "/must-not-receive-document")
            else:
                self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "document": {"md_content": "# Synthetic book"}}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"synthetic")
    options = remote.RemoteDoclingOptions(f"http://127.0.0.1:{server.server_port}", api_key="secret")
    try:
        if redirect:
            with pytest.raises(remote.RemoteDoclingError, match="HTTP 307"):
                remote.extract_with_remote_docling(str(pdf), options)
        else:
            assert remote.extract_with_remote_docling(str(pdf), options) == "# Synthetic book"
        assert len(received) == 1
        path, headers, body = received[0]
        assert path == "/v1/convert/source"
        assert headers["X-Api-Key"] == "secret"
        assert base64.b64decode(body["sources"][0]["base64_string"]) == b"synthetic"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
