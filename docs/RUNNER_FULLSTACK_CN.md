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
