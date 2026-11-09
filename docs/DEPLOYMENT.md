# R2 Relay 部署与运行手册

最后核对：2026-11-09。本文区分当前真实 VPS 部署状态、客户端本地配置和仍待真实 R2 环境验证的项目。

## 1. 当前部署状态

截至 2026-11-09，以下状态已通过 ChatGPT bandwagonhost MCP 只读检查：

- 当前应用 Release：`eafa6ed358eca98b9d3b91d71565ffae74914770`。
- 统一检查：`UNIFIED_DEPLOYMENT_STATUS_PASS`。
- R2 Relay API：`r2-relay-vps.service` active，Uvicorn 只监听 `127.0.0.1:18085`。
- Caddy：`caddy.service` active，对外提供 HTTPS `:8443`。
- sing-box 原有 `:443` 仍在监听，本项目不接管或重启它。
- MCP 与 Secure Tunnel：`r2-mcp.service`、`r2-mcp-tunnel.service` active。
- EnvironmentFile：`/etc/r2-relay/vps.env`，root:root、0600；本手册不记录其内容。
- Cache：`/var/lib/r2-relay/cache`，r2relay 专用目录，0700。
- 18085 只允许 loopback；不得直接暴露公网。
- 本机 Runtime Smoke：`RUNTIME_SMOKE_PASS`（health 200、metrics 未授权 401、授权 200）；使用临时假 Token，不访问真实 R2。

以上是部署与服务状态证据，不等于真实 Cloudflare R2 PUT、ETag、Multipart Complete 或 VPS→R2 性能验收通过。真实 R2 项目由 [Issue #16](https://github.com/sunxu/r2-relay/issues/16) 跟踪。

## 2. VPS 当前架构

```text
本地 Python / aiohttp 客户端
  └─ HTTP CONNECT → 127.0.0.1:18585 (mitmdump)
       └─ TLS MITM 仅限 *.r2.cloudflarestorage.com
            └─ HTTPS :8443 (Caddy，自签名 IP SAN 证书)
                 └─ 127.0.0.1:18085 (R2 Relay FastAPI)
                      └─ /var/lib/r2-relay/cache
                      └─ R2 UploadPart Worker（真实 R2 尚待验收）
```

`:443` 由既有 sing-box 使用。禁止将 Caddy 绑定到 443、80，禁止将 API 18085 暴露公网。公网 8443 的服务端证书为 VPS 自建私有 CA 签发的 IP SAN 证书；客户端必须使用已通过可信通道取得并核对指纹的 CA 公钥证书验证链与 IP SAN。不得使用 `curl -k`、`verify=False` 或 `ssl_insecure=true`。

证书及权限设计见 [双栈 IP 自签名 HTTPS 部署记录](R2_HTTPS_IP_MCP_DEPLOY_CN.md)。CA 私钥留在 VPS；客户端只需要 CA 公钥证书。证书轮换前需更新客户端信任锚并重新验证指纹。

## 3. 本地代理安装（macOS / Linux）

要求 Python 3.11+。

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e './local[test]'
```

在启动代理的同一个终端中配置：

```bash
export R2_RELAY_VPS_URL='https://YOUR_VPS_IP:8443'
export R2_RELAY_TOKEN='从受控秘密分发渠道取得的客户端 Token'
export R2_RELAY_VPS_CA_FILE='/absolute/path/to/r2-vps-root-ca.pem'
export R2_RELAY_BUFFER_BUDGET=536870912
python -m r2relay_local --listen-host 127.0.0.1 --listen-port 18585
```

不要将真实 Token 写入 Git、聊天、shell 命令历史、CI 日志或工单。推荐使用本机密码管理器/受保护环境注入方式；不要将真实值填入仓库中的示例文件。

## 4. 本地代理安装（Windows PowerShell）

要求安装 Python 3.11+，在项目根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.\local[test]'
```

在同一 PowerShell 会话配置环境变量（下列值均为占位符）：

```powershell
$env:R2_RELAY_VPS_URL = 'https://YOUR_VPS_IP:8443'
$env:R2_RELAY_TOKEN = '<从受控秘密分发渠道取得>'
$env:R2_RELAY_VPS_CA_FILE = 'C:\Users\<用户>\certs\r2-vps-root-ca.pem'
$env:R2_RELAY_BUFFER_BUDGET = '536870912'
python -m r2relay_local --listen-host 127.0.0.1 --listen-port 18585
```

Windows PowerShell 当前会话关闭后，环境变量不会自动保留。不要使用 `setx` 将秘密写入长期用户环境；应通过受保护的秘密管理方式注入。

## 5. 客户端 CA 信任配置

首次启动 mitmproxy 后会在用户目录生成代理 CA。常见路径：

- macOS/Linux：`~/.mitmproxy/mitmproxy-ca-cert.pem`
- Windows：`%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem`

只分发 `mitmproxy-ca-cert.pem` 公共证书；绝不分发 `mitmproxy-ca.pem` 私钥文件。

调用方必须在原有系统/应用 CA 信任基础上追加代理 CA，并继续验证 TLS。aiohttp 示例：

```python
import ssl
import certifi
import aiohttp

ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl_context.load_verify_locations(cafile="/path/to/mitmproxy-ca-cert.pem")

connector = aiohttp.TCPConnector(ssl=ssl_context)
session = aiohttp.ClientSession(
    connector=connector,
    trust_env=False,
)
# 发请求时显式指定 proxy="http://127.0.0.1:18585"
```

Windows 将 CA 文件路径替换为 Windows 路径。若应用原本使用自定义 CA bundle，应以该 bundle 为基础追加 mitmproxy CA，不要用代理 CA 覆盖原有信任根。VPS 的自签名 CA 与 mitmproxy CA 是两种不同用途的信任锚：前者由 RelayClient 的 `R2_RELAY_VPS_CA_FILE` 使用，后者由本地 aiohttp 客户端用于代理 TLS MITM。

## 6. 本地代理行为与限制

- 默认监听 `127.0.0.1:18585`，仅对 `*.r2.cloudflarestorage.com` CONNECT 进行 TLS MITM；其他 HTTPS 作为普通隧道。
- 只识别 HTTPS PUT UploadPart（需 `uploadId` 和 `partNumber`）。
- 已知 Content-Length 且不超过 32 MiB、全局预算允许时完整缓冲并计算 MD5；未知长度、超限或超预算流量走普通流式路径。
- 默认 HTTP/2 连接 VPS；`R2_RELAY_HTTP2=0` 可切换 HTTP/1.1 对照，修改后需重启本地代理。
- VPS TCP 连接建立前失败可按既定逻辑回退；已提交任务后状态不确定时不发起竞争性直传。
- R2 业务错误应透传，不伪造成功。真实 R2 错误与 Multipart 行为仍需 Issue #16 的独立环境验收。

## 7. 运行、诊断与回滚

- 本地停止：在运行代理的终端按 Ctrl+C；调用方移除显式代理设置即可恢复原上传路径。
- VPS 状态：通过 ChatGPT 的 `vps_r2_deployment_status` 只读检查。旧第三方 `deployment_inspect` 为 Legacy，不作为生产验收依据。
- 不要手工修改 `/etc/r2-relay/vps.env` 权限或内容；不要将其复制到项目目录。
- 生产部署由固定 SHA 的受控 GitHub Actions / Root Receiver 流程管理；部署前检查、备份和回滚回执见 [GitHub Actions VPS 部署说明](GITHUB_ACTIONS_DEPLOY_CN.md)。
- 发生异常时先保存脱敏状态与日志，再按已审核部署流程回滚；不要通过任意 root shell、重启 sing-box 或改动 443 进行排障。

## 8. 测试状态与明确未验收项

- Runner 离线全栈与 Mock R2：`PASS_SIMULATED`，见 [RUNNER_FULLSTACK_CN.md](RUNNER_FULLSTACK_CN.md)。
- Mac→VPS 短时网络吞吐基线：已记录，见 [R17 报告](R17_EXISTING_NETWORK_EVIDENCE_CN.md)；不代表长期稳定性或 VPS→R2 速度。
- 真实 Cloudflare R2 SigV4、UploadPart、真实 ETag、CompleteMultipartUpload、真实 R2 故障重试：`BLOCKED_REAL_R2`，见 [Issue #16](https://github.com/sunxu/r2-relay/issues/16)。
- 不宣称 90% 持续上行利用率、长期稳定性、资源峰值或未执行的并发矩阵已通过。
