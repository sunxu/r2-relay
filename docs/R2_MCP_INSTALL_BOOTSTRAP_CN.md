# 一次性升级现有 MCP/rootd，接入 R2 Relay 安装工具

本升级仅改变 root-owned 的三个 Python 文件，不修改 sing-box、SSH、443、Caddy、R2 Relay systemd 单元或环境变量。

## 为什么还需要一次 SSH 引导
现有插件没有任意文件部署权限，rootd 当前动作白名单也不接受新动作；不应借助 shell 注入或 sudo 绕过白名单。本次只需一次由 Owner 执行的、固定完整 Git Commit 安装脚本。之后 ChatGPT 可直接执行 vps_r2_install_prepare(false/true)。

## 推荐操作
使用审核并合并后的完整 SHA：

    COMMIT=REPLACE_WITH_MERGED_FULL_SHA
    curl -fsSL "https://raw.githubusercontent.com/sunxu/r2-relay/$COMMIT/ops/bootstrap_r2_install_mcp.py" -o /root/bootstrap_r2_install_mcp.py
    python3 /root/bootstrap_r2_install_mcp.py --commit "$COMMIT"
    python3 /root/bootstrap_r2_install_mcp.py --commit "$COMMIT" --apply

脚本通过 HTTPS 下载同一固定 SHA 的三个文件，先编译和检查路径，备份旧文件；仅重启 r2-rootd.service 与 r2-mcp.service。出现失败时尝试恢复备份并重启。Tunnel 如果因目录同步无法连接，确认 MCP 本地 18765 监听后，单独重启 r2-mcp-tunnel.service。避免将部署输出的任何密钥发到 ChatGPT。

完成后由 ChatGPT 验证工具可见与 DRY_RUN_PASS；只有确认安全后再调用 apply=true。成功安装后系统仍不运行 R2 Relay，/opt/r2-relay/.venv 的应用依赖可供后续离线预检。

## 运行限制
Stage 的 Commit 固定为 d6fa3503127e7e8b91076caff187f6ec095ccf60，并非用户输入；不能覆盖 /opt/r2-relay。如果旧安装目录存在将 fail closed。失败后不要自动清理部分安装，先审查。
