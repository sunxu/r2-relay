# 验收矩阵

状态：RC1 已通过 GitHub Runner 的模拟/本地真实进程测试；**尚未完成真实 VPS 公网、Cloudflare R2 和实际带宽验收**。各 Txx 的原始通过条件保持不变；不得将模拟覆盖误写为真实生产 PASS。

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
| T16 | RLY-006/016 | 100×10 MB 首次传输，以独立目标进行 4/8/16 并发测试，记录 MB/s、P95、内存和失败率。 |
| T17 | RLY-005/016 | 重试全部缓存命中时，本地到 VPS 文件正文为 0 B，只有元数据请求。 |
| T18 | RLY-001/012 | 代理重启、VPS 停机、HTTP/2 断链、签名参数复杂时不破坏正常上传路径。 |

验收证据：测试命令、环境、代码 commit、原始计数器和日志、错误注入方法、PASS/FAIL。性能要求以本地到 VPS 可用带宽为基准，明确协议开销；对照原始直传 R2。

## RC1 模拟环境与未完成验收的区分

- **已通过 Runner 离线全栈**：aiohttp → mitmdump CONNECT/TLS → Caddy HTTPS/HTTP2 `8443` → VPS Uvicorn → 独立 Mock R2 TCP 服务；验证 MD5 缓存 HIT/MISS、模拟 502/429 重试及 403、ETag 透传、VPS 进程重启后缓存恢复。证据：[Actions #42443068248](https://github.com/sunxu/r2-relay/actions/runs/42443068248)，[中文说明](../RUNNER_FULLSTACK_CN.md)。
- **已通过 Runner 离线负载**：100 × 10 MiB 并发 8，第二轮缓存命中时本地→VPS 分片正文为 0 B。仅是 Runner 内部模拟，不能替代 T16 对并发 4/8/16 的真实网络 P95、RSS 与失败率要求。
- **待外部环境**：真实 VPS、Cloudflare R2 的 SigV4、真实 ETag、Multipart Complete、公网传输和超时/故障行为；对应 [Issue #16](https://github.com/sunxu/r2-relay/issues/16)、[Issue #17](https://github.com/sunxu/r2-relay/issues/17)。
- **状态定义**：`PASS_SIMULATED` 仅表示对应模拟场景通过；`NOT_TESTED_REAL` 表示真实环境尚无证据，绝不等同于 `PASS_REAL`。
