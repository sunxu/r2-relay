# 本地 Python HTTPS 代理（开发分支）

程序由 `python -m r2relay_local` 启动 mitmdump，并加载项目自己的 Python addon。默认监听 `127.0.0.1:18585`；只有 `*.r2.cloudflarestorage.com` 的 CONNECT 进入 TLS MITM，其他 HTTPS 原样隧道转发。目标域名中的非 UploadPart 请求不会进入缓存逻辑。

## 运行

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e './local[test]'
export R2_RELAY_VPS_URL=https://YOUR_VPS_HOST:8443
export R2_RELAY_TOKEN='YOUR_SECRET'
# 如果 VPS 使用私有测试 CA，请指定该 CA；httpx 仍验证证书：
# export R2_RELAY_VPS_CA_FILE=/path/to/vps-ca.pem
python -m r2relay_local
```

代理首次启动后，可从 `~/.mitmproxy/mitmproxy-ca-cert.pem` 取得代理 CA 公共证书。调用方自行在其原 CA 信任库之上追加代理 CA，并使用 aiohttp `proxy="http://127.0.0.1:18585"`。禁止使用 `ssl=False` 代替 CA 验证；不得提交 mitmproxy CA 私钥、token 或真实签名 URL。

## 行为

- Content-Length 已知且不超过 32 MiB，且全局缓冲预算（默认 512 MiB）允许时，完整读取分片并计算 MD5。
- 未知长度、超上限或超预算请求不进入缓存，改走普通流式 R2 直传。
- VPS TCP 建连失败时直传；R2 真实业务 4xx/5xx 透传；上传任务已提交后通信断开时返回状态未知错误，避免立即竞争性直传。
- VPS 磁盘明确以 HTTP 507 拒绝缓存写入（还未启动 R2 Worker）时，可安全回退直传。
- 当前采用声明长度预算预留，真实高负载与恶意 HTTP 长度仍需外网/真实客户端验证。

**仍未生产验收：** 实际 VPS 8443、真实 R2 签名/ETag 与 100 Mbps 链路测试。
