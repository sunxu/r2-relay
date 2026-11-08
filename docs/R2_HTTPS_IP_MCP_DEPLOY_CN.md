# HTTPS 8443 双栈 IP 自签名 CA 受控部署

固定公网 IPv4 `104.224.168.192` 和 IPv6 `2607:8700:5500:a47f::2`。Caddy `tls internal` 使用 VPS 本地 CA 为 IP 签发含 IP SAN 的服务器证书（CA root 是自签名）。不会申请域名，不接管 sing-box 443。

rootd/MCP 固定动作，**无用户输入路径或命令**：
- `vps_r2_https_preflight`：无写入检查 loopback API 已 active、443 监听、8443 空闲、未部署 Caddy。
- `vps_r2_https_prepare`：下载 Debian apt 已索引的 caddy .deb，仅用 dpkg-deb -x 解包其程序，不执行安装包 maintainer scripts；创建隔离的 r2caddy 用户、专用 Caddyfile 和 systemd unit；配置验证，不启动、不 enable。
- `vps_r2_https_start`：只启动 caddy.service；用显式信任 VPS Caddy root.crt 的 TLS 客户端，分别核验 IPv4 与 IPv6 IP SAN，检查 443 保持、8443 监听；出错停止本次启动的服务。
- `vps_r2_https_ca_fingerprint`：只返回 root CA 公钥证书路径和 SHA256 摘要，不回传私钥和 PEM。

客户端通过可信 SSH/SFTP 从 VPS 安全获取 `/var/lib/r2-caddy/data/caddy/pki/authorities/local/root.crt`（公开证书），独立比对 MCP 报告的 SHA256；安装为 Mac 单独信任锚或为 httpx/curl 显式指定。绝不使用 `verify=False` 或 `curl -k` 作正式验证，绝不传输 `root.key`。

## 固定 SHA Bootstrap
合并 PR 并核对 CI 后，root SSH 执行：

```bash
set -euo pipefail
COMMIT=REPLACE_WITH_MERGED_SHA
curl -fsSL "https://raw.githubusercontent.com/sunxu/r2-relay/$COMMIT/ops/bootstrap_r2_https_ip.py" -o /root/bootstrap_r2_https_ip.py
python3 /root/bootstrap_r2_https_ip.py --commit "$COMMIT"
python3 /root/bootstrap_r2_https_ip.py --commit "$COMMIT" --apply
```

Bootstrap 仅更新固定 3 个 root-owned 文件，创建独立备份，重启 rootd 和 MCP，不操作 443 或 Caddy。升级后刷新 ChatGPT 插件目录。按 preflight → prepare → start → fingerprint 顺序执行。

## 尚未覆盖
本机 loopback TLS 通过不等于公网 IPv4/IPv6 双栈已连通；需从 Mac 外部验证公网 8443、运营商防火墙和本地网络 IPv6 路由。客户端尚未获得 API Token；未执行真实 R2 上传；均不能宣称性能验收通过。
