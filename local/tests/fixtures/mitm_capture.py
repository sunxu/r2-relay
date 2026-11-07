"""Isolated mitmproxy fixture: proves full HTTPS request buffering, not real R2."""
import hashlib
import json
import os
from pathlib import Path

from mitmproxy import http


class ProofAddon:
    def request(self, flow: http.HTTPFlow):
        body = flow.request.raw_content
        digest = hashlib.md5(body, usedforsecurity=False).hexdigest() if body is not None else None
        Path(os.environ["MITM_CAPTURE_RESULT"]).write_text(
            json.dumps({
                "url": flow.request.pretty_url,
                "method": flow.request.method,
                "length": len(body) if body is not None else None,
                "md5": digest,
            }),
            encoding="utf-8",
        )
        flow.response = http.Response.make(
            200, b"", {"ETag": '"mitm-real-connect-proof"', "X-Captured-Md5": digest or ""}
        )


addons = [ProofAddon()]
