# 离线开发验收记录

状态：主要离线模拟完成，生产验收待真实 VPS/R2 环境。所有通过结论限定在 GitHub Actions/Linux Runner 与模拟 R2 场景。

## 已验证的项目

- VPS: MD5 缓存写入/重建、固定 1800 秒到期、权限与 507 磁盘失败、执行状态和容量限制、最多额外 3 次重试、真实格式的状态/ETag 响应。
- Local: R2 目标分类、请求体完整 MD5、预算预留、非目标 HTTPS 隧道、建连失败回退和已提交后的结果未知隔离。
- 网络栈：真实 aiohttp 客户端通过真实 mitmdump 进程 CONNECT/TLS、合并 CA，访问回环 HTTPS VPS，再通过 VPS MockTransport 模拟 R2；验证首次 MISS、重复 MD5 HIT、502→200、每次 R2 PUT 的请求体长度及 MD5。
- 可观测性：`/v1/metrics` Bearer Token 鉴权、HIT/MISS、接收字节和 R2 请求统计。
- 离线命令：`python -m pytest -q vps/tests local/tests`；可选基准 `python benchmarks/offline_benchmark.py --parts 100 --part-mib 10 --parallel 8`。

## 100 × 10 MiB 离线模拟证据

GitHub Actions [运行 42395100244](https://github.com/sunxu/r2-relay/actions/runs/42395100244)（成功）：

| 指标 | 模拟结果 |
|---|---:|
| 分片数 | 100 |
| 每片大小 | 10 MiB = 10,485,760 B |
| 并发 | 8 |
| 首次写入 VPS 缓存正文 | 1,048,576,000 B |
| 再次命中相同 MD5 时传输正文 | **0 B** |
| VPS 缓存文件 | 100 |
| 模拟 R2 PUT 总数 | 200 |
| 首次模拟耗时 | 11.514 s |
| 重复命中模拟耗时 | 4.845 s |

**这些耗时是在 GitHub Runner 内存和磁盘/ASGI 模拟环境下取得，不代表真实 100 Mbps 本地上行、1000 Mbps VPS 出口或 Cloudflare R2 延迟。** 未使用真实 R2 签名、真实 VPS 8443 或实际分片合并。

## 未完成（需环境）

- 独立测试 Bucket、实际预签名 URL/签名头/ETag/CompleteMultipartUpload 兼容。
- VPS 公网 HTTPS 8443、Caddy HTTP/2 与 100 Mbps 带宽测量。
- 在真实链路注入网络断开、502/超时以及多层请求重试。
- 在真实原始 aiohttp 客户端上执行长时间稳定性和取消压力测试。

生产上线及 PR 合并仍需审核。本文件不声称真实性能或生产就绪。
