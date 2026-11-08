# R2 Relay

Cloudflare R2 Multipart Upload 的 Python 本地 HTTPS 代理与 VPS MD5 内容缓存中继。

## 当前状态：RC1 离线验收基线已合并 main

- 本地：`127.0.0.1:18585` 显式代理、仅对 R2 UploadPart 做 TLS MITM、完整请求体 MD5、HIT/MISS 协商、真实 R2 HTTP 结果透传、VPS 不可用回退。
- VPS：HTTPS `8443`（Caddy TLS 反代至回环 API）、磁盘 MD5 缓存、固定 1800 秒 TTL、重启扫描恢复、R2 PUT Worker 最多额外三次重试。
- 安全边界：仅 MD5 内容缓存键；非 UploadPart 正常透传；不接管 CompleteMultipartUpload；不使用数据库。
- 可观测性：鉴权指标 `GET /v1/metrics`、缓存与 R2 重试日志。
- 离线：真实 aiohttp → mitmdump → TLS VPS → 模拟 R2 跨进程集成，以及可选 100×10 MiB 模拟基准。

**RC1 已通过 GitHub Runner 全栈离线验收，并通过 [PR #15](https://github.com/sunxu/r2-relay/pull/15) 合并到 `main`（Merge Commit `18e068a923238bccb83b2e8a77fe25ac4f7abb32`）。** 不代表生产就绪：真实公网 VPS、Cloudflare R2 SigV4/Multipart Complete 和真实带宽均为 `NOT_TESTED`，未生产部署。

## 文档

- [正式需求](docs/requirements/REQUIREMENTS.md) · [验收矩阵](docs/requirements/ACCEPTANCE.md)
- [架构](docs/architecture/ARCHITECTURE.md) · [API](docs/architecture/API_PROTOCOL.md)
- [离线验收记录](docs/OFFLINE_ACCEPTANCE.md) · [部署物料](docs/DEPLOYMENT.md)
- [开发 Agent 规范](AGENTS.md)
- [部署前离线预检（不需 VPS/R2）](docs/DEPLOYMENT_PREFLIGHT_CN.md) · [GitHub Runner 真进程全栈验收](docs/RUNNER_FULLSTACK_CN.md)
- [真实 VPS/R2 协议验收（待环境）](https://github.com/sunxu/r2-relay/issues/16) · [真实网络性能验收（待环境）](https://github.com/sunxu/r2-relay/issues/17)

## RC1 离线审计与演练

- [RC1 审计结论及待验收项](docs/RC1_AUDIT.md)
- [RC1 单命令验证](scripts/rc1_offline.py)（完全使用模拟 R2，不触发外网云端上传）

## 运行本地模拟

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/rc1-direct.txt
python -m pip install -e ./vps -e ./local
python scripts/rc1_offline.py
```

离线测试包含真实 TLS CONNECT；独立 [Runner 全栈工作流](.github/workflows/runner-fullstack.yml)还会启动真实 Caddy HTTPS `8443`、VPS 进程和独立 Mock R2 HTTP 服务。两类测试均不连接真实 Cloudflare R2，不能证明公网吞吐。
