# 验收矩阵

状态（2026-11-09）：RC1 Runner 模拟/本地真实进程测试已通过；真实 VPS R2 Relay API 与 Caddy HTTPS 8443 已部署，统一只读检查 `UNIFIED_DEPLOYMENT_STATUS_PASS`，Runtime Smoke `RUNTIME_SMOKE_PASS`。真实外部客户端 TLS/IP SAN 验收仍由 #33 跟踪；真实 Cloudflare R2 SigV4/Multipart 仍由 #16 跟踪。Mac→VPS 短时吞吐基线已记录并关闭 #17。各 Txx 必须区分 `PASS_SIMULATED` 与 `PASS_REAL`，不得将模拟覆盖误写为真实 R2 PASS。

| 测试 ID | 需求 | 场景与通过条件 |
|---|---|---|
| T01 | RLY-001/003 | aiohttp 使用显式代理 18585、合并 CA，完成正确的 R2 HTTPS CONNECT/TLS 验证。 |
| T02 | RLY-002/013 | 非目标 HTTPS 直接隧道、R2 非 UploadPart 正常转发，合并请求无特殊处理。 |
| T03 | RLY-004/005 | 读取完整 10 MB 请求体计算 MD5，MD5 对应预期字节；无 MD5 请求头仍正常。 |
| T04 | RLY-006/007 | MISS 时正文只向 VPS 发送一次，完整持久化后立即进入 R2 上传流程。 |
| T05 | RLY-005/006 | HIT 时不向 VPS 发送正文，传输字节统计为零。 |
| T06 | RLY-014 | 同 MD5 + 不同 UploadId/Key 复用相同缓存字节，但 R2 PUT 使用当前请求 URL。 |
| T07 | RLY-008 | 首次完整落盘 1800 秒时停止命中；命中和失败重试不续期。 |
| T08 | RLY-009 | VPS 重启扫描完整缓存文件并剔除已过期文件，进程内队列不声称恢复。 |
| T09 | RLY-010 | 首次 R2 502、重试成功，最多总计 4 次请求并返回真实结果。 |
| T10 | RLY-010/011 | 重试耗尽、签名无效、网络异常时不伪造 R2 ETag/成功；区分合成代理错误与 R2 响应。 |
| T11 | RLY-011 | 完整保留真实 ETag、状态、端到端头和响应正文，去除逐跳头。 |
| T12 | RLY-012 | VPS 连接失败、超时、磁盘满时可以从本地完整缓冲区直传 R2。 |
| T13 | RLY-012 | R2 返回业务 403/502（由 VPS 正常返回）不错误触发本地直传竞争。 |
| T14 | RLY-004 | 超出内存阈值的合法分片不导致无限内存增长，按预先确定的直传路径处理。 |
| T15 | RLY-007/008 | 临时文件不可被 HIT；文件到期删除时 R2 任务失败可诊断。 |
| T16 | RLY-006/016 | 使用已存在的 GitHub Runner 100×10 MiB MockTransport 基准作为离线模拟证据；不得将其解释为真实公网吞吐。Owner 已明确不执行 4/8/16 并发测试，不新增此类并发测试。真实 Mac→VPS 吞吐证据单独记录于 Issue #17。 |
| T17 | RLY-005/016 | 重试全部缓存命中时，本地到 VPS 文件正文为 0 B，只有元数据请求。 |
| T18 | RLY-001/012 | 代理重启、VPS 停机、HTTP/2 断链、签名参数复杂时不破坏正常上传路径。 |
| T19 | RLY-017 | 固定 Issue #41 仅接受仓库 Owner 的 `/deploy <完整 SHA>`；拒绝错误 Issue/作者/格式、非 main 祖先及部署专项 CI 未成功的 Commit；SSH 使用独立无密码部署账号，Forced Command 仅能 sudo 执行固定 Root 接收器；成功时只切换并重启 R2 Relay API，健康检查失败恢复旧版本；已存在的目标版本必须校验所有权、权限、符号链接和运行时导入后才能复用；Caddy、8443、凭据、缓存和其他服务不变。 |
| T20 | RLY-018 | 新 Release 必须由受信任的完整 SHA 生成 `.deployment-commit`（root:r2relay、0640）；部署后 verifier 可读取并与 Release 目录 SHA 一致；缺失环境文件、缓存目录、Release 根目录、标记文件及 unit 必须分别报告准确路径错误；旧 Release 缺少标记时不得误报为 `vps.env` 缺失；VPS Bootstrap 必须同步升级 Root Receiver，避免旧接收器继续删除标记。 |
| T21 | RLY-019 | 唯一受支持的生产状态入口 `vps_r2_deployment_status` 直接调用固定 root-only service verifier；验证当前 Release 指针与 commit marker、Release 所有权/权限、EnvironmentFile 权限及必需字段、systemd unit 模板与监听边界；验证失败必须 fail-closed；不得在 MCP 响应中返回凭据或 Token，不修改生产环境。旧 `deployment_inspect` 源码、测试和文档从仓库退役。 |

验收证据：测试命令、环境、代码 commit、原始计数器和日志、错误注入方法、PASS/FAIL。性能要求以本地到 VPS 可用带宽为基准，明确协议开销；对照原始直传 R2。

## RC1 模拟环境与未完成验收的区分

- **已通过 Runner 离线全栈**：aiohttp → mitmdump CONNECT/TLS → Caddy HTTPS/HTTP2 `8443` → VPS Uvicorn → 独立 Mock R2 TCP 服务；验证 MD5 缓存 HIT/MISS、模拟 502/429 重试及 403、ETag 透传、VPS 进程重启后缓存恢复。证据：[Actions #42443068248](https://github.com/sunxu/r2-relay/actions/runs/42443068248)，[中文说明](../RUNNER_FULLSTACK_CN.md)。
- **已通过 Runner 离线负载**：历史结果为 100 × 10 MiB、Runner 内部并发 8，第二轮缓存命中时本地→VPS 分片正文为 0 B。仅作为既有离线模拟证据，不是公网测试；不再扩展或重跑 4/8/16 并发矩阵。真实 Mac→VPS 的已完成观测见 [Issue #17](https://github.com/sunxu/r2-relay/issues/17) 及 [R17 报告](../R17_EXISTING_NETWORK_EVIDENCE_CN.md)。
- **当前仍待外部验收**：外部客户端对 VPS 自签名 CA 的信任、双栈 IP SAN/TLS 与鉴权连通性（[Issue #33](https://github.com/sunxu/r2-relay/issues/33)）；真实 Cloudflare R2 SigV4、真实 ETag、Multipart Complete 与 R2 故障行为（[Issue #16](https://github.com/sunxu/r2-relay/issues/16)）。Mac→VPS 短时网络吞吐基线见已关闭的 [Issue #17](https://github.com/sunxu/r2-relay/issues/17)。
- **状态定义**：`PASS_SIMULATED` 仅表示对应模拟场景通过；`NOT_TESTED_REAL` 表示真实环境尚无证据，绝不等同于 `PASS_REAL`。


## 统一生产状态入口（RLY-019）

- 唯一权威入口：MCP 工具 `vps_r2_deployment_status`，输出 schema `r2_relay_unified_deployment_status_v1`。
- 统一入口直接调用固定 root-only `r2_loopback_control.py` 验证器，不再运行或依赖第二套 Legacy Inspector。
- 验证器检查当前 Release 指针与 commit marker、Release 所有权/权限、EnvironmentFile 权限及必需字段、systemd unit 模板和 443/8443/18085 监听边界。
- EnvironmentFile 值只在 root 验证器内存中用于校验，不会进入 MCP 响应；统一检查不修改生产环境，也不执行真实 R2 请求。
- 旧 `deployment_inspect` 源码、测试和文档已从仓库退役。历史 VPS 上可能残留的文件或 MCP 工具注册不由本次代码变更自动删除。


## R2 Download Relay v1（Issue #63）

| 测试 ID | 需求 | 场景与通过条件 |
|---|---|---|
| T22 | RLY-020/002 | 所有 R2 GET 均由本地代理接管并由 VPS 执行，原始 URL/请求头保留。 |
| T23 | RLY-023 | <=24 MiB 使用本地内存；>24 MiB 使用 Part/本地磁盘。 |
| T24 | RLY-025/007/008 | VPS 从 R2 响应流直接生成 16 MiB Part；仅原子发布的完整 Part 可读取。 |
| T25 | RLY-028 | 未 READY Part 等待；READY 后完整返回；等待不占全局数据传输名额。 |
| T26 | RLY-030 | 单文件最多 16、所有任务全局最多 32 个 Part 同时传输，均由 TOML 配置。 |
| T27 | RLY-032/014 | 本地大文件使用单个磁盘临时文件按偏移写入；全部校验且 VPS COMPLETE 后才向原始客户端返回。 |
| T28 | RLY-034/016 | 真实状态、端到端头、Range 和编码语义保留，hop-by-hop 头过滤。 |
| T29 | RLY-036/018 | 上游失败不发布半成品；正常完成主动删除；600 秒空闲 TTL 兜底；活动读写不被清理。 |
| T30 | RLY-038 | VPS 磁盘与任务准入、本地内存预算和等待者数量有界。 |
| T31 | Gate 0 / T42 | 1 GiB 本地磁盘响应在 regular CONNECT HTTP/1.1 下不整体进入 Python 内存；RelayClient → VPS 的 HTTP/2 单独验收。 |
| T32 | RLY-039 | 现有上传路径、默认配置和既有测试全部回归通过。 |
| T33 | R2 real acceptance | 真实 R2 GET/Range GET 与故障场景 PASS_REAL；Mock 只能标记 PASS_SIMULATED。 |

详细测试矩阵见 [R2_DOWNLOAD_RELAY_V1_CN.md](R2_DOWNLOAD_RELAY_V1_CN.md)。
