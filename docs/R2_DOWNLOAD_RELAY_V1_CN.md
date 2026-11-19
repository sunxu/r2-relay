# R2 Download Relay v1 技术需求与验收标准

- 需求编号：RLY-020～RLY-039
- 跟踪任务：[Issue #63](https://github.com/sunxu/r2-relay/issues/63)
- 状态：仓库负责人已确认设计基线；Stage 0 PoC 已通过；Stage 1 VPS 核心已合并（PR #66，45 项 VPS 测试及 7 个 Actions 流程通过）；Stage 2 Mac 客户端实现已通过 Mock R2 全栈验收（Draft PR #68；88 pytest、134 RC1 tests）；尚未部署 VPS。
- 原则：下载是独立能力，不改变现有 UploadPart PUT 上传行为。

## 1. 目标

本地代理接管所有发往 R2 的 GET，由 VPS 使用原始 URL 和请求头向 R2 发起一次 GET。VPS 将响应流直接写成独立 Part 文件；Mac 按 Part 编号并发取回，在本地内存或磁盘组装，完整验证后才向原始客户端返回响应。

只在 VPS → Mac 并发。R2 → VPS 只发起一次 GET，不做 R2 Range 并发。每个原始 GET 创建独立任务；v1 不做跨请求任务合并或对象去重。

## 2. 仓库负责人已确认的参数和配置

| 参数 | 值 | 配置位置 | 使用方 |
|---|---:|---|---|
| Part 大小 | 16 MiB | VPS /etc/r2-relay/config.toml | VPS |
| 分片阈值 | 严格大于 24 MiB | VPS /etc/r2-relay/config.toml | VPS |
| 单文件最大并发 | 16 | Mac ~/.config/r2-relay/config.toml | Mac |
| 全局最大并发 | 32 | Mac ~/.config/r2-relay/config.toml | Mac |

VPS 的 [download] 配置段包含 part_size_mib=16、split_threshold_mib=24、cache_ttl_seconds=600。Mac 的 [download] 配置段包含 max_concurrency=16、global_max_concurrency=32，并与现有上传配置共存。

VPS 使用 /etc/r2-relay/config.toml；默认 enabled=false。启用前必须根据 VPS 实际磁盘检查填写 max_cache_bytes、min_free_bytes、max_active_tasks、max_retained_tasks 和 max_waiters，未填写有效正值时 fail-closed。当前不臆造这些容量值，也不授权部署。Mac 配置路径为 ~/.config/r2-relay/config.toml。

四个核心参数必须通过 TOML 加载、严格校验，不得硬编码或要求通过环境变量维护。Token、VPS 地址、TLS CA 等凭据和连接信息继续按现有安全方式管理。VPS manifest 是每个任务 Part 布局的唯一权威来源。

## 3. 请求和响应处理

- **RLY-020 GET 接管：** 所有发往 Cloudflare R2 S3 endpoint 的 GET 均由本地代理接管并交由 VPS 执行，不限于 GET Object。v1 沿用现有上传目标识别边界：HTTPS、目标端口 443（URL 未显式指定端口时按 443）、主机名以 .r2.cloudflarestorage.com 结尾。非目标域名和非目标端口的 GET 按普通代理透传；未来若要支持 R2 自定义域名，另行增加显式 allowlist 配置和验收。
- **RLY-021 原始请求：** 转发原始 URL、查询参数和必要请求头，保留请求头多值语义；仅移除必须由 HTTP 客户端重新生成的 hop-by-hop 头。不得擅自修改 SigV4 签名相关 URL、查询参数、Host 或签名头。不设计签名 URL 权限缓存。
- **RLY-022 单次上游请求：** VPS 对每个原始 GET 只发起一次 R2 GET，不发起多个 Range 请求。
- **RLY-023 响应模式：** 实际响应体 <=24 MiB 时，Mac 使用内存；>24 MiB 时，VPS 生成 16 MiB Part，Mac 使用磁盘。204/304 等无响应体响应直接返回。3xx/4xx/5xx 保留真实状态、响应头和响应体，不得伪造成成功；不自动跟随重定向。
- **RLY-024 未知长度：** 缺少 Content-Length 时不得无限制缓冲内存。VPS 使用有界临时 spool；超过 24 MiB 时将已暂存前缀交给 Part writer 并继续生成 Part，最终不超过阈值则作为小响应返回。临时 spool 不得被读取接口当作 READY Part。Mac v1 在未知长度场景下等待 VPS COMPLETE，取得最终总长度后一次性预留磁盘预算，再取回 READY Part；因此未知长度响应暂不具备 Mac 侧边生成边取回能力。这是有意接受的 v1 限制，避免多个未知长度任务分别持有部分预算而互相等待。

## 4. VPS Part 生成与任务管理

- **RLY-025 流式分片：** 已知响应长度超过阈值时，VPS 从 R2 响应流直接生成 Part，不先保存完整对象再二次切割。每片 16 MiB，最后一片可更小。
- **RLY-026 原子发布：** 每个任务有独立目录和 metadata。写入文件使用 N.part.tmp；完整写入、关闭并校验长度后原子重命名为 N.part。读取接口永远不能读取临时文件。
- **RLY-027 Part 状态：** WRITING、READY、FAILED。只有 READY 可读取；Part READY 后立即提供，不必等待整个对象下载结束。
- **RLY-028 等待：** 本地请求未 READY 的 Part 时，VPS 等待 READY、任务失败/取消或超时；不得返回半个文件。等待请求不占全局数据传输名额，但等待连接数必须有上限和超时。
- **RLY-029 Manifest：** 至少包含 task_id、任务状态、R2 状态码、原始端到端响应头、总长度、Part 大小/数量/已发布 Part 长度、失败原因和过期时间。不得返回或记录 Token 等凭据。

## 5. Mac 取回、组装和返回

- **RLY-030 并发：** 单文件最多 16 个 Part 同时传输；所有任务合计最多 32 个。使用一个共享全局调度器；等待未 READY Part 不占数据传输名额；重试也必须经过调度器。
- **RLY-031 小响应：** <=24 MiB 使用内存，不创建大文件缓存。必须有独立的全局小响应内存预算；预算耗尽时排队，不得无限分配。预算默认值在内存压力测试后确定。
- **RLY-032 大响应：** >24 MiB 使用一个本地任务级临时文件。按 VPS manifest 的偏移和长度定位写入，不在 Mac 保存第二套 Part 文件、不二次合并。每片校验实际长度；建议 VPS 同时计算 SHA-256 并由本地验证。
- **RLY-033 完整后返回（已接受方案 A）：** 对 Content-Length 已知的大响应，Mac 可以在 VPS 下载期间提前取回 READY Part；未知长度响应按 RLY-024 等待 VPS COMPLETE 并预留最终磁盘预算后再取回。两种情况都必须等待所有 Part 收齐、长度/摘要校验通过且 VPS 任务 COMPLETE，才向原始客户端发送响应体。大文件从本地临时文件流式返回，不得整体读入 Python 内存。确认不再需要 VPS 文件后通知删除。

### 5.1 Mac 小响应内存峰值

小响应在 Mac 端使用 bytearray 累积并直接交给 Loopback 响应服务，不再转换成另一份完整 bytes 对象，避免 24 MiB 响应在转换瞬间出现两份完整 payload。预算按响应 payload 字节数计量；Python 对象和分配器的少量管理开销不属于 payload 预算。

### 5.2 Mac 下载超时（v1 暂定值）

当前 Mac RelayClient 的控制 API 请求超时为 15 秒，单个 Part/body 流读取超时为 90 秒（连接 15 秒、写入 30 秒），单个下载任务整体期限为 180 秒。这些值暂未进入 TOML。先通过真实 R2 和不同网络条件测量实际吞吐、慢速窗口与失败恢复，再决定是否增加配置项；v1 不采用无限等待，也不在缺少实测数据时直接放宽期限。

## 6. HTTP 语义

- **RLY-034 响应头：** 保留 VPS 从 R2 获得的真实状态码和端到端响应头；过滤 Connection、Transfer-Encoding、Keep-Alive、Upgrade 等 hop-by-hop 头。对于有响应体的响应，Content-Length 必须与实际返回字节数一致；304 等无响应体状态按 HTTP 语义保留 R2 提供的合法元数据头，不得人为生成响应体。重复端到端响应头应保留多值语义。Content-Encoding 必须与交付的原始字节一致，避免自动解压后仍保留旧编码/长度。
- **RLY-035 原始 Range：** 原始客户端的 Range、If-Range、If-None-Match 等条件请求原样交给 VPS/R2。R2 返回 206 时，对该 206 响应体分 Part；本地内部仍按 Part ID 取回，不使用 HTTP Range。最终保留真实状态和 Content-Range、ETag。多 Range 响应作为原始 multipart/byteranges 字节流处理，不自行重建边界。

## 7. 失败、清理和资源保护

- **RLY-036 失败安全：** 上游失败后任务 FAILED，未完成 Part 不得发布。已发布部分 Part 后上游失败，不得重新 GET 后把不同对象版本混入旧任务。Part 传输中断可重新请求 READY Part，仍受并发限制。创建使用 request_id/idempotency key；删除幂等。v1 不静默绕过 VPS 直连 R2；VPS 不可用返回明确代理错误。
- **RLY-037 删除/TTL：** 本地完整接收并验证后通知 VPS 删除；默认 TTL 600 秒兜底。TTL 为空闲过期，从上游完成后开始计时，活动读取刷新；正在写入或有活动读取者时不得删除。失败/取消尽快清理；正常完成不等待 TTL。
- **RLY-038 资源保护：** VPS 必须限制缓存磁盘预算、最低剩余空间、活动任务、Part 等待者和等待时间；Mac 必须限制小响应总内存和临时目录空间。具体容量值根据 VPS 磁盘检查和 Mac 内存测试确定，不臆造。
- **RLY-039 隔离：** 下载有独立 API、任务状态、缓存目录、调度器和指标；不复用上传 MD5 缓存键、上传执行记录或上传 TTL。现有上传配置和行为不变。

## 8. VPS 下载 API（Stage 1）

所有端点使用现有 Relay Bearer 鉴权，并由 VPS HTTPS 入口提供服务。请求创建后由 VPS 直接向 R2 发起一次 GET；不会自动跟随重定向，也不会向 R2 发起并发 Range 请求。

| 方法与路径 | 用途 | 行为 |
|---|---|---|
| POST /v1/downloads | 创建下载任务 | JSON 提交 schema_version、request_id、url、headers；返回 202 和 task_id。相同 request_id 与相同请求内容重试时返回原任务；相同 request_id 对应不同请求返回 409。 |
| GET /v1/downloads/{task_id} | 查询 manifest | 返回状态、R2 状态码、端到端响应头、实际/声明长度、Part 列表、每片状态与 SHA-256、错误和过期时间。 |
| GET /v1/downloads/{task_id}/parts/{part_number} | 获取 Part | READY 立即流式返回；尚未 READY 时等待，等待有总量上限和超时；只返回完整发布的 Part。 |
| GET /v1/downloads/{task_id}/body | 获取小响应 | 等待任务 COMPLETE 后流式返回，仅适用于 <=24 MiB 的 small 模式。 |
| DELETE /v1/downloads/{task_id} | 删除/取消任务 | 幂等；不会在上游 writer 或活动 Part reader 使用文件时删除。 |

Part 写入使用 N.part.tmp；完整写入、fsync 并计算 SHA-256 后原子改名为 N.part，manifest 才把状态改为 READY。未知 Content-Length 的响应先写入不超过阈值的临时 spool；一旦超过 24 MiB，已暂存前缀转入 Part writer，随后继续消费同一条 R2 响应流。

任务状态包括 QUEUED、RECEIVING、COMPLETE、FAILED、CANCELLED；Part 状态包括 WRITING、READY、FAILED。上游失败时清除未完成文件和已发布的局部 Part，不允许把局部内容作为成功对象交付。

下载任务 manifest 与幂等索引目前保存在 VPS 进程内存。VPS 重启后旧 task_id 不恢复；启动时清理下载目录中的孤儿文件，客户端必须重新创建任务。该行为避免把重启前的未完成对象误判为可用数据。

所有 VPS API 沿用现有 Relay Bearer 鉴权和 HTTPS。签名 URL 权限缓存不属于 v1。

## 9. Gate 0：mitmproxy 磁盘响应 PoC（实现前置门槛）

当前 addon 使用完整 bytes 构造响应，不适合 GiB 文件。已完成的 Gate 0 PoC 验证：从 Mac 本地磁盘经 Loopback 文件服务向原始客户端发送 1 GiB；在现有 regular HTTP proxy + CONNECT 模式下使用 HTTP/1.1；验证 Content-Length、完整字节数、客户端中途断开传播，以及 mitmdump RSS 增量。当前 CI 实测 RSS 增量为 0.3 MiB，低于 128 MiB 上限。

现有 regular CONNECT 路径的客户端侧协议验收限定为 HTTP/1.1；Mac RelayClient → VPS 的 HTTP/2 是独立链路，必须在后续下载全栈测试中单独验证，不能把客户端 CONNECT 的 HTTP/1.1 结果解释为 HTTP/2 已通过。Loopback 文件响应服务 PoC 已通过 Gate 0；不得据此认定完整下载中继已经实现。Gate 0 不部署 VPS、不改变生产服务。

## 10. 验收矩阵

| ID | 场景 | 通过条件 |
|---|---|---|
| DL-T01 | TOML | 四个参数均配置化；非法值 fail-closed；值为 16/24/16/32 |
| DL-T02 | 阈值 | 24 MiB 内存；24 MiB + 1 Byte 分片 |
| DL-T03 | 分片 | 25 MiB = 16+9 MiB；100 MiB = 7 Part |
| DL-T04 | 原子发布 | READY 后才可读；临时文件不可读 |
| DL-T05 | 等待 | 未 READY 等待；READY 返回完整片；失败/取消/超时明确结束 |
| DL-T06 | 单文件并发 | 同一任务最多 16 个传输中的 Part |
| DL-T07 | 全局并发 | 所有任务合计最多 32 个传输中的 Part |
| DL-T08 | 内存 | 小响应使用内存且受总预算限制 |
| DL-T09 | 大文件 | 1 GiB 不整体进内存，按偏移写入一个本地临时文件 |
| DL-T10 | 边下边取 | Content-Length 已知的大响应中，VPS 未完成对象时本地可取 READY Part；未知长度按 RLY-024 等待 COMPLETE |
| DL-T11 | 完整返回 | 所有 Part 和任务 COMPLETE 前客户端未收到响应体 |
| DL-T12 | Range | 原始 Range 正确返回 206/Content-Range；内部使用 Part ID |
| DL-T13 | 状态码 | 204/304/重定向/403/404/5xx 语义正确，304 无响应体及其元数据头处理符合 HTTP 语义 |
| DL-T14 | 响应头 | 原始字节、Content-Encoding、Length、ETag 一致；逐跳头过滤 |
| DL-T15 | 未知长度 | 不无限缓冲；VPS spool/模式切换和最终长度正确；Mac 等待 COMPLETE 后预留最终预算再取回（v1 已知限制） |
| DL-T16 | 上游失败 | 不发布半成品、不混合重试对象 |
| DL-T17 | Part 重试 | 只重取完整 READY Part，仍受 16/32 限制 |
| DL-T18 | TTL | 正常完成主动删除；600 秒空闲过期；活动读写不被删除 |
| DL-T19 | 幂等 | 创建重试不重复执行；删除重试安全 |
| DL-T20 | 资源 | VPS 磁盘/任务/等待者和 Mac 内存/临时空间均有界 |
| DL-T21 | mitmproxy PoC | 1 GiB 磁盘响应在 regular CONNECT HTTP/1.1 下通过；客户端断开可传递到文件服务；mitmdump RSS 增量低于 128 MiB。RelayClient → VPS HTTP/2 在后续下载全栈测试中单独验收 |
| DL-T22 | 上传回归 | 既有 UploadPart 测试、默认配置和行为不变 |
| DL-T23 | 全栈模拟 | aiohttp → mitmproxy → VPS HTTPS → Mock R2 覆盖小/大响应、失败和清理；Mac RelayClient → VPS HTTP/2 单独验证 |
| DL-T24 | 目标范围 | R2 HTTPS 443 域名命中接管；非目标域名/端口 GET 正常透传 |
| DL-T25 | 真实 R2 | GET/Range GET、响应头、签名请求和故障行为单独记录 PASS_REAL；Mock 不替代真实验收 |

## 11. 实施阶段

1. Stage 0：mitmproxy 磁盘响应 PoC，已通过 DL-T21（对应仓库总验收 T31）。
2. Stage 1：VPS 任务状态、单 GET、流式 Part、manifest、等待式 Part API、TTL 和磁盘保护（已合并 PR #66；VPS 45 项测试及 7 个 Actions 流程通过；未部署）。
3. Stage 2：Mac GET 接管、TOML、16/32 调度、小响应内存和大响应磁盘组装（已实现并通过隔离 Mock R2 全栈验收，见 Draft PR #68；真实 Cloudflare R2 与生产 VPS 尚待验收）。
4. Stage 3：HTTP 语义、失败恢复、删除、Mock R2 全栈和上传回归。
5. Stage 4：真实 R2 验收和性能测量。

每阶段通过独立 PR 和 CI。生产 VPS 部署必须另行取得仓库负责人明确授权；本需求不授权部署。

## 12. v1 不做

- 不做 R2 → VPS Range 并发。
- 不让 Mac 向 VPS 发 HTTP Range；内部按 Part ID 请求。
- 不保存 VPS 完整对象后再二次切割。
- 不在 Mac 保存第二套 Part 文件后再合并。
- 不做跨 GET 缓存复用/任务去重。
- 不做签名 URL 权限缓存。
- 不改变现有上传 MD5 缓存、1800 秒 TTL 和上传配置。
- 不向原始客户端边下载边发送；完整取回和验证后再返回。
