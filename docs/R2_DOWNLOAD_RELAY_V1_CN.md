# R2 Download Relay v1 技术需求与验收标准

- 需求编号：RLY-DL-001～020
- 跟踪任务：[Issue #63](https://github.com/sunxu/r2-relay/issues/63)
- 状态：Owner 已确认设计基线；尚未实现、尚未部署 VPS。
- 原则：下载是独立能力，不改变现有 UploadPart PUT 上传行为。

## 1. 目标

本地代理接管所有发往 R2 的 GET，由 VPS 使用原始 URL 和请求头向 R2 发起一次 GET。VPS 将响应流直接写成独立 Part 文件；Mac 按 Part 编号并发取回，在本地内存或磁盘组装，完整验证后才向原始客户端返回响应。

只在 VPS → Mac 并发。R2 → VPS 只发起一次 GET，不做 R2 Range 并发。每个原始 GET 创建独立任务；v1 不做跨请求任务合并或对象去重。

## 2. Owner 已确认参数和配置

| 参数 | 值 | 配置位置 | 使用方 |
|---|---:|---|---|
| Part 大小 | 16 MiB | VPS /etc/r2-relay/config.toml | VPS |
| 分片阈值 | 严格大于 24 MiB | VPS /etc/r2-relay/config.toml | VPS |
| 单文件最大并发 | 16 | Mac ~/.config/r2-relay/config.toml | Mac |
| 全局最大并发 | 32 | Mac ~/.config/r2-relay/config.toml | Mac |

VPS 的 [download] 配置段包含 part_size_mib=16、split_threshold_mib=24、cache_ttl_seconds=600。Mac 的 [download] 配置段包含 max_concurrency=16、global_max_concurrency=32，并与现有上传配置共存。

四个参数必须通过 TOML 加载、严格校验，不得硬编码或要求通过环境变量维护。Token、VPS 地址、TLS CA 等凭据和连接信息继续按现有安全方式管理。VPS manifest 是每个任务 Part 布局的唯一权威来源。

## 3. 请求和响应处理

- **RLY-DL-001 GET 接管：** 所有发往 Cloudflare R2 S3 endpoint 的 GET 均由本地代理接管并交由 VPS 执行，不限于 GET Object。v1 沿用现有上传目标识别边界：HTTPS、目标端口 443（URL 未显式指定端口时按 443）、主机名以 .r2.cloudflarestorage.com 结尾。非目标域名和非目标端口的 GET 按普通代理透传；未来若要支持 R2 自定义域名，另行增加显式 allowlist 配置和验收。
- **RLY-DL-002 原始请求：** 转发原始 URL、查询参数和必要请求头，不擅自改写 SigV4 签名内容。不设计签名 URL 权限缓存。
- **RLY-DL-003 单次上游请求：** VPS 对每个原始 GET 只发起一次 R2 GET，不发起多个 Range 请求。
- **RLY-DL-004 响应模式：** 实际响应体 <=24 MiB 时，Mac 使用内存；>24 MiB 时，VPS 生成 16 MiB Part，Mac 使用磁盘。204/304 等无响应体响应直接返回。3xx/4xx/5xx 保留真实状态、响应头和响应体，不得伪造成成功；不自动跟随重定向。
- **RLY-DL-005 未知长度：** 缺少 Content-Length 时不得无限制缓冲内存。VPS 使用有界临时 spool；超过 24 MiB 转 Part 模式，最终不超过阈值则作为小响应返回。临时 spool 不得被读取接口当作 READY Part。

## 4. VPS Part 生成与任务管理

- **RLY-DL-006 流式分片：** 已知响应长度超过阈值时，VPS 从 R2 响应流直接生成 Part，不先保存完整对象再二次切割。每片 16 MiB，最后一片可更小。
- **RLY-DL-007 原子发布：** 每个任务有独立目录和 metadata。写入文件使用 N.part.tmp；完整写入、关闭并校验长度后原子重命名为 N.part。读取接口永远不能读取临时文件。
- **RLY-DL-008 Part 状态：** WRITING、READY、FAILED。只有 READY 可读取；Part READY 后立即提供，不必等待整个对象下载结束。
- **RLY-DL-009 等待：** 本地请求未 READY 的 Part 时，VPS 等待 READY、任务失败/取消或超时；不得返回半个文件。等待请求不占全局数据传输名额，但等待连接数必须有上限和超时。
- **RLY-DL-010 Manifest：** 至少包含 task_id、任务状态、R2 状态码、原始端到端响应头、总长度、Part 大小/数量/已发布 Part 长度、失败原因和过期时间。不得返回或记录 Token 等凭据。

## 5. Mac 取回、组装和返回

- **RLY-DL-011 并发：** 单文件最多 16 个 Part 同时传输；所有任务合计最多 32 个。使用一个共享全局调度器；等待未 READY Part 不占数据传输名额；重试也必须经过调度器。
- **RLY-DL-012 小响应：** <=24 MiB 使用内存，不创建大文件缓存。必须有独立的全局小响应内存预算；预算耗尽时排队，不得无限分配。预算默认值在内存压力测试后确定。
- **RLY-DL-013 大响应：** >24 MiB 使用一个本地任务级临时文件。按 VPS manifest 的偏移和长度定位写入，不在 Mac 保存第二套 Part 文件、不二次合并。每片校验实际长度；建议 VPS 同时计算 SHA-256 并由本地验证。
- **RLY-DL-014 完整后返回（已接受方案 A）：** Mac 可以在 VPS 下载期间提前取回 READY Part，但必须等待所有 Part 收齐、长度/摘要校验通过且 VPS 任务 COMPLETE，才向原始客户端发送响应体。大文件从本地临时文件流式返回，不得整体读入 Python 内存。确认不再需要 VPS 文件后通知删除。

## 6. HTTP 语义

- **RLY-DL-015 响应头：** 保留 VPS 从 R2 获得的真实状态码和端到端响应头；过滤 Connection、Transfer-Encoding、Keep-Alive、Upgrade 等 hop-by-hop 头。Content-Length 必须与实际字节数一致。Content-Encoding 必须与交付的原始字节一致，避免自动解压后仍保留旧编码/长度。
- **RLY-DL-016 原始 Range：** 原始客户端的 Range、If-Range、If-None-Match 等条件请求原样交给 VPS/R2。R2 返回 206 时，对该 206 响应体分 Part；本地内部仍按 Part ID 取回，不使用 HTTP Range。最终保留真实状态和 Content-Range、ETag。多 Range 响应作为原始 multipart/byteranges 字节流处理，不自行重建边界。

## 7. 失败、清理和资源保护

- **RLY-DL-017 失败安全：** 上游失败后任务 FAILED，未完成 Part 不得发布。已发布部分 Part 后上游失败，不得重新 GET 后把不同对象版本混入旧任务。Part 传输中断可重新请求 READY Part，仍受并发限制。创建使用 request_id/idempotency key；删除幂等。v1 不静默绕过 VPS 直连 R2；VPS 不可用返回明确代理错误。
- **RLY-DL-018 删除/TTL：** 本地完整接收并验证后通知 VPS 删除；默认 TTL 600 秒兜底。TTL 为空闲过期，从上游完成后开始计时，活动读取刷新；正在写入或有活动读取者时不得删除。失败/取消尽快清理；正常完成不等待 TTL。
- **RLY-DL-019 资源保护：** VPS 必须限制缓存磁盘预算、最低剩余空间、活动任务、Part 等待者和等待时间；Mac 必须限制小响应总内存和临时目录空间。具体容量值根据 VPS 磁盘检查和 Mac 内存测试确定，不臆造。
- **RLY-DL-020 隔离：** 下载有独立 API、任务状态、缓存目录、调度器和指标；不复用上传 MD5 缓存键、上传执行记录或上传 TTL。现有上传配置和行为不变。

## 8. 逻辑 API

最终 URL 和 JSON schema 在实现 PR 冻结，逻辑上包括：
1. 创建任务：提交原始 GET URL/headers 与 request_id，返回 task_id 和初始元数据。
2. 查询 manifest：响应状态/头、任务状态、长度、Part 布局和 READY 状态。
3. 获取 Part：按 task_id + part_number 请求；未 READY 时等待，READY 时返回完整 Part 和校验信息。
4. 获取小响应 body：仅用于 <=24 MiB。
5. 删除/取消任务：幂等，不破坏正在写入或读取的数据。

所有 VPS API 沿用现有 Relay Bearer 鉴权和 HTTPS。签名 URL 权限缓存不属于 v1。

## 9. Gate 0：mitmproxy 磁盘响应 PoC（实现前置门槛）

当前 addon 使用完整 bytes 构造响应，不适合 GiB 文件。完整实现前必须验证：从 Mac 磁盘向客户端发送 1 GiB；不将完整文件读入 Python 内存；HTTP/1.1 和 HTTP/2 均通过；Content-Length/Content-Encoding/响应头正确；客户端中途断开可停止发送并清理文件。

若 mitmproxy addon 无法可靠实现，评估受控 Loopback 文件响应服务或其他可验证方案；不得未经验证就认定 response stream 能直接充当任意磁盘文件响应源。Gate 0 不部署 VPS、不改变生产服务。

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
| DL-T10 | 边下边取 | VPS 未完成对象时，本地可取 READY Part |
| DL-T11 | 完整返回 | 所有 Part 和任务 COMPLETE 前客户端未收到响应体 |
| DL-T12 | Range | 原始 Range 正确返回 206/Content-Range；内部使用 Part ID |
| DL-T13 | 状态码 | 204/304/重定向/403/404/5xx 语义正确 |
| DL-T14 | 响应头 | 原始字节、Content-Encoding、Length、ETag 一致；逐跳头过滤 |
| DL-T15 | 未知长度 | 不无限缓冲；模式切换和最终长度正确 |
| DL-T16 | 上游失败 | 不发布半成品、不混合重试对象 |
| DL-T17 | Part 重试 | 只重取完整 READY Part，仍受 16/32 限制 |
| DL-T18 | TTL | 正常完成主动删除；600 秒空闲过期；活动读写不被删除 |
| DL-T19 | 幂等 | 创建重试不重复执行；删除重试安全 |
| DL-T20 | 资源 | VPS 磁盘/任务/等待者和 Mac 内存/临时空间均有界 |
| DL-T21 | mitmproxy PoC | 1 GiB 磁盘响应在 HTTP/1.1/2 通过，内存不随文件大小线性增长 |
| DL-T22 | 上传回归 | 既有 UploadPart 测试、默认配置和行为不变 |
| DL-T23 | 全栈模拟 | aiohttp → mitmproxy → VPS HTTPS → Mock R2 覆盖小/大响应、失败和清理 |
| DL-T24 | 真实 R2 | GET/Range GET、响应头、签名请求和故障行为单独记录 PASS_REAL；Mock 不替代真实验收 |

## 11. 实施阶段

1. Stage 0：mitmproxy 磁盘响应 PoC，先过 DL-T21。
2. Stage 1：VPS 任务状态、单 GET、流式 Part、manifest、等待式 Part API、TTL 和磁盘保护。
3. Stage 2：Mac GET 接管、TOML、16/32 调度、小响应内存和大响应磁盘组装。
4. Stage 3：HTTP 语义、失败恢复、删除、Mock R2 全栈和上传回归。
5. Stage 4：真实 R2 验收和性能测量。

每阶段通过独立 PR 和 CI。生产 VPS 部署必须另行取得 Owner 明确授权；本需求不授权部署。

## 12. v1 不做

- 不做 R2 → VPS Range 并发。
- 不让 Mac 向 VPS 发 HTTP Range；内部按 Part ID 请求。
- 不保存 VPS 完整对象后再二次切割。
- 不在 Mac 保存第二套 Part 文件后再合并。
- 不做跨 GET 缓存复用/任务去重。
- 不做签名 URL 权限缓存。
- 不改变现有上传 MD5 缓存、1800 秒 TTL 和上传配置。
- 不向原始客户端边下载边发送；完整取回和验证后再返回。
