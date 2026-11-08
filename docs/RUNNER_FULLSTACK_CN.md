# GitHub Runner 真进程全栈验收（不需要 VPS/R2）

新增 `.github/workflows/runner-fullstack.yml`：在一个 GitHub-hosted Ubuntu Runner 上运行五个真实组件：

1. Python aiohttp HTTPS 客户端
2. mitmdump TLS MITM 本地代理
3. Caddy Docker 容器监听 HTTPS `localhost:8443`
4. VPS ASGI/Uvicorn 监听 `127.0.0.1:18085` 以外的临时回环端口
5. 独立 Uvicorn Mock R2 HTTP Server，通过 VPS Worker 的 **测试专用传输适配器**走真实 HTTP/TCP

只有 `tests/runner_fullstack/mock_vps.py` 使用测试专用网络适配器，生产 `r2relay_vps.app:create_app` 完全没有任何可配置到任意内网的 R2 URL 重写接口。Mock R2 用独立 HMAC 见证码检查 UploadId/PartNumber/内容 MD5，**这不是 AWS SigV4，也不是 Cloudflare R2 兼容性证明**。

### 验收场景

- 首次 MISS 与不同 UploadId 第二次 HIT：VPS 只存一份文件，每次都通过 Worker 发实际模拟 R2 PUT
- 第一次 R2 返回 502，下一次成功；429 同理
- 403 不重试，原始状态由真实 aiohttp 收到
- 真实 Caddy HTTPS 8443 TLS、证书验证、mitmproxy CONNECT/TLS 与代理信任链
- JSON 安全报告包含运行 Commit、模拟请求数量、状态、真实 VPS/R2/SigV4/带宽全为 `NOT_TESTED`

### 运行

CI 自动在 PR 变更对应目录时触发，或 Actions → Runner full-stack acceptance → Run workflow。需要 GitHub Runner 自带的 Docker Engine，自动拉取 Caddy 2。**不可把这个脚本放到真实 VPS 上运行**；测试证书、明文 mock token、HMAC key 都是固定测试值。

本工作流不会调用 Cloudflare R2 或连接真实 VPS，没有外部云凭据。Caddy 镜像下载和 Python 依赖下载是构建依赖网络请求，不是 R2 上传测试。

## 验证证据（2026-11-08）

- GitHub Actions [Runner Full Stack run #42442988163](https://github.com/sunxu/r2-relay/actions/runs/42442988163)：**SUCCESS**
- 测试 Commit：`4d121ae77bcfaa9ec459760e9446ab0bc7a30912`
- 实际通过 Docker Caddy HTTPS `:8443` → 真实 TCP VPS/Uvicorn → 独立 HTTP Mock R2，客户端是 aiohttp 经 mitmdump。
- 已验证 Caddy HTTP/2、Bearer 指标、首次 MISS、后续 HIT、502/429 各额外重试一次、403 不重试，模拟 ETag 与正文 MD5。
- 真实重启 VPS Uvicorn 后在同一磁盘目录恢复缓存，再次命中且不用第二次传输本地分片正文。
- JSON 机器报告作为 Actions artifact `runner-fullstack-safe-report` 上传；含 Commit、真实协议组件、模拟请求次数与 `NOT_TESTED` 的 Cloudflare R2/SigV4/公网/带宽边界。

这些结论只针对 GitHub Runner 内部服务，不能用作真实 VPS/R2 的生产验收。测试用独立 HMAC 见证码不等价于 AWS SigV4。

## 主分支状态

RC1 已由 [PR #15](https://github.com/sunxu/r2-relay/pull/15) 合并到 `main`（Merge Commit `18e068a923238bccb83b2e8a77fe25ac4f7abb32`）。测试环境不等于生产部署；真实协议和性能分别由 Issues [#16](https://github.com/sunxu/r2-relay/issues/16)、[#17](https://github.com/sunxu/r2-relay/issues/17) 跟踪。
