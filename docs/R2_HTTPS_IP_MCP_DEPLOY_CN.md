# R2 Relay 自签名 HTTPS 8443：固定双公网 IP + 单张双 SAN 服务端证书

部署身份：
- IPv4：`https://104.224.168.192:8443`
- IPv6：`https://[2607:8700:5500:a47f::2]:8443`

**重要：** IPv4/IPv6 URL 的 TLS 握手通常不发送 SNI。不能依赖 Caddy 为每个 IP 自动签发独立证书，否则无 SNI 客户端可能收到另一个 IP 的证书而校验失败。本方案改为由 root 在 VPS 本地生成**一套自签名私有 CA + 一张同时包含 IPv4 和 IPv6 的 IP SAN 服务器证书**，Caddy 加载单张证书，不依赖 SNI。

## 证书与权限
- CA 私钥：`/etc/r2-relay/tls-ca/root.key`，root-only 0600，绝不导出；该目录位于原有 root-only `/etc/r2-relay` 内。
- 公共 CA 根证书：`/etc/r2-relay/tls-ca/root.crt`；可经可信 SSH 由 owner 取回，用于 Mac 的 httpx/curl 信任锚。通过 MCP 提供文件 SHA256，以独立通道比对。
- 服务器私钥：`/etc/r2-caddy/tls/server.key`，root:r2caddy 0640，TLS 目录 root:r2caddy 0750。
- 服务端公钥证书：`/etc/r2-caddy/tls/server.crt`，包含严格两条 IP SAN、serverAuth 扩展，1 年期，私有 CA 10 年期。到期前必须规划轮换。
- Caddyfile：`/etc/r2-caddy/Caddyfile`，root-owned 0644，父目录 root-owned 0755，无秘密；**绝不能**放在 root-only 0700 的 API Token 目录中，以免 r2caddy 无法读取。
- Caddy：`auto_https off`、`:8443`、固定 `tls server.crt server.key`、`reverse_proxy 127.0.0.1:18085`。不绑定 80/443，不修改 sing-box。

## MCP 固定动作（无自由输入）
1. `vps_r2_https_preflight`：只读核验 API 已在 18085 loopback 运行、443 在监听、8443 未被占用、Caddy 文件未安装。
2. `vps_r2_https_prepare`：只从系统 apt 软件源下载 Caddy deb 并以 dpkg-deb -x 解包固定二进制，不运行安装包 postinst；创建 r2caddy 用户、证书、Caddyfile、systemd unit，只做静态 `caddy validate`，不 start、enable。
3. `vps_r2_https_start`：确认准备状态后仅启动 caddy.service；使用 CA 公钥信任链及 IP SAN 核验两个身份，通过 loopback 的 TLS 8443；失败时尝试停止服务，不影响 443。
4. `vps_r2_https_ca_fingerprint`：只返回 CA 公钥证书 SHA256 和路径，不暴露证书私钥或生产 Token。

## MCP 升级
GitHub PR 合并、CI 全部通过、人工确认固定 SHA 后，在 root SSH 一次性执行：

```bash
set -euo pipefail
COMMIT=REPLACE_WITH_MERGED_FULL_SHA
curl -fsSL "https://raw.githubusercontent.com/sunxu/r2-relay/$COMMIT/ops/bootstrap_r2_https_ip.py" -o /root/bootstrap_r2_https_ip.py
python3 /root/bootstrap_r2_https_ip.py --commit "$COMMIT"
python3 /root/bootstrap_r2_https_ip.py --commit "$COMMIT" --apply
```

更新 rootd / MCP 固定白名单，重启 rootd 和 MCP，不启动 Caddy；刷新 bandwagonhost 工具目录后通过 ChatGPT 操作。

## 客户端验证
安全地取回 CA 根证书文件，核对 SHA256，然后执行：

```bash
curl --cacert /path/to/r2-root.crt https://104.224.168.192:8443/healthz
curl --cacert /path/to/r2-root.crt 'https://[2607:8700:5500:a47f::2]:8443/healthz'
```

必须启用 TLS 证书链和 IP SAN 验证；禁止 `curl -k` / `verify=False`。从 VPS 自身对 loopback 的验证不能替代真正的外部 IPv4/IPv6 连通性测试。HTTPS /healthz=200 也不等于客户端 Token 已安全同步或真实 R2 上传成功。

既有 `deploy/Caddyfile.ip-selfsigned.example` 是早期单 IP `tls internal` 方案说明，不是此部署动作的运行配置。


## 当前部署状态（2026-11-09）

上述固定双栈 IP SAN 自签名 CA 方案已在 VPS 部署。只读状态核验确认 Caddy active、8443 监听、443 原有监听保留、API 18085 仅 loopback；统一状态入口返回 `UNIFIED_DEPLOYMENT_STATUS_PASS`。CA 公钥指纹工具返回 `CA_FINGERPRINT_READY`，SHA-256：`2a5fd6545942b88f3b3c13f9db4e3e04cfc9e251ad39282440ba948440a2e80b`。该动作仅输出指纹和公钥证书路径，不输出 CA PEM 或私钥。

**仍待完成：** Owner Mac/外部客户端通过可信通道取得 CA 公钥证书、独立核对指纹，并从 IPv4/IPv6 外部验证证书链、IP SAN、/healthz 与 metrics 鉴权。VPS 本机状态与监听不能替代外部验证。真实 R2 上传仍由 #16 负责。

## Owner Mac 外部验收（可重复脚本）

先经可信 SSH 通道将 VPS 的**公共 CA 证书**取到 Mac（只取 `root.crt`，不取 `root.key`）：

```bash
mkdir -p "$HOME/.config/r2-relay"
chmod 700 "$HOME/.config/r2-relay"
scp root@104.224.168.192:/etc/r2-relay/tls-ca/root.crt \
  "$HOME/.config/r2-relay/root.crt"
chmod 600 "$HOME/.config/r2-relay/root.crt"
```

核对 SHA-256 并执行脚本（脚本会再次核对指纹）：

```bash
shasum -a 256 "$HOME/.config/r2-relay/root.crt"
bash scripts/verify_external_https.sh "$HOME/.config/r2-relay/root.crt"
```

脚本使用 `curl --cacert` 严格验证 CA 链和 URL IP SAN，不使用 `-k`，并用 `--noproxy '*'` 测试 Mac 的直接公网路径。它分别检查 IPv4/IPv6 的 `/healthz=200`、未授权 `/v1/metrics=401`，以及使用客户端 Token 后授权 `/v1/metrics=200`。Token 通过环境变量传给脚本，再经 curl 配置 stdin 传递，不进入命令行参数或输出。若 Mac 的直接 IPv6 网络不可用，IPv6 检查会明确失败；不要将其改成跳过 TLS 或伪造 PASS。

脚本通过后，保留完整终端输出作为 #33 的外部验收证据。该脚本不执行真实 R2 请求。

## Owner Mac 外部验收（可重复脚本）

先经可信 SSH 通道将 VPS 的**公共 CA 证书**取到 Mac（只取 `root.crt`，不取 `root.key`）：

```bash
mkdir -p "$HOME/.config/r2-relay"
chmod 700 "$HOME/.config/r2-relay"
scp root@104.224.168.192:/etc/r2-relay/tls-ca/root.crt \
  "$HOME/.config/r2-relay/root.crt"
chmod 600 "$HOME/.config/r2-relay/root.crt"
```

核对 SHA-256 并执行脚本（脚本会再次核对指纹）：

```bash
shasum -a 256 "$HOME/.config/r2-relay/root.crt"
bash scripts/verify_external_https.sh "$HOME/.config/r2-relay/root.crt"
```

脚本使用 `curl --cacert` 严格验证 CA 链和 URL IP SAN，不使用 `-k`，并用 `--noproxy '*'` 测试 Mac 的直接公网路径。它分别检查 IPv4/IPv6 的 `/healthz=200` 与未授权 `/v1/metrics=401`，不会读取或发送生产 Token。若 Mac 的直接 IPv6 网络不可用，IPv6 检查会明确失败；不要将其改成跳过 TLS 或伪造 PASS。

脚本通过后，保留完整终端输出作为 #33 的外部验收证据。该脚本不执行真实 R2 请求。

在 Mac 终端交互式输入 Token（输入不回显），执行完整验收；不要把 Token 粘贴到 ChatGPT、Issue 或 PR：

```bash
IFS= read -r -s -p "R2 Relay client token: " R2_RELAY_TOKEN
printf '\\n'
export R2_RELAY_TOKEN
bash scripts/verify_external_https.sh "$HOME/.config/r2-relay/root.crt"
unset R2_RELAY_TOKEN
```
