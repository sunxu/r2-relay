"""Isolated real mitmproxy integration: actual R2RelayAddon with stub VPS response."""
import hashlib
import json
import os
from pathlib import Path

from r2relay_local.addon import R2RelayAddon
from r2relay_local.relay import UpstreamResult


class StubVpsRelay:
    async def upload(self, url, headers, body):
        md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
        Path(os.environ["MITM_CAPTURE_RESULT"]).write_text(
            json.dumps({
                "url": url, "method": "PUT", "length": len(body), "md5": md5,
                "headers": headers,
            }),
            encoding="utf-8",
        )
        return UpstreamResult(
            status=200,
            headers=[("ETag", '"mitm-real-connect-proof"'), ("X-Captured-Md5", md5)],
            body=b"",
        )


addons = [R2RelayAddon(StubVpsRelay())]
