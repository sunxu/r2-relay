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


## 隔离真实 R2 协议验收（不启用生产）

**复用已有脚本** \`local/poc/real_r2_download_acceptance.py\` 和原来的私有 0600 JSON 请求样本；不建立第二套签名请求验收工具。该脚本现在按 TOML \`download.backend\` 自动选择 Remote / Local；Remote 的现有逻辑、验收输出不变。Local 会读取完整原始 GET 响应流、核对状态/长度/指定响应头及 SHA-256，并检查临时文件与预算清理。报告只包含布尔检查与未签名的统计数字，不显示 URL、Token 或认证头。

隔离验收建议使用**独立配置**，例如在 Mac 的私有目录建立 \`config.local-acceptance.toml\`，设置 \`[download] enabled=true, backend="local", max_temp_mib=64\`，并把 \`temp_dir\` 设到与现有生产缓存不同的私有目录。不要改写生产 \`~/.config/r2-relay/config.toml\`。私有 JSON 样本继续设置 \`R2_RELAY_REAL_GET_SPEC\`，必须为 0600，含原 URL、请求头、参考 HTTP 状态、大小、完整 SHA-256 和至少一组预期响应头。

在仓库已有 Python 环境（\`local\` 可导入）中执行：

\`\`\`sh
R2_RELAY_CONFIG="$PRIVATE_LOCAL_ACCEPTANCE_TOML" \
R2_RELAY_REAL_GET_SPEC="$PRIVATE_MODE_0600_JSON" \
python local/poc/real_r2_download_acceptance.py
\`\`\`

只有 \`PASS_REAL_LOCAL\` 且状态、长度、指定头、SHA-256、清理均通过，才算真实 R2 协议正确性通过。输出 \`split_gets\` / \`child_range_requests\` 用于辨别是否真的触发了并发子 Range：两者均为 0 只证明单连接回退正确，**不能**证明并发 Range 的真实协议正确性。无需设置 VPS URL、Token 或 CA，也不需要速度测速。

已验证协议后，仍使用原有 \`local/poc/real_r2_mitmproxy_acceptance.py\` 在单独的 127.0.0.1:18586 mitmdump 实例上核对 TLS 代理和完整响应。该步骤要求独立临时配置、单独证书与私有样本；不得占用生产 18585，不能将这一步当作 iota-tools 的慢速接管已验证。最终 IOTA 兼容性需在独立 replacement_proxy 及可复现的慢速触发环境下验证，且必须继续保留 IOTA 前缀 SHA-256 责任边界。

**真实验收仍待在拥有私有请求样本的 Mac 运行。** 公开 GitHub Actions 的 Mock 测试结果不代表 R2 真机通过，严禁向 GitHub Secrets、Issue、PR 日志或本文件复制签名 URL / Token。
