# VPS Core — 开发中的阶段性实现

本目录是 M1 的首批实现，包含 MD5-only 固定 1800 秒磁盘缓存、重启扫描、Prepare/MISS/HIT API、分片存储与基础测试。

**尚未实现 R2 UploadPart Worker、重试与 R2 响应透传，因此目前绝不可接入正式原始 aiohttp 上传程序或生产部署。** `/v1/executions` 此阶段只会返回 `QUEUED`，不会返回伪造成功。

本地测试：
```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e './vps[test]'
cd vps && pytest -q
```

受控开发环境启动（仅测试用途）：
```bash
export R2_RELAY_TOKEN='dev-only-token'
export R2_RELAY_CACHE_DIR=/tmp/r2-relay-cache
uvicorn r2relay_vps.app:create_app --factory --app-dir vps --host 127.0.0.1 --port 8000
```

公网 TLS 8443 网关、上传执行队列与预签名 URL 实际 PUT 属于后续 M1 工作。
