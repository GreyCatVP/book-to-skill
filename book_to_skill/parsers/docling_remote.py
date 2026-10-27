"""Dependency-free client for the docling-serve v1 conversion API."""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class RemoteDoclingOptions:
    base_url: str
    timeout: float = 300
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        try:
            url = urlsplit(self.base_url)
            valid = (
                url.scheme in {"http", "https"} and url.hostname and url.port != 0
                and not url.username and not url.password
                and not url.query and not url.fragment
                and not any(c.isspace() or ord(c) < 32 for c in self.base_url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Docling URL must be an HTTP(S) base URL without credentials, query, or fragment")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Docling timeout must be a positive finite number of seconds")
        if self.api_key and any(ord(c) < 32 or ord(c) > 126 for c in self.api_key):
            raise ValueError("Docling API key must contain only printable ASCII characters")


class RemoteDoclingError(RuntimeError):
    """A safe diagnostic: never includes response bodies or credentials."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward document contents or API keys to another endpoint.
        return None


def extract_with_remote_docling(pdf_path: str, options: RemoteDoclingOptions) -> str:
    """Upload one PDF and return Markdown, matching the local Docling options."""
    path = Path(pdf_path)
    payload = {
        "sources": [{
            "kind": "file", "filename": path.name,
            "base64_string": base64.b64encode(path.read_bytes()).decode("ascii"),
        }],
        "options": {
            "to_formats": ["md"], "do_ocr": False,
            "do_table_structure": True, "image_export_mode": "placeholder",
        },
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if options.api_key:
        headers["X-Api-Key"] = options.api_key
    request = Request(
        options.base_url.rstrip("/") + "/v1/convert/source",
        data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with build_opener(_NoRedirects()).open(request, timeout=options.timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise RemoteDoclingError(f"Remote Docling returned HTTP {code}") from None
    except (URLError, OSError, HTTPException):
        raise RemoteDoclingError("Remote Docling connection failed or timed out") from None
    except (ValueError, UnicodeError):
        raise RemoteDoclingError("Remote Docling returned invalid JSON") from None

    if not isinstance(result, dict) or result.get("status") != "success":
        raise RemoteDoclingError("Remote Docling conversion did not complete successfully")
    document = result.get("document")
    markdown = document.get("md_content") if isinstance(document, dict) else None
    if not isinstance(markdown, str) or not markdown.strip():
        raise RemoteDoclingError("Remote Docling returned no Markdown")
    return markdown
