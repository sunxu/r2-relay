R2 Relay 运行时验收 MCP 安装引导

本阶段新增两个固定工具 vps_r2_runtime_smoke 和 vps_r2_service_prepare。由现有 rootd 固定白名单调用，工具不能传递路径/命令。前者以 r2relay 账户、假令牌和临时缓存执行 ASGI 烟测，后者仅准备随机令牌和 systemd unit，绝不启动服务。

合并 PR 后，使用审核后的完整 SHA 下载 ops/bootstrap_r2_runtime_service.py 到 root SSH，再分别执行 python3 脚本 --commit FULL_SHA（dry-run）和 python3 脚本 --commit FULL_SHA --apply。安装器只覆盖固定四个 root-owned 文件并创建独立备份，重启 rootd 与 MCP。必要时更新 Tunnel 工具目录。不得启动 sing-box 或公开 8443。

完成后先调用 vps_r2_runtime_smoke，只有返回 RUNTIME_SMOKE_PASS 才允许下一步 vps_r2_service_prepare。后续通过 `vps_r2_service_verify` 核实 unit、EnvironmentFile 和权限已满足要求，R2 service 仍为 inactive，443/8443 监听边界未改变。Token 不通过 MCP 返回。