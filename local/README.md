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


## Mac → VPS HTTP 版本 A/B 测速

默认使用 HTTP/2；只允许 `R2_RELAY_HTTP2=1`（HTTP/2）或 `0`（仅 HTTP/1.1）。修改环境变量后需要重启本地代理；该开关仅影响本地 RelayClient → VPS:8443，不改变 curl → 18585 或 VPS → R2 的 HTTP 版本，也不关闭 TLS 验证。

```bash
# 在启动代理的终端中，确保已配置 R2_RELAY_VPS_URL/TOKEN/CA_FILE
export R2_RELAY_HTTP2=0  # HTTP/1.1 对照组；默认 1
python -m r2relay_local --listen-host 127.0.0.1 --listen-port 18585
```

使用同一大小、不同内容（确保 MISS）的受控测试分片，分别在 HTTP/1.1 和 HTTP/2 下运行，至少各 3 次，记录 `relay_timing` 中的 `data_upload_ms`、`data_mbps`、`vps_http_version`。相同 MD5 第二次会变为 HIT，**无法**用于对照 Data 上传带宽。日志不输出 Token、签名 URL 或请求体。请使用独立测试任务，避免修改生产 Multipart 的有效分片。

`data_mbps` 反映上传请求体到收到 VPS Data 确认的有效速度，包括 HTTPS/TCP、VPS 缓存写入及处理开销，不是纯网络链路带宽。签名过期后的 R2 HTTP 403 也可以用于测这段耗时，但不代表实际 R2 上传成功。
