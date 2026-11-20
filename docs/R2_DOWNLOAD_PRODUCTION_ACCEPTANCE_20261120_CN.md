# R2 Relay GET 生产部署与验收签收（2026-11-20）

> 状态：**生产下载已启用，Mac 18585 真实 GET 验收通过。** 本文件记录实际已获得的证据，不把 Mock 结果或没有环境的 PUT 验收写作生产通过。

## 1. 生产版本与部署

- 仓库：`sunxu/r2-relay`
- 生产基线 SHA：`ffccaac302df167078c97cbe9968699f62242f0a`，该版本对应 GitHub main、VPS 活动 Release 和 Mac 生产 worktree。
- Owner 可信 Root SSH 已执行 Receiver v8 bootstrap：`BOOTSTRAP_APPLIED`；既有 Ed25519 key 指纹未变，`authorized_key_added=false`、`sshd_restarted=false`、SSH forwarding 与 PTY 均禁用。受控账号空归档烟测返回 `REJECTED_OR_FAILED / empty_archive`，Receiver build ID：`r2-relay-deploy-receiver-v8-production-download-v1`，SSH 退出码 1 是预期的安全拒绝。
- 正式 VPS 生产启用通过 Issue #63 命令 `/download-config production ffccaac302df167078c97cbe9968699f62242f0a`，Actions [44701872701](https://github.com/sunxu/r2-relay/actions/runs/44701872701) 成功，返回 `DOWNLOAD_PRODUCTION_ENABLED`、`download_enabled=true`、`profile=production-download-v1`。
- 随后的独立只读核查：`UNIFIED_DEPLOYMENT_STATUS_PASS`、`SERVICE_VERIFY_PASS`；VPS systemd 服务 active；下载 API 仅监听 `127.0.0.1:18085`，Caddy 8443 监听正常；EnvironmentFile 权限 0600。未读取或披露凭据。

## 2. 生产配置与安全边界

### VPS 固定配置（Receiver v8）

| 字段 | 值 |
|---|---:|
| 分片大小 | 16 MiB |
| 分片阈值 | 严格大于 24 MiB |
| 下载缓存 TTL | 600 秒 |
| 下载缓存总额 | 8 GiB |
| 最低磁盘余量 | 8 GiB |
| 最大活动任务 | 2 |
| 最大保留任务 | 4 |
| 最大等待者 | 8 |
| READY 等待超时 | 30 秒 |
| `test_expires_at_epoch` | 0（正式生产无自动到期） |

### Mac 生产代理

- 代理 `127.0.0.1:18585`；Owner 重启后实测监听 PID **38774**，解释器来自 `~/workspace/r2-relay-production/.venv/bin/python`。
- 仍使用原 `~/.config/r2-relay/config.toml`，保留 VPS IPv6 地址、TLS CA/Token 及 `R2_RELAY_HTTP2=0`，不在文档记录任何凭据或签名 URL。
- 上传配置保持 `max_part_mib=1`、`global_buffer_mib=1024`；这意味着实际大于 1 MiB 的 UploadPart **不会走 Relay 缓存**，而是按现有逻辑透传。
- 下载配置 `enabled=true`、`max_concurrency=16`、`global_max_concurrency=32`、`small_response_memory_mib=32`、`max_temp_mib=2048`、`temp_dir=~/.cache/r2-relay/production-downloads`。
- 内部流式响应由 `DiskResponseServer` 在 `127.0.0.1` 监听 OS 分配的临时端口（例如 57791），mitmproxy 内部 GET `/download/<一次性随机令牌>`。出现 `206 Partial Content (content missing)` 是流式代理不缓存正文的表现，不代表内容损坏；真实 SHA-256 另行核验。

## 3. 验收证据

| 验收项 | 结论 | 证据 |
|---|---|---|
| Mac 代码、venv、TOML 加载 | **PASS** | Python 3.11.15；生产模块来自生产 worktree；下载 enabled true、上传参数保留 |
| 本地自动化测试 | **PASS** | 115 passed；8 subtests passed；隔离临时测试配置（下载禁用、上传 16 MiB），不会替换生产 1 MiB 配置 |
| Mac 18585 代理进程 | **PASS** | 监听 PID 38774，生产虚拟环境，配置来源为原 config.toml |
| 真实 R2 GET / Range 内容 | **PASS** | `REAL_GET_PROXY_PASS`；HTTP 206；25,301,339 bytes；状态码、大小、SHA-256、Content-Range、Content-Length、ETag 全部匹配直接 R2 基准 |
| 真实 GET SHA-256 | **PASS** | `aeb9681c80723f604b8926542b4372e3dd3641b4f1bd1275ee023b821a4c4ddd` |
| VPS 版本、systemd、监听 | **PASS** | `UNIFIED_DEPLOYMENT_STATUS_PASS`、`SERVICE_VERIFY_PASS`，Release SHA 一致 |
| VPS 本次 GET 的单任务请求追踪／上游实际 GET 次数／真实 Part 数 | **NOT_INDEPENDENTLY_VERIFIED** | 只读服务检查不读取单个下载任务详情；离线 Mock 已测一次上游 GET 与分片，但不能代替真实网络取证 |
| 真实 R2 PUT 上传回归 | **NOT_TESTED_ENV_UNAVAILABLE** | Owner 明确当前无 R2 上传环境，决定本轮不执行真实上传流程；不能填写 PASS 或 FAIL |

本次验收引用本机原有 0600 私有请求和基准规格：`~/.config/r2-relay/private/real-get-full-spec.json`。URL、Token、认证请求头和私有数据没有上传 GitHub。先前单独 18586 / RelayClient 的真实 R2 验收记录详见 Issue #63 历史讨论和 [真实 R2 验收手册](R2_DOWNLOAD_REAL_R2_ACCEPTANCE_CN.md)。

## 4. 运维及回滚

- **当前不需再次部署、重建 venv、修改配置或重启服务。** VPS 与 Mac 已在同一基线上运行。
- VPS 生产配置不会自动过期。需要紧急关闭下载时，由 Owner 在 Issue #63 使用 `/download-config disabled ffccaac302df167078c97cbe9968699f62242f0a`，随后核验配置回执和服务健康。
- Mac 下载需单独将原 `config.toml` 的 `[download].enabled` 改为 `false` 并由 Owner 自行重启；保留上传参数及凭据原状。
- 升级 VPS Release 前先禁用生产下载，部署新 Release 并通过健康检查后再受控启用，不得绕开 SHA/Receiver 检查。
- 目前不新增上传测试环境，不将真实 PUT 视为下载上线的阻塞项。以后有上传条件或出现上传异常，再执行独立回归。
- 维护时可清理已合并且不再使用的远端开发分支，但不得清理正在运行的 `~/workspace/r2-relay-production` worktree 或其虚拟环境；任何本地 worktree/branch 的删除需由 Owner 在 Mac 上核对占用、未提交改动及可恢复性。

## 5. 跟踪

- [Issue #63](https://github.com/sunxu/r2-relay/issues/63)：需求与生产验收主记录
- [生产准入与正式启用操作说明](R2_DOWNLOAD_PRODUCTION_ADMISSION_20261120_CN.md)
- [真实 R2 验收手册](R2_DOWNLOAD_REAL_R2_ACCEPTANCE_CN.md)
- [生产启用 GitHub Actions](https://github.com/sunxu/r2-relay/actions/runs/44701872701)

**本轮最终结论：R2 Relay GET 生产上线与真实内容完整性验收完成。单任务 VPS 独立追踪未取证，真实 PUT 无环境且未测试，均如实保留。**
