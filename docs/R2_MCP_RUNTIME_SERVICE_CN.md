# R2 Relay 运行时验收和 systemd 准备（MCP）

阶段 1：工具 vps_r2_runtime_smoke 通过 rootd 固定动作，以 r2relay 非特权运行 /usr/local/libexec/r2_runtime_smoke.py。使用临时 CacheStore、假 token 和 FastAPI TestClient 验证 /healthz=200、/v1/metrics 未授权=401 与已授权=200；不访问真实 R2，不读取生产密钥，不启动服务。

阶段 2：vps_r2_service_prepare 通过 rootd 固定动作仅安装 /etc/systemd/system/r2-relay-vps.service 和 /etc/r2-relay/vps.env。root 以 secrets.token_urlsafe(48) 创建随机 token，环境文件权限 0600，不在 MCP 响应中输出。仅调用 systemctl daemon-reload，不 start、不 enable。不修改 sing-box/443，也不创建 Caddy 配置。

注意：此时本地客户端还没有相同 token，不能宣称端到端可用。后续需要受控的秘密分发方式、端口和 HTTPS 8443 验证，然后再分离授权启用服务与真实 R2 测试。

首次接入需要以可信 root SSH 更新 root-owned 的 ops/r2_rootd.py、ops/generic_vps_mcp.py，并新增 ops/r2_runtime_smoke.py 与 ops/r2_service_prepare.py 到 /usr/local/libexec。先备份旧文件，再 py_compile 并重启 r2-rootd.service、r2-mcp.service，检查 Tunnel。绝不可绕过 rootd 的固定动作来执行任意 shell。
