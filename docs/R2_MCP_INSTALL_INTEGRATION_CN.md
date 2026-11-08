# R2 Relay MCP 安装集成

现有 generic_vps_mcp.py 增加 vps_r2_install_prepare(apply=false) 工具。默认 dry-run。apply=true 调用 Unix Socket rootd 的固定 r2_install_apply 动作，rootd 只调用 /usr/bin/python3 /usr/local/libexec/r2_install_prepare.py --apply。不允许任意路径、命令、仓库地址、commit 参数。

需经过 CI、安全复核后，用 root SSH 安装审核版本的三个文件：ops/r2_install_prepare.py → /usr/local/libexec/r2_install_prepare.py、ops/r2_rootd.py → /usr/local/libexec/r2-rootd.py、ops/generic_vps_mcp.py → /opt/r2-mcp/generic_vps_mcp.py。三者归 root，只有 MCP 模块允许 mcp-relay 组读取。备份旧文件，py_compile，再依次重启 r2-rootd.service、r2-mcp.service。若 Tunnel 短暂断线，必要时再重启 r2-mcp-tunnel.service。rootd 执行器不占 443 或 8443。

进入 ChatGPT 后先调用 vps_r2_install_prepare(apply=false)，看到 DRY_RUN_PASS 之后才调用 apply=true。成功返回 INSTALLED_NOT_STARTED。不得把 Stage 验证、安装准备等同于生产部署成功。此动作不安装 Caddy、未生成 token、未启动 systemd/公网 HTTPS；真实 R2 测试仍被禁止。
