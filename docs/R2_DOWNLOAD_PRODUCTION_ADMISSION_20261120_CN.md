# R2 GET 生产准入与正式启用 v1（2026-11-20）

## 授权与生产边界

Owner 已于 2026-11-20 明确授权 R2 Relay 下载的生产准入、正式部署和启用。**不授权绕过 Root Bootstrap、GitHub required checks、Release SHA、TLS 或回滚健康检查**。现有 UploadPart PUT、18585 生产代理和 Cloudflare 原始对象必须保持不受破坏。启用 VPS 后，Mac 端需另行受控切换 TOML 并重启实际代理才能正式接管生产 GET；不能因 VPS health=true 就声称 Mac 生产链路已切换。

## 1. 资源准入（VPS 只读快照）

- 根盘约 38 GiB，已用 3.1 GiB，**可用约 33 GiB**；内存总共 **2.0 GiB**，当时可用约 **1.5 GiB**。
- 原有 VPS 18085 仅监听 `127.0.0.1`，443 为现有 sing-box，8443 为 Caddy；不改变任何端口和监听地址。
- `UNIFIED_DEPLOYMENT_STATUS_PASS`、`SERVICE_VERIFY_PASS`。现有活动 Release `a77f2414bd32370cf8f6f5ac78014fbf923e605a`，受控下载上次已显式 `disabled`。
- 小响应 12.06 MiB、完整 24.13 MiB 的真实 R2 RelayClient 均 PASS_REAL，独立 18586 真实代理 PASS_REAL_PROXY。Mock VPS 验证一次上游 GET、16+9 MiB 两 Part；真实网络的单次 GET 和准确 Part 数量尚缺独立运行时计数，不假装已取证。

## 2. 固定生产配置 production-download-v1

生产配置只由 Receiver v8 固定写入 `/etc/r2-relay-download/download.toml`，不接受任意 TOML / 用户提交的参数 / 环境覆盖：

| 项目 | 生产固定值 |
|---|---:|
| enabled | true |
| 单 Part | 16 MiB |
| 分片阈值 | 严格大于 24 MiB |
| 缓存 TTL | 600 秒 |
| 总缓存预算 | 8 GiB = 8,589,934,592 bytes |
| 最低磁盘余量 | 8 GiB = 8,589,934,592 bytes |
| 初次启用所需空闲磁盘 | 至少 16 GiB |
| 活动任务上限 | 2 |
| 保留任务上限 | 4 |
| READY 等待者 | 8 |
| 等待超时 | 30 秒 |
| test_expires_at_epoch | 0（生产配置不会因测试 timer 关闭） |

这不是“不限量生产”：单任务若导致 8 GiB 缓存预算耗尽，会安全失败，不能宣称能下载任意大小文件。实际任务峰值、磁盘余量和延迟需持续监测，再以单独审查的版本调整容量。单任务最大本地磁盘预算也应由 Mac 独立配置控制。

## 3. 启用保护

- 只允许 Issue #63 的 Owner 评论 `/download-config production <已部署且通过 deploy-tests 的 40 位 main SHA>`。Issue #41 继续仅允许 `disabled`。
- Receiver v8 必须通过 Owner 的原 Root SSH Bootstrap 更新；**部署 Release 并不会自动升级 Root 安装的 Receiver**。
- 新 Release 必须同时包含 `deploy/download-config-dropin-v1` 与 `deploy/production-download-v1` 正确标记，SHA 必须与 VPS 当前活动 Release 一致。
- 启用要求原配置严格等于固定 disabled profile、VPS 下载目录安全、原有服务 health=false 且无有效测试租约；若空闲磁盘少于 16 GiB 则拒绝。
- 受控配置备份、原子写入、systemd 重启、health=true；失败则回退固定 disabled 并再次验证 health=false。回退失败则停止 R2 Relay 服务，不允许悄悄维持 enabled。
- 禁止生产 enabled 期间直接部署新 Release；升级先 `disabled`、验证，再部署并重新受控启用。
- 永久关闭／紧急回滚：`/download-config disabled <当前活动 SHA>`；此操作不恢复旧 enabled 文件。
- 原有 30 分钟 `real-test` 依旧可用，但与生产启用相互隔离。

## 4. 执行顺序（严禁跳过）

1. PR 的 `deploy-tests`、pytest、RC1/全栈等所有 required checks 通过，并正常合并 main；main-push `deploy-tests` / pytest 均通过。
2. 在 Owner 本机以精确 main Merge SHA 获取 `ops/r2relay_deploy_receiver.py` 和 `ops/bootstrap_github_actions_deploy.py`，使用原可信 Root SSH 通道执行 Bootstrap dry-run，验证原 Ed25519 fingerprint，然后 `--apply`，无须重启 sshd。完成空归档拒绝烟测，必须显示 `r2-relay-deploy-receiver-v8-production-download-v1`。
3. 在 Issue #63 评论 `/deploy <SHA>`，须收到 `DEPLOYED` / `health=PASS`，并且此时 `download_enabled=false`。运行 VPS 只读部署／服务验证。
4. 在 Issue #63 评论 `/download-config production <同 SHA>`。检查 `DOWNLOAD_PRODUCTION_ENABLED`、`profile=production-download-v1`、上述固定资源限额、`download_enabled=true`、实际 Caddy TLS/HTTP/2 与健康检查。
5. Mac 将合并后的 main 代码安装到**独立测试代理 18586** 并以独立配置确认正常，之后为 18585 生产代理执行受控配置切换和仅本地代理滚动重启；必须单独确认 Mac 配置实际加载 enabled=true。不得修改 Token、VPS 证书私钥，或关闭 TLS 校验。
6. 核对真实业务 GET 成功、连续故障和磁盘余量；如失败立刻禁用 VPS 并在 Mac 下载配置中回退 disabled，保留上传功能；不要在 VPS 环境文件 / Caddy 配置 / 上传缓存做非必要变更。

## 5. 完成判定

**生产准入代码就绪** 不等于 **VPS 已启用**，也不等于 **Mac 18585 已启用**。分别保留 SHA、Actions URL、Receiver 回执、VPS 状态、Mac 进程及配置的脱敏验收证据。真实运行前不要标记 `PRODUCTION_ENABLED`。

Production 运行不具有自动租约到期，必须依靠显式关闭、磁盘保护和监控。不能把此行为描述为与 30 分钟测试配置同等自动恢复。


## 6. 正式生产签收（2026-11-20，已执行）

> **本节是执行结果，优先于前文“资源准入快照”和“待执行流程”的历史状态。** 前文旧 Release、disabled 等描述对应的是生产启用前快照，不代表当前状态。

- 正式 VPS Release 与 Mac worktree：`ffccaac302df167078c97cbe9968699f62242f0a`；Receiver 已受控升级 v8，正式生产 Actions [44701872701](https://github.com/sunxu/r2-relay/actions/runs/44701872701) 成功，下载配置 `production-download-v1` / `enabled=true`。
- Owner 重启 Mac 后 `127.0.0.1:18585` 使用生产虚拟环境和原 `~/.config/r2-relay/config.toml`，下载开关已打开；上传保持单 Part 1 MiB、全局 1024 MiB。
- 本地自动化测试 115 passed + 8 subtests passed；真实 18585 GET 全 Range HTTP 206、25,301,339 字节，SHA-256 `aeb9681c80723f604b8926542b4372e3dd3641b4f1bd1275ee023b821a4c4ddd`，Content-Range、Content-Length、ETag 均与 R2 直连基准一致。
- 独立只读 VPS 验证 `UNIFIED_DEPLOYMENT_STATUS_PASS` 和 `SERVICE_VERIFY_PASS`；但未独立读取本次任务的 VPS manifest / 实际上游 GET 次数。
- Owner 当前无真实 R2 PUT 上传环境，本轮 **NOT_TESTED_ENV_UNAVAILABLE**；不搭建额外环境、不阻塞已验证的 GET 上线，也不宣称上传回归 PASS。
- 详细签收及当前回滚边界见：[R2 GET 生产部署与验收签收](R2_DOWNLOAD_PRODUCTION_ACCEPTANCE_20261120_CN.md)。


## 7. 容量调整申请（2026-11-21；尚未在 VPS 应用）

Owner 根据 IOTA 实际 R2 GET 并发时 `POST /v1/downloads` 返回 HTTP 503 的日志，明确要求将 VPS 生产下载任务上限修改为 `max_active_tasks = 24` 和 `max_retained_tasks = 32`。此项属于 **production-download-v2 待部署变更**；上文 v1 的 2/4 和此前真实 GET 签收均为历史生产基线，不能将代码准备误称为现网生效。

- 只修改这两个任务上限；`max_waiters = 8`、8 GiB 缓存预算、8 GiB 最低空闲、16 MiB Part、24 MiB 阈值、600 秒 TTL 均不变。提高任务数不意味着提高磁盘容量或等待者数量，仍可能出现 503。
- 使用新的 `deploy/production-download-v2` 能力标记、固定 `production-download-v2` 配置、Receiver v9、GitHub Actions 精确回执校验；保留 v1 标记作历史兼容，不开放任意数值传参。
- **必要的维护窗口**：先在 Issue #63 通过当前活动 SHA 执行 `/download-config disabled <旧SHA>` 并确认服务健康；再合并 PR、在 Owner 本机用受信 Root SSH Bootstrap 升级 Receiver v9、通过 Issue #63 执行 `/deploy <新main SHA>`，最后执行 `/download-config production <新main SHA>`。确认回执 `profile=production-download-v2`、`max_active_tasks=24`、`max_retained_tasks=32` 以及 VPS 健康，再恢复 IOTA 下载。不要在生产任务进行中重启服务。
- 任一步失败维持 disabled 或旧 Release，不直接编辑 root-owned drop-in，不绕过 GitHub required checks；生产配置应用后仍须对真实 IOTA 并发观察 503、缓存空间与等待者上限。
