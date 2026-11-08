# 通用 MCP 一次性 Bootstrap（Owner SSH）

此脚本将已经通过 GitHub CI 的通用工具模块安装到现有 r2-mcp.service。不新增公网端口，不修改 sing-box/443，沿用 Secure MCP Tunnel 与独立 root Socket。只需安装一次；后续新版 MCP 工具通过已有连接提供能力。

## 操作前确认
- 已审查 ops/bootstrap_generic_vps_mcp.py 与 ops/generic_vps_mcp.py；
- 使用同一个审核过的完整 Git Commit SHA 下载两个文件；
- 保留 SSH 访问，不改变现有 mcp-relay 账号和服务；
- 安装脚本会重启 r2-mcp.service，短暂中断 MCP；不自动重启 Tunnel；
- 脚本需要以 root SSH 执行。当前旧 MCP 根本不支持文件下载及代码安装，因此不能从现有 MCP 调用该脚本。

## 执行
在 root SSH 中运行，COMMIT 替换为两文件均已合并后的完整 SHA：

```bash
set -euo pipefail
COMMIT=REPLACE_WITH_APPROVED_FULL_SHA
BASE="https://raw.githubusercontent.com/sunxu/r2-relay/$COMMIT"
curl -fsSL "$BASE/ops/bootstrap_generic_vps_mcp.py" -o /root/bootstrap_generic_vps_mcp.py
curl -fsSL "$BASE/ops/generic_vps_mcp.py" -o /root/generic_vps_mcp.py
python3 /root/bootstrap_generic_vps_mcp.py --module-source /root/generic_vps_mcp.py
python3 /root/bootstrap_generic_vps_mcp.py --module-source /root/generic_vps_mcp.py --apply
systemctl is-active r2-mcp.service
systemctl show r2-mcp.service -p NoNewPrivileges
```

正常应返回 INSTALLED_MCP_RESTARTED。若 Tunnel 因 MCP restart 断联，在 MCP 本地确认端口 18765 后单独执行 systemctl restart r2-mcp-tunnel.service；不要重启 sing-box。

## 回滚
脚本默认备份 /var/backups/vps-mcp/server-pre-generic-v1.py，恢复示例：

```bash
cp -a /var/backups/vps-mcp/server-pre-generic-v1.py /opt/r2-mcp/server.py
rm -f /opt/r2-mcp/generic_vps_mcp.py
rm -f /etc/systemd/system/r2-mcp.service.d/generic-workspace.conf
systemctl daemon-reload
systemctl restart r2-mcp.service
```

不要删除 Stage 工作区中的用户数据。安装后必须通过 ChatGPT 新工具 vps_system_snapshot 验证；VPS 未验证之前不能称为部署成功。
