# R2 Relay root Unix Socket 执行器

目标：MCP 主服务继续以非 root 的 mcp-relay 账户运行，并保留 NoNewPrivileges=yes、空 CapabilityBoundingSet。需要提权的操作由独立 root 服务处理，通讯使用本机 Unix Socket，不开放新 TCP 端口。

## 接入范围
当前仅提供 probe、relay_status、install_prereqs、prepare_dirs 四个固定动作，不能执行任意 shell、指定 apt 包名、修改 sing-box/443、启动 R2 Relay 或执行 R2 上传。rootd 使用 SO_PEERCRED 限定 peer UID、固定 JSON 结构且拒绝重复字段；限制输入/输出及命令运行时间；以 syslog 记录动作，不记录凭据。

## Owner SSH 一次性安装
在 VPS 上由 root 执行；下面的 COMMIT 必须替换为经过审核且已合并 main 的完整 SHA。依赖 curl，但不需要 git。

\`\`\`bash
set -euo pipefail
COMMIT='REPLACE_WITH_FULL_REVIEWED_MAIN_COMMIT_SHA'
BASE="https://raw.githubusercontent.com/sunxu/r2-relay/$COMMIT"
install -d -o root -g root -m 0755 /usr/local/libexec
curl -fsSL "$BASE/ops/r2_rootd.py" -o /tmp/r2_rootd.py
curl -fsSL "$BASE/ops/r2_root_socket_client.py" -o /tmp/r2_root_socket_client.py
curl -fsSL "$BASE/ops/r2-rootd.service" -o /tmp/r2-rootd.service
python3 -m py_compile /tmp/r2_rootd.py /tmp/r2_root_socket_client.py
install -o root -g root -m 0755 /tmp/r2_rootd.py /usr/local/libexec/r2-rootd.py
install -o root -g mcp-relay -m 0640 /tmp/r2_root_socket_client.py /opt/r2-mcp/r2_root_socket_client.py
install -o root -g root -m 0644 /tmp/r2-rootd.service /etc/systemd/system/r2-rootd.service
rm -f /tmp/r2_rootd.py /tmp/r2_root_socket_client.py /tmp/r2-rootd.service
systemd-analyze verify /etc/systemd/system/r2-rootd.service
systemctl daemon-reload
systemctl enable --now r2-rootd.service
\`\`\`

Socket 本机验收：

\`\`\`bash
runuser -u mcp-relay -- /opt/r2-mcp/venv/bin/python -c \
'from sys import path; path.insert(0,"/opt/r2-mcp"); from r2_root_socket_client import root_action_via_socket; print(root_action_via_socket("probe"))'
\`\`\`

然后手工备份现有 /opt/r2-mcp/server.py，并将原有 @mcp.tool() 的 root_action 函数体替换为使用 adapter：

\`\`\`python
from r2_root_socket_client import root_action_via_socket

@mcp.tool()
def root_action(action: Literal["probe","install_prereqs","prepare_dirs","relay_status"]) -> dict:
    return root_action_via_socket(action)
\`\`\`

注册已有四个工具必须保持不变，不要创建第二个 FastMCP 实例。验证 py_compile 后**只重启 r2-mcp.service**，在本机 MCP tools/list 出现五个工具并能执行 probe 后，再按需单独重启 r2-mcp-tunnel.service。检查 /readyz 为 200 才刷新 ChatGPT 工具。

当 Socket 已验证可用，将 r2-mcp.service 的旧 NoNewPrivileges=no 临时 override 撤销；确认实际 MCP 进程 /proc/PID/status NoNewPrivs 为 1（这是安全目标），重新通过 MCP 测试 probe 成功后，移除过去为 sudo 提权创建的 /etc/sudoers.d/r2-mcp。

## 回滚

\`\`\`bash
systemctl disable --now r2-rootd.service
# 从本地备份恢复 /opt/r2-mcp/server.py，再分别重启 MCP / Tunnel
\`\`\`

切勿停止 sing-box、SSH 或改动 443。rootd 对 apt 包安装具有较高权限，只有明确授权且经过代码审查后才能运行 install_prereqs。该工具仍未提供主程序部署或 Caddy 配置权限。
