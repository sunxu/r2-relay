# 本地 Python HTTPS 代理（开发分支）

程序由 `python -m r2relay_local` 启动 mitmdump，并加载项目自己的 Python addon。默认监听 `127.0.0.1:18585`；只有 `*.r2.cloudflarestorage.com` 的 CONNECT 进入 TLS MITM，其他 HTTPS 原样隧道转发。下载默认关闭；下载启用时接管 R2 GET，并将响应改写到仅监听 loopback 的磁盘响应服务。mitmproxy allow-hosts 同时允许 R2 endpoint 和 `127.0.0.1:<port>`，否则本地 Loopback 响应无法送回原始客户端。

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

- Content-Length 已知且不超过配置的单分片上限（默认 128 MiB），且全局缓冲预算（默认 1024 MiB）允许时，完整读取分片并计算 MD5。
- 未知长度、超上限或超预算请求不进入缓存，改走普通流式 R2 直传。两个数值均由 `~/.config/r2-relay/config.toml` 管理，不再通过 `R2_RELAY_BUFFER_BUDGET` 环境变量设置。
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


## 上传缓存资源配置

默认配置随安装包提供：`r2relay_local/defaults.toml`。首次配置时，在仓库根目录执行：

```bash
mkdir -p ~/.config/r2-relay
cp -n local/r2relay_local/defaults.toml ~/.config/r2-relay/config.toml
chmod 600 ~/.config/r2-relay/config.toml
```

配置内容：

```toml
schema_version = 1

[upload]
max_part_mib = 128
global_buffer_mib = 1024

# 下载默认关闭；启用前必须根据 Mac 内存和临时磁盘容量填写预算。
[download]
enabled = false
backend = "remote"
max_temp_mib = 0
temp_dir = "~/.cache/r2-relay/downloads"

[download.remote]
max_concurrency = 16
global_max_concurrency = 32
small_response_memory_mib = 0

[download.local]
chunk_size_mib = 2
min_size_mib = 2
max_connections_per_get = 8
global_max_connections = 128

[download.predownload]
enabled = false
```

- `max_part_mib`：单个 UploadPart 进入缓存加速的最大请求体，默认 128 MiB。
- `global_buffer_mib`：所有正在缓存的 UploadPart 共享的 Content-Length 预留预算，默认 1024 MiB。
- 超过任一上传限制的请求仍由 mitmproxy 流式转发到原始 R2，不会被拒绝。
- `download.remote.max_concurrency = 16`：单文件同时取回的 Part 数上限。
- `download.remote.global_max_concurrency = 32`：所有下载任务共享的 Part 传输上限；等待 VPS READY 的 Part 不占用传输名额。
- `download.remote.small_response_memory_mib`：所有小响应共享的内存预算；启用下载时至少为 24 MiB，预算耗尽时排队。
- `download.max_temp_mib`：remote/local 共享的本地临时磁盘预算；启用 remote 时至少为 25 MiB，启用 local 时至少为 2 MiB。超预算的单个对象按原有逻辑处理。
- `download.temp_dir`：本地任务级临时文件目录。大响应只使用一个任务文件，按 Part 偏移写入，不保存第二套 Part 文件。
- 下载默认关闭。启用 remote 时须配置小响应内存预算及临时磁盘预算；启用 local 时只需共享临时磁盘预算，不需要 remote 小响应内存预算。
- 所有发往 Cloudflare R2 S3 endpoint 的 GET 在下载启用后由本地代理接管；原始 URL、Range/条件请求头和签名头交给 VPS。VPS 不可用或下载失败时返回明确 502，不静默直连 R2。
- Mac 只并发取回 VPS 已 READY 的 Part：单文件最多 16、所有任务合计最多 32。每片验证长度和 SHA-256 后直接写入一个任务级文件；所有 Part 完成且 VPS 任务 COMPLETE 后才返回原始响应。
- 大文件通过仅监听 `127.0.0.1` 的 Loopback 文件响应服务交给 mitmproxy，并开启 response streaming；不把完整大文件读入 Python 内存。
- 本地代理启动时使用 mitmproxy `connection_strategy=lazy`：先让 addon 决定是否接管，再建立上游连接，避免已接管的 R2 GET/UploadPart 预先连接原始 R2 主机。
- 旧版 `[download]` 下的 `max_concurrency`、`global_max_concurrency` 和 `small_response_memory_mib` 已删除；**不向后兼容**，必须迁移到 `[download.remote]`，否则启动时明确报 `unknown config key`。共享 `max_temp_mib`、`temp_dir` 仍保留在 `[download]`。
- 启动时日志输出实际加载的配置来源、上传预算、下载启用状态及后端、共享临时目录和预算、Predownload 状态与目录、当前后端的并发和内存设置；不打印 Token、VPS URL、R2 签名 URL 或请求头。
- 修改配置后重启本地 R2 Relay（18585）进程生效。
- 可用 `R2_RELAY_CONFIG=/absolute/path/config.toml` 指定另一份配置文件；该变量只指定配置文件路径，不承载数值配置。



## 真实 Cloudflare R2 GET 验收

Stage 2 的 Mock R2 全栈验收已覆盖 mitmproxy CONNECT、Caddy TLS/HTTP/2、VPS、16 MiB Part 组装和 Loopback 流式返回。真实 Cloudflare R2 验收必须在 VPS 下载服务部署并启用后，由操作者提供一条有效签名 GET 请求。

操作步骤见 [真实 R2 GET 验收手册](../docs/R2_DOWNLOAD_REAL_R2_ACCEPTANCE_CN.md)。手册先建立 25 MiB 对象的 R2 直连 Range 基线，再使用权限为 0600 的本地 JSON 请求描述文件运行 `local/poc/real_r2_download_acceptance.py`。脚本只输出状态码、长度、SHA-256、HTTP 版本及 VPS/本地清理结果，不输出签名 URL 或请求头；`PASS_REAL` 要求远端任务删除成功且本地临时资源释放。脚本直接验证 RelayClient → VPS → R2；随后手册还要求用 curl 经本地 18585 再完成一次真实 mitmproxy GET。该脚本不会在 CI 中运行，也不会部署或启用 VPS 下载服务。
