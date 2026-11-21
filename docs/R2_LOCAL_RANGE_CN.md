# R2 Relay：Mac 本地 HTTPX Range 下载（v1）

状态：开发分支功能验证阶段；默认关闭，未获生产切换授权。需求基线：2026-11-21 v1.0。

## 边界

IOTA 原始 GET 路由、iota-tools 慢速探测、替代响应状态/响应头对照及已交付前缀 SHA-256 验证全部保留。只有当现有慢速接管调用 replacement_proxy，代理才执行所选下载后端。

- remote：原有 VPS DownloadCoordinator；保持默认且不修改其算法。
- local：使用 HTTPX 直接连接允许的 HTTPS Cloudflare R2 主机。单 GET 最多 8 条连接，全 Local 最多 128 条 R2 GET 连接。
- 上传：配置 Token 时仍使用原有 VPS 上传中继。无 Token 且启用 local 下载时，上传请求保持原始直连路径，不伪造 VPS UploadPart 成功。
- 原有 DiskResponseServer 将初始 GET 的状态、端到端响应头及完整字节流交付给 iota-tools；不会交付只有尾部的替代响应。

## 配置

原位置：\`~/.config/r2-relay/config.toml\`。已有 Remote 部署无需更新。

\`\`\`toml
[download]
enabled = true
backend = "local"
max_temp_mib = 512
# Remote 配置继续保留但 Local 不使用：
max_concurrency = 16
global_max_concurrency = 32

[download.local]
chunk_size_mib = 2
min_size_mib = 2
max_connections_per_get = 8
global_max_connections = 128
\`\`\`

\`backend\` 只接受 remote 或 local；未指定时 remote。Local 需要设置非零的 \`max_temp_mib\`，但无需设置 Remote 专用的小响应内存预算，无需配置 VPS Endpoint、Token 或 CA。若继续要求 VPS UploadPart 缓存，仍需单独提供有效 VPS Token 等设置。当前仅在单独测试配置中使用 local，不改动正在运行的实例。

Local 需要 2 MiB 固定分片。每个 GET 响应长度小于等于 2 MiB 或无法证明 Range 并发安全时，保留最初单连接的原始响应流，不额外发起 HEAD 或重复 GET。未知长度默认单流。

## 可拆分性与版本保护

只有以下条件全部成立，才可能拆分：

1. 初始 R2 GET 返回 200 或 206、明确的 Content-Length、强 ETag 和 identity（或无）Content-Encoding。
2. 206 必须具有和原始单一 \`Range: bytes=start-end\`／\`bytes=start-\` 一致的 Content-Range；200 无 Range 时必须声明 \`Accept-Ranges: bytes\`。
3. 请求不得包含条件请求、多 Range、含糊的认证签名约束，且签名不得绑定 Range 或新增的 If-Match。
4. 响应长度大于 2 MiB 且临时磁盘有足够可预留预算与剩余空间。

首个 GET 已建立的连接读取响应头和首个 2 MiB，后续连接只读取原响应范围内的其它 2 MiB 子 Range。对子请求附加强 ETag 的 If-Match，逐条校验 206、Content-Range、Content-Length、Content-Encoding、ETag 和 Last-Modified，禁止混用不同版本。TransportError 最多重试两次；响应版本或范围不匹配立即终止。

如果对象版本条件无法证明，使用原单连接，不放宽验收规则。HTTPX 不信任环境代理设置，验证 TLS，不跟随重定向；保留原始请求 URL 和必要端到端请求头。禁止将签名 URL、认证头、原始异常字符串写入日志。

## 交付与资源

网络按 64 KiB 原始字节流读取，使用已有受控异步 pwrite 工具写入单任务临时文件，按原始响应体相对偏移定位。不执行多分片文件合并，READY 仅在完整分片字节数、HTTP 元数据及磁盘写入验证后发布。返回最初 GET 的响应状态和响应头；完整 READY 分片按顺序交付。Loopback 已有断连监听与异常结束机制负责使正文错误表现为截断，不伪造成功 EOF。

资源预算由 Local 独立全局 Semaphore（128）与每 GET Semaphore（8）控制，包含初始连接、子 Range 与重试。仅调度有限的在途分片，未消费分片窗口按每 GET 连接上限约束，慢客户端背压不会无限扩张预取量。临时文件位于 \`~/.cache/r2-relay/downloads/local/\`，权限 0600，目录 0700；采用独立进程锁，启动时只清理该目录中的历史遗留文件。取消时等待已有后台写入结束后关闭 FD、删除文件并释放磁盘预算；不影响 Remote 缓存目录。

## 验证与限制

- 自动化：\`cd local && python -m pytest -q\`，覆盖 Range、配置默认值、单连接、原始 200/206、错误状态、版本不一致、并发额度、取消与资源清理，且保留现有 Remote/Upload 回归。
- 可接入独立的 iota-tools replacement_proxy 做隔离协议验证；不改变生产代理。
- 不设置速度收益验收门槛，不声称已测出收益。
- 上线前仍需在隔离环境确认真实 R2 签名头、Accept-Ranges/Content-Range/ETag 语义、内容 SHA-256、原始 200/206/403/404/416/5xx 及异常截断行为，再单独申请合并、部署和启用授权。

已知限制：没有强 ETag、未知 Content-Length、压缩编码、条件 GET、多 Range、签名包含 Range/If-Match/If-Range 或临时磁盘不足时，不拆分。也不支持跨 GET 共享缓存、动态分片或自动回退到 VPS。
