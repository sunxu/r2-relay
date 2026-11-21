# RLY-033：VPS → Cloudflare R2 GET 性能诊断（2026-11-21）

## 范围与安全边界

仅在 VPS 既有 `DownloadManager._run()` 路径增加一条脱敏的 `r2_get_perf` INFO 日志，复用已有 `_raw_chunks`、PartWriter、任务 API 和真实 R2 验收脚本。不改变下载/上传协议、Part 16 MiB、分片阈值 24 MiB、16/32 本地并发、VPS 容量限制、签名头、重试或 IOTA 行为。此代码合并和上线是两回事；未经单独授权不部署 VPS、不重启 Mac/IOTA。

## 日志字段

每个真正开始执行的 VPS GET 任务在完成、失败或取消时输出一条 `r2_get_perf` 日志。所有计时均使用单进程 `time.perf_counter()`；缺少有效阶段的字段为 `NA`，不可按零处理。

| 字段 | 口径 |
| --- | --- |
| `status` / `r2_http_status` | VPS 任务状态、真实 R2 HTTP 状态码（尚无响应头时为 NA） |
| `r2_bytes` | VPS 从 R2 读取的原始正文总字节数；失败时为已收到的部分字节数 |
| `r2_header_elapsed_ms` | VPS 开始上游 GET 到取得 R2 响应头的耗时，含建连/握手 |
| `r2_first_body_elapsed_ms` | VPS 开始上游 GET 到接收到首批正文的耗时 |
| `r2_body_elapsed_ms` | 收到 R2 响应头到消费完上游正文/EOF 的墙钟耗时 |
| `r2_read_wait_ms` | 累计等待每次上游流 `anext()` 返回数据/EOF 的时间（毫秒） |
| `r2_effective_mbps` | `r2_bytes × 8 / (r2_body_elapsed_ms × 1000)`，十进制 Mbps；仅 EOF 完成时有效 |
| `r2_first_part_ready_ms` | 开始 GET 到 VPS 首个 Part 写盘、fsync、SHA-256 并原子发布 READY 的耗时；小响应为 NA |
| `r2_total_elapsed_ms` | 开始上游 GET 到任务得到 COMPLETE/FAILED/CANCELLED 并进入清理的耗时 |

**重要**：`r2_body_elapsed_ms` / `r2_effective_mbps` 是**VPS 实际消费上游响应的有效耗时/吞吐**，包含分片写盘、fsync 和背压，不是独立于 VPS I/O 的 R2 线路测速；`r2_read_wait_ms` 是协程读等待之和，也不是物理线路独占测速。只有结合这些字段与 Part READY 时间才能分辨下一步应检查上游、VPS 磁盘还是 VPS→Mac。不同机器的时钟不做绝对时间相减，只对比各自的耗时。

不打印 R2 签名 URL、Host、请求头、Token、请求 ID、R2 对象名或底层异常文本。任务日志中的状态和字节计数不含私密请求值。

## 最小验收与数据采集

1. 新 PR 先运行既有 VPS pytest/CI：覆盖真实分片规格 Mock（25 MiB → 16+9 MiB）、脱敏日志字段、失败时不输出签名 URL。CI 与模拟数据只验证计时功能，不代表真实 R2 测速。
2. **只有获得明确的 VPS 部署授权后**，使用仓库既有 GitHub Actions 精确 SHA 部署流程，不得直接手改生产服务，也不得因诊断需求自动切换 Mac 18585 或 IOTA。
3. 部署后可用只读命令查看汇总日志：`sudo journalctl -u r2-relay-vps.service --since '10 minutes ago' --no-pager | grep 'r2_get_perf '`。仅截取这一条日志；不要上传其他可能含敏感数据的原始 journald 内容。
4. 复用已有私有 0600、未过期签名 GET 和 `local/poc/real_r2_mitmproxy_acceptance.py` 取得 Mac 端的 `header_elapsed_ms`、`first_body_elapsed_ms`、`first_16mib_elapsed_ms`、`elapsed_ms`。保持现有 24.13 MiB / 206 / SHA-256 规格，不改 R2 对象、不重复构造测试框架。
5. 对同一轮测量比较：R2 头等待、首正文、流读取等待、首片 READY、VPS 完整上游消费、Mac 首正文与完成时间；同时记录 VPS→Mac HTTP/1.1 或 HTTP/2。避免用单轮结果推出固定带宽结论。

**未授权部署或无有效私有签名 URL 时，应停在代码与 CI 验收，不可填造真实 VPS→R2 的速度。**

Refs #63；RLY-033、T37 真实网络性能诊断补充，不替换 T37 正确性验收。
