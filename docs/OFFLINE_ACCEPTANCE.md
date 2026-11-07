# 离线模拟与验收边界

状态：待最新 CI 证据更新。**离线测试不证明真实 R2 和 VPS 生产可用。**

## 组件与可复现命令
1. `cd vps && pytest -q`：磁盘缓存、TTL、进程重建、鉴权、任务/并发限制、R2 PUT 502/403、响应大小。
2. `cd local && pytest -q`：请求分类、MD5、内存预算、签名 URL 字节序、回退/未知结果。
3. `local/tests/test_real_aiohttp_connect.py`：真实 aiohttp→mitmdump CONNECT/TLS、合并原 CA+代理 CA、非 R2 直通。
4. `local/tests/test_real_fullstack.py`：真实 aiohttp→mitmdump addon→HTTPS VPS 进程→模拟 R2，覆盖首次 MISS、同 MD5 第二次 HIT、502→200、各自真实格式 ETag 及原始字节哈希。

## 不能从模拟推断的事实
- VPS 8443 公开访问延迟、HTTP/2 带宽、R2 网络故障与 502 具体模式。
- Cloudflare R2 对不同签名头、checksum、URL 过期与真实 Multipart Complete 的端到端行为。
- 本地真实 100 Mbps、VPS 1000 Mbps 的链路实际利用率。
- 用户生产 aiohttp 请求实际会否存在非标准 Transfer-Encoding、Content-Length 失配或特殊代理配置。

## 发布门槛
真实环境恢复后另行授权执行：测试 Bucket、预签名 URL 签名、分片 ETag、VPS 8443 TLS、持续并发高负载、缓存 1800 秒过期与故障注入。生产上线及 PR 合并需单独审核。
