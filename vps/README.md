# VPS Core — M1 开发版本（未生产验收）

实现内容：MD5-only 固定 1800 秒磁盘缓存、重启扫描、Prepare/MISS/HIT API、持久化数据上传、R2 预签名 PUT Worker、最多额外 3 次重试、真实 HTTP 状态/ETag/响应体查询。

**仍不建议生产接入：** 这是 M1 原型，尚未完成公网 HTTPS :8443 部署、资源和执行任务限额、原始 Python HTTPS 代理、真实 R2 完整链路测试、原始响应兼容性和故障注入验收。当前只有模拟 R2 的自动化测试。

本地运行测试：
```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e './vps[test]'
cd vps && pytest -q
```

仅在隔离开发环境启动：
```bash
export R2_RELAY_TOKEN='dev-only-token'
export R2_RELAY_CACHE_DIR=/tmp/r2-relay-cache
uvicorn r2relay_vps.app:create_app --factory --app-dir vps --host 127.0.0.1 --port 8000
```

生产网络入口端口要求为 VPS HTTPS :8443，但此开发命令只在 loopback 暴露明文 HTTP，不能直接公网开放。
