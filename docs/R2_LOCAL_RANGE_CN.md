# R2 Relay：Mac 本地 HTTPX Range 下载（v1）

状态：**直接 R2 下载协议验收完成（14/14 PASS）**；PR #82 仍为 Draft，未合并、未部署、未启用生产。需求基线：2026-11-21 v1.0。IOTA 集成验收按用户决定取消。

## 边界

IOTA 原始 GET 路由、iota-tools 慢速探测、替代响应状态/响应头对照及已交付前缀 SHA-256 验证全部保留。只有当现有慢速接管调用 replacement_proxy，代理才执行所选下载后端。

- remote：原有 VPS DownloadCoordinator；保持默认且不修改其算法。
- local：使用 HTTPX 直接连接允许的 HTTPS Cloudflare R2 主机。单 GET 默认 8 条连接，全 Local 默认 128 条 R2 GET 连接；可以通过配置提高额度，仍受实际 Semaphore 限制。
- 上传：配置 Token 时仍使用原有 VPS 上传中继。无 Token 且启用 local 下载时，上传请求保持原始直连路径，不伪造 VPS UploadPart 成功。
- 原有 DiskResponseServer 将初始 GET 的状态、端到端响应头及完整字节流交付给 iota-tools；不会交付只有尾部的替代响应。

## 配置

配置文件：\`~/.config/r2-relay/config.toml\`。Remote 专用参数已移入 \`[download.remote]\`。旧版顶层键不再兼容，升级前必须迁移。

\`\`\`toml
[download]
enabled = true
backend = "local"
max_temp_mib = 512
[download.remote]
# Local 后端不使用这组参数。
max_concurrency = 16
global_max_concurrency = 32
small_response_memory_mib = 0

[download.local]
chunk_size_mib = 2
min_size_mib = 2
max_connections_per_get = 8
global_max_connections = 128
\`\`\`

\`backend\` 只接受 remote 或 local；未指定时 remote。Local 需要设置非零的 \`max_temp_mib\`，但无需设置 Remote 专用的小响应内存预算，无需配置 VPS Endpoint、Token 或 CA。若继续要求 VPS UploadPart 缓存，仍需单独提供有效 VPS Token 等设置。当前仅在单独测试配置中使用 local，不改动正在运行的实例。

Local 默认使用 2 MiB 分片；chunk_size_mib 和 min_size_mib 分别接受正整数 MiB，并可独立配置。连接并发及内存、磁盘预算允许正整数配置超过原有固定上限，0/负值、非整数及单 GET 大于全局连接额度仍被拒绝。每个 GET 响应长度小于等于配置的 chunk_size_mib 或无法证明 Range 并发安全时，保留最初单连接的原始响应流，不额外发起 HEAD 或重复 GET。未知长度默认单流。

## 可拆分性与版本保护

只有以下条件全部成立，才可能拆分：

1. 初始 R2 GET 返回 200 或 206、明确的 Content-Length、强 ETag 和 identity（或无）Content-Encoding。
2. 206 必须具有和原始单一 \`Range: bytes=start-end\`／\`bytes=start-\` 一致的 Content-Range；200 无 Range 时必须声明 \`Accept-Ranges: bytes\`。
3. 请求不得包含条件请求、多 Range、含糊的认证签名约束，且签名不得绑定 Range 或新增的 If-Match。
4. 响应长度大于配置的 chunk_size_mib、且不小于 min_size_mib，并且临时磁盘有足够可预留预算与剩余空间。

首个 GET 已建立的连接读取响应头和首个配置大小的分片，后续连接只读取原响应范围内的其它同尺寸子 Range（尾块可更小）。对子请求附加强 ETag 的 If-Match，逐条校验 206、Content-Range、Content-Length、Content-Encoding、ETag 和 Last-Modified，禁止混用不同版本。TransportError 最多重试两次；响应版本或范围不匹配立即终止。

如果对象版本条件无法证明，使用原单连接，不放宽验收规则。HTTPX 不信任环境代理设置，验证 TLS，不跟随重定向；保留原始请求 URL 和必要端到端请求头。禁止将签名 URL、认证头、原始异常字符串写入日志。

## 交付与资源

网络按 64 KiB 原始字节流读取，使用已有受控异步 pwrite 工具写入单任务临时文件，按原始响应体相对偏移定位。不执行多分片文件合并，READY 仅在完整分片字节数、HTTP 元数据及磁盘写入验证后发布。返回最初 GET 的响应状态和响应头；完整 READY 分片按顺序交付。Loopback 已有断连监听与异常结束机制负责使正文错误表现为截断，不伪造成功 EOF。

资源预算由 Local 独立全局 Semaphore（默认 128）与每 GET Semaphore（默认 8）控制，包含初始连接、子 Range 与重试。仅调度有限的在途分片，未消费分片窗口按每 GET 连接上限约束，慢客户端背压不会无限扩张预取量。临时文件位于 \`~/.cache/r2-relay/downloads/local/\`，权限 0600，目录 0700；采用独立进程锁，启动时只清理该目录中的历史遗留文件。取消时等待已有后台写入结束后关闭 FD、删除文件并释放磁盘预算；不影响 Remote 缓存目录。

## 验证与限制

- 自动化：\`cd local && python -m pytest -q\`，覆盖 Range、配置默认值、单连接、原始 200/206、错误状态、版本不一致、并发额度、取消与资源清理，且保留现有 Remote/Upload 回归。
- 不执行 IOTA 集成验收；也不声称 IOTA 慢速接管或已交付前缀校验经过端到端验证。
- 不设置速度收益验收门槛，不声称已测出收益。
- 14/14 条 Mac 私有真实签名请求已完成原始直连与 Local 对照（HTTP 200/206、状态、关键响应头、长度、完整 SHA-256、资源清理）。403/404/416/5xx、异常截断和版本漂移是 Mock/离线 CI 覆盖，**不能冒充真实 R2 错误场景验收**；合并、部署、生产切换另需授权。

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

既有独立 18586 mitmproxy 验收工具可以留作以后代理层排障，但**不作为本次直接下载协议验收的剩余必做项**。按用户决定不执行 IOTA 集成验收，其相关端到端行为不属于此次 PASS 范围；生产 18585 保持不动。

**真实 R2 验收已由用户在 Mac 私有工作区完成，并提供不含敏感凭据的 JSON 回执。** 公开 GitHub Actions 仅提供 Mock/离线证据；原始签名 URL、Token、响应正文未上传 GitHub。


## 独立 Mac 测试工作区（PR #82）

可重复使用仓库内 local/poc/local_range_workspace.sh 初始化，不修改当前 ~/workspace/r2-relay、~/workspace/r2-relay-production 或 ~/.config/r2-relay/config.toml。该脚本仅允许在独立克隆、指定功能分支上执行；已有测试 TOML 不会被覆盖。

首次在 **Mac** 执行：

~~~bash
mkdir -p "$HOME/workspace"
git clone --single-branch --branch feat/local-httpx-range-backend \
  https://github.com/sunxu/r2-relay.git \
  "$HOME/workspace/r2-relay-local-range-test"
bash "$HOME/workspace/r2-relay-local-range-test/local/poc/local_range_workspace.sh" setup
~~~

setup 创建独立 .venv（Python >=3.11）、安装既有 Local/VPS 测试依赖，写入只供测试使用的 Local TOML，执行现有 config/addon/local_range/real-R2-spec **Mock** 测试；不发真实 R2 GET，不启动 18585/18586，不操作生产进程或 VPS。

| 类型 | 工作区 |
| --- | --- |
| 独立 Git 代码 | ~/workspace/r2-relay-local-range-test/ |
| 独立 Python | 该代码目录下的 .venv/ |
| 只读现有签名请求样本 | 执行者提供原有 mode-0600 绝对路径，不复制 |
| 私有配置 (0600) | ~/.local/share/r2-relay/private/local-range-acceptance/config.toml |
| 独立缓存 (0700) | ~/.cache/r2-relay/local-range-acceptance/downloads/ |
| Local 内部分片文件 | 独立缓存下的 local/，退出后清理 |

配置为 download.enabled=true、backend="local"、max_temp_mib=64，固定 2 MiB 分片、单 GET 默认 8 连接、Local 全局默认 128 连接，Remote 继续默认且生产无变化。

仅重新运行离线测试：

~~~bash
bash "$HOME/workspace/r2-relay-local-range-test/local/poc/local_range_workspace.sh" smoke
~~~

**只有收到明确的真实 R2 测试授权、拥有有效的已有私有 0600 请求样本后**，执行：

~~~bash
bash "$HOME/workspace/r2-relay-local-range-test/local/poc/local_range_workspace.sh" \
  real "/absolute/path/to/existing-private-0600.json"
~~~

脚本仅说明工作区初始设施，仍可复用既有 real_r2_download_acceptance.py 的单请求测试；本次实际的 14/14 验收由用户在 Mac 私有目录中使用未脱敏请求、以原始直连和 Local 后端双路比对完成。初始化不会启动 18586 mitmdump 或 IOTA。


## 真实日志提取的离线协议包（只读 Mock）

可使用 r2_local_range_offline_test_data.zip，**不需要解压、不需要添加到 Git**。独立工作区升级到本功能分支最新提交后，从 Mac 上运行：

~~~bash
git -C "$HOME/workspace/r2-relay-local-range-test" pull --ff-only
bash "$HOME/workspace/r2-relay-local-range-test/local/poc/local_range_workspace.sh" \
  offline "/absolute/path/to/r2_local_range_offline_test_data.zip"
~~~

offline 只读取经白名单核对的 5 个 ZIP 成员，包括 25,301,339 字节纯合成对象和 SHA-256 期望清单。利用现有 test_local_range.py 的 HTTPX MockTransport、LocalRangeDownloader 和原始 HTTP 响应头逻辑，完整验证 10 种场景的状态、Content-Range、流式正文长度/哈希、首次请求及子 Range 的数量/If-Match、清理；绝不访问真实 R2，绝不调用 VPS。不会将 ZIP 文件内容放入 CI、Git 或日志。其他既有 Mock 故障注入测试继续覆盖对象版本变化/异常与取消，不重复开发。

私人包 r2_local_range_private_replay_candidates.zip 含原始签名 URL，不能上传 GitHub，不能传给只读 offline Mock，也不能直接交给既有单请求 real 格式。实际本地验收已使用独立私有脚本读取原始请求、直接 R2 基线与 Local 响应比对，无需把缺失的 expected_sha256 硬填成猜测值。原始数据和独立测试脚本留在 Mac 本地，离线 Mock 不能代替真实 R2 回执。


## 最终验收记录与范围（2026-11-22 汇总）

- **直接 R2 协议：14/14 PASS**，8 条原始请求和 6 条派生边界请求，失败数 0。首个原始样本独立通过，其余 7+6 之后通过。
- **拆分：9 个真实场景实际触发，合计 47 次子 Range 请求**，其他 5 个为单连接。全部对照状态、关键响应头、响应长度、全量 SHA-256、临时资源清理。
- **边界：** 1 MiB 和恰好 2 MiB 不拆分；2 MiB+1 字节、4 MiB、8 MiB、约 12 MiB 固定 Range、25,301,339 字节无 Range 的已知长度响应成功拆分；12 字节固定 Range、235 字节开放 Range 不拆分。
- **归类：** `derived_no_range_unknown_size` 实际得到已知 Content-Length（25,301,339 字节），不应计作真实未知长度回退。历史 `slow_takeover_failed` 样本的直接 GET 对照通过，不证明历史 IOTA 接管问题已经端到端修复。
- **CI：** 开发分支 HEAD `0374e2f56bec38a91d0a065c5d0d9a48bca2c1d7` 6/6 SUCCESS；168 passed、1 skipped（可选离线 ZIP 没有提供给 CI）、12 subtests passed。真实 R2 请求与本地原始测试脚本均不提交 Git。
- **明确不执行：** IOTA 集成验收与下载性能测速；本次也不新增 VPS 开发、上传改造、重复测试框架。
- **后续另需授权：** 合并 PR #82、Mac 部署、生产切换。当前保持 Draft 和默认 Remote。
