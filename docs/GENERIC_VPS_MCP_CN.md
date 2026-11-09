# 通用 VPS MCP v1 接入与当前工具说明

## 当前部署状态（2026-11-09）

通用 VPS MCP 已接入当前 ChatGPT bandwagonhost Secure MCP Tunnel，以下工具已在当前会话工具目录中可用：

- `vps_system_snapshot`：只读系统资源、白名单服务与监听状态。
- `vps_project_doc_read`：读取固定工作区内 R2 Relay 的公开文档。
- `vps_git_stage`：仅 Stage 固定仓库与 40 位完整 Commit SHA，不执行代码。
- `vps_r2_install_prepare`：固定安装准备器；已安装目标存在时 fail closed，不覆盖。
- `vps_r2_runtime_smoke`：临时目录、假 Token、本地 TestClient 的无网络烟测。
- `vps_r2_service_prepare`：安装固定 systemd unit 与本机 Token，不启动。
- `vps_r2_service_verify`：root-only 只读服务与权限核验。
- `vps_r2_loopback_start`：受控启动仅监听 127.0.0.1:18085 的 API。
- `vps_r2_https_preflight/prepare/start/ca_fingerprint`：固定 IP 自签名 HTTPS 8443 的准入、准备、启动和公钥指纹检查。
- `vps_r2_deployment_status`：唯一权威统一只读部署状态入口。

旧的 `deployment_inspect` 是 Legacy 兼容工具，不作为生产验收入口。

## 安全边界

- MCP 层保持非特权运行；需要 root 权限的操作经 SO_PEERCRED 验证的 Unix Socket 调用 rootd 固定动作白名单。
- 不提供任意 root shell、任意命令、任意仓库或任意文件路径。
- 安装/部署仅接受固定仓库与完整 Commit SHA；生产 Release 由受控 Receiver 验证并切换。
- 凭据和 Token 不进入 MCP 响应；统一状态检查仅读取 EnvironmentFile 元数据，不读取内容。
- 443 由现有 sing-box 使用，不允许 R2 Relay/Caddy 接管；R2 Relay API 仅监听 loopback 18085，Caddy 对外提供 8443。
- 真实 R2 上传必须另有明确授权和独立测试 Bucket；当前没有把真实 R2 请求作为部署状态检查的一部分。

## 生产验收顺序

1. 使用 `vps_r2_deployment_status` 作为唯一权威状态检查。
2. 只有在明确部署变更已获授权时，才运行固定 Bootstrap/Receiver。
3. 部署后再次调用统一状态检查，确认 Release SHA、EnvironmentFile、systemd release layout 与 SERVICE_VERIFY_PASS 一致。
4. 不要将 `RUNTIME_SMOKE_PASS` 或 `UNIFIED_DEPLOYMENT_STATUS_PASS` 解释为真实 R2 上传成功。

历史安装、Runtime Smoke、Loopback Start 与 HTTPS 部署说明见：

- [固定 Commit 安装准备](R2_MCP_INSTALL_INTEGRATION_CN.md)
- [运行时烟测与 systemd 准备](R2_MCP_RUNTIME_SERVICE_CN.md)
- [root-only 服务核验与 Loopback 启动](R2_MCP_LOOPBACK_CONTROL_CN.md)
- [自签名 HTTPS 8443 部署](R2_HTTPS_IP_MCP_DEPLOY_CN.md)
