# R2 Relay

Cloudflare R2 Multipart Upload 的 Python 本地 HTTPS 代理与 VPS MD5 内容缓存中继。

## 当前实现（开发分支，尚未合并到 main）

- 本地：`127.0.0.1:18585` 显式代理、仅对 R2 UploadPart 做 TLS MITM、完整请求体 MD5、HIT/MISS 协商、真实 R2 HTTP 结果透传、VPS 不可用回退。
- VPS：HTTPS `8443`（Caddy TLS 反代至回环 API）、磁盘 MD5 缓存、固定 1800 秒 TTL、重启扫描恢复、R2 PUT Worker 最多额外三次重试。
- 安全边界：仅 MD5 内容缓存键；非 UploadPart 正常透传；不接管 CompleteMultipartUpload；不使用数据库。
- 可观测性：鉴权指标 `GET /v1/metrics`、缓存与 R2 重试日志。
- 离线：真实 aiohttp → mitmdump → TLS VPS → 模拟 R2 跨进程集成，以及可选 100×10 MiB 模拟基准。

**代码开发与离线模拟不等于生产验收。** 当前没有可用真实 VPS / R2 测试环境，也未部署生产或取得实际链路带宽指标。

## 文档

- [正式需求](docs/requirements/REQUIREMENTS.md) · [验收矩阵](docs/requirements/ACCEPTANCE.md)
- [架构](docs/architecture/ARCHITECTURE.md) · [API](docs/architecture/API_PROTOCOL.md)
- [离线验收记录](docs/OFFLINE_ACCEPTANCE.md) · [部署物料](docs/DEPLOYMENT.md)
- [开发 Agent 规范](AGENTS.md)

## RC1 离线审计与演练

- [RC1 审计结论及待验收项](docs/RC1_AUDIT.md)
- [RC1 单命令验证](scripts/rc1_offline.py)（完全使用模拟 R2，不触发外网云端上传）

## 运行本地模拟

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e './vps[test]' -e './local[test]'
python -m pytest -q vps/tests local/tests
python benchmarks/offline_benchmark.py --parts 100 --part-mib 10 --parallel 8
```

离线测试包含真实 TLS CONNECT，但所有 R2 PUT 使用模拟 Transport，不会写入实际 Cloudflare R2。
