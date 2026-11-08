#!/usr/bin/env python3
"""Non-network in-process smoke test as r2relay. Never uploads to R2."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/r2-relay/vps")

def main():
    if os.geteuid() == 0:
        raise RuntimeError("root_execution_forbidden")
    from fastapi.testclient import TestClient
    from r2relay_vps.app import create_app
    from r2relay_vps.cache import CacheStore
    # No production token or cache is accessed.
    with tempfile.TemporaryDirectory(prefix="r2-smoke-") as tmp:
        app = create_app(store=CacheStore(Path(tmp)), token="SMOKE_FAKE_LOCAL_TOKEN_ONLY")
        with TestClient(app) as client:
            health = client.get("/healthz")
            unauth = client.get("/v1/metrics")
            auth = client.get("/v1/metrics",
                headers={"Authorization": "Bearer SMOKE_FAKE_LOCAL_TOKEN_ONLY"})
            ok = (health.status_code == 200 and health.json().get("status") == "ok"
                  and unauth.status_code == 401 and auth.status_code == 200)
            return {"status": "RUNTIME_SMOKE_PASS" if ok else "RUNTIME_SMOKE_FAIL",
                    "health_status": health.status_code,
                    "unauthorized_metrics": unauth.status_code,
                    "authorized_metrics": auth.status_code,
                    "network_used": False, "real_r2_tested": False,
                    "production_token_accessed": False,
                    "service_started": False}

if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        result = {"status": "RUNTIME_SMOKE_FAIL",
                  "error_type": type(exc).__name__,
                  "network_used": False, "real_r2_tested": False}
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "RUNTIME_SMOKE_PASS" else 1)
