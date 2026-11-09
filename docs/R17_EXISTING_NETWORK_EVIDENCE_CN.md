# R2 Relay #17 真实网络测试现有证据与范围调整

日期：2026-11-09  
Issue: [#17 真实本地→VPS 上行利用率与稳定性验收](https://github.com/sunxu/r2-relay/issues/17)  
基线生产 Release：`eafa6ed358eca98b9d3b91d71565ffae74914770`  
报告性质：历史测试结果整理；本次提交没有发起新的网络请求、压力测试或生产变更。

## 1. 测试范围决定

根据 Owner 明确决定：

- 不执行 4、8、16 并发测试。
- 不为了填满并发矩阵而重复已有 1、2 并发测试。
- 不把客户端并发数等同于服务端实际并发处理数。
- 仅整理已经产生的真实网络测试结果；缺少原始证据的指标标记为 NOT_RECORDED，不推算、不补造。
- 真实 Cloudflare R2、SigV4、Multipart Complete、VPS→R2 路径不属于本报告可证明范围。

## 2. 已有真实网络结果

下列数据来自 Owner 在 2026-11-08 至 2026-11-09 提供的终端测试回执与历史对话记录。当前仓库尚未保存这些原始终端日志，因此标记为 **OWNER_REPORTED**，不是由本次 CI 重跑产生。

| 测试 | 负载 / 并发 | 已记录结果 | 统计口径 |
|---|---|---:|---|
| HTTP/2 单请求 | 20 MiB / 1 | 约 42.38 Mbps | 两次测试平均 |
| HTTP/1.1 单请求 | 20 MiB / 1 | 约 86.05 Mbps | 两次测试平均 |
| Relay MISS 路径 | 2 × 20 MiB / 2 | 约 56.97 Mbps | 客户端端到端总吞吐 |
| Relay MISS Data 阶段 | 2 × 20 MiB / 2 | 约 78.4 Mbps | 数据传输阶段聚合吞吐，不含完整端到端耗时 |
| 独立 sink | 20 MiB / 1 | 服务端约 49.11 Mbps | 服务端接收阶段 |
| 独立 sink | 2 × 20 MiB / 2 | 聚合约 83.03 Mbps | 客户端 wall-clock 聚合吞吐 |

### 双并发 sink 原始回执

- 两个请求均发送 20,971,520 bytes。
- 客户端总 wall time：4.041 秒。
- 聚合吞吐：83.03 Mbps。
- 服务端请求 1：48.66 Mbps，server_elapsed_ms 3447.83。
- 服务端请求 2：48.71 Mbps，server_elapsed_ms 3443.98。
- 两个 HTTP 请求均返回 200。

计算口径：

`40 \times 1024^2 \times 8 / 4.041 / 10^6 \approx 83.03\ Mbps`

独立 sink 的作用是接收请求体后丢弃，不涉及 R2 Cache 写入、fsync、Worker 或 R2 请求。其服务端吞吐和客户端端到端吞吐不是同一统计口径。

## 3. 测试路径和边界

独立 sink 测试：

`Mac curl → 公网 HTTPS :8443 → VPS FastAPI /v1/bench/sink`

该路径直接访问 VPS，不经过 Mac 本地 18585 MITM 代理。它用于隔离测量 Mac→VPS HTTPS 请求体接收能力。

Relay MISS 测试涉及本地代理、VPS 缓存写入与请求协调，不能与 sink 结果直接视为同一测试路径。

当前生产版本的 sink 端点限制（见 `docs/NETWORK_BENCHMARK.md` 和 `vps/r2relay_vps/app.py`）：

- Bearer Token 鉴权。
- 请求必须提供 Content-Length。
- 单请求默认最大 32 MiB。
- sink handler 默认并发 semaphore 为 4。
- 请求体流式接收、校验声明长度后丢弃。
- 不写缓存、不访问 R2。

因此，历史客户端双并发 sink 结果是在服务端上限 4 以内完成；本报告不安排更高客户端并发。

## 4. 尚无充分证据的项目

以下状态不影响记录已有吞吐结果，但不能宣称 #17 全部验收通过。

| 项目 | 状态 | 说明 |
|---|---|---|
| Mac CPU / 内存峰值 | NOT_RECORDED | 历史回执未提供可核验数值 |
| VPS CPU / RSS 峰值 | NOT_RECORDED | 历史回执未提供可核验数值 |
| VPS 网卡实时吞吐 | NOT_RECORDED | 缺少同一轮测试的接口采样 |
| P50 / P95 延迟分布 | NOT_RECORDED | 当前只有单次耗时和聚合吞吐 |
| 多轮长时间稳定性 | NOT_TESTED | 未发现足够的连续负载证据 |
| 网络中断恢复 | NOT_TESTED | 未发现对应故障注入回执 |
| VPS 重启后请求恢复 | NOT_TESTED | 缓存恢复与进行中任务恢复需区分 |
| 缓存过期对性能的影响 | NOT_TESTED | 没有同一条件下的对照记录 |
| 同等条件原始直连 vs Relay | PARTIAL | 有不同路径数据，但未形成严格同负载、同时间窗口对照 |
| 真实 R2 PUT / Multipart Complete | BLOCKED_REAL_R2 | 无可用独立测试 Bucket 和凭据 |
| VPS→R2 公网性能 | NOT_TESTED | 不得从 Mac→VPS 结果推断 |

## 5. 结论

1. 已有真实网络数据证明：在当时测试条件下，HTTP/1.1 单请求和双并发 sink 均取得约 83–86 Mbps 量级的观测吞吐；不同路径与统计口径必须分开解释。
2. 双并发 sink 的 83.03 Mbps 是当前最清晰的 Mac→VPS 纯网络接收基线之一。
3. 现有数据不足以证明可用上行利用率在持续负载下稳定达到 90%；也不足以定位 CPU、RSS 或网卡瓶颈。
4. 本次不新增任何 4/8/16 并发测试，不将其列为未来待办。
5. 真实 R2 Multipart 和 VPS→R2 继续保持 NOT_TESTED / BLOCKED_REAL_R2。

## 6. 后续处理

- 将本报告链接回 Issue #17。
- 以已有数据作为历史基线，不重复跑相同短时单/双并发测试。
- 若 Owner 以后决定补稳定性或资源监控，必须单独定义测试窗口和安全边界；本报告不自动授权生产压力测试。
- Issue #17 暂不关闭：真实 R2 阻塞及部分稳定性证据尚未解决。可将 Mac→VPS 已有吞吐测量标记为已完成子项。
