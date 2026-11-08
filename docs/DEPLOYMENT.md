# R2 Relay 部署说明（预备材料，当前未部署）

RC1 离线代码已合并到 `main`（PR #15）；**本文件仍只是部署预备材料**，不代表真实 VPS/R2 已验证。本地代理默认 `127.0.0.1:18585`，VPS HTTPS `8443`（443 已被占用）；只部署经过审核的正式 Commit，生产执行必须另行授权。

## 1. VPS Python API

Linux 上配置专用服务账户、缓存目录和 Python 虚拟环境。以下命令是供运维审核的示例，不会自动执行：

```bash
sudo useradd --system --home /var/lib/r2-relay --shell /usr/sbin/nologin r2relay
sudo install -d -o r2relay -g r2relay -m 0700 /var/lib/r2-relay/cache
sudo install -d -o root -g root -m 0700 /etc/r2-relay
sudo install -d -o root -g root -m 0755 /opt/r2-relay
# 将已审查代码放到 /opt/r2-relay
python3.11 -m venv /opt/r2-relay/.venv
/opt/r2-relay/.venv/bin/pip install -r /opt/r2-relay/requirements/rc1-direct.txt
/opt/r2-relay/.venv/bin/pip install -e /opt/r2-relay/vps
# 将 deploy/vps.env.example 复制为 /etc/r2-relay/vps.env，填写随机 token
sudo chown root:root /etc/r2-relay/vps.env
sudo chmod 0600 /etc/r2-relay/vps.env
sudo cp deploy/vps.service /etc/systemd/system/r2-relay-vps.service
sudo systemctl daemon-reload
sudo systemctl enable --now r2-relay-vps
```

Systemd 只监听 `127.0.0.1:18085`，不能把明文 HTTP 18085 暴露公网。

## 2. VPS HTTPS 8443

`deploy/Caddyfile.example` 在 8443 接收 HTTPS/2，并反代 loopback API。配置 `R2_RELAY_HOST`、`R2_RELAY_TLS_CERT`、`R2_RELAY_TLS_KEY`，证书必须匹配主机名；使用现有已签发证书，不依赖占用中的 443 或 HTTP-01。安装和启用 Caddy 的操作应与 VPS 现有配置隔离，不能覆盖正在运行的 443 服务。

Caddy 对外仅暴露 `8443`，务必由防火墙限制预期来源；API 内部端口 `18085` 仅回环绑定。验证 `curl --fail --cacert <CA> https://HOST:8443/healthz`，再测试认证拒绝和 Prepare/MISS/HIT。不要开启 TLS 验证绕过。

## 3. 本地 mitmproxy

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e './local[test]' -e './vps[test]'
# 设置 R2_RELAY_VPS_URL、R2_RELAY_TOKEN，必要时 R2_RELAY_VPS_CA_FILE
python -m r2relay_local
```

原 aiohttp 程序自行将原有 CA 与 mitmproxy CA 合并，并通过 `proxy="http://127.0.0.1:18585"` 连接。代理 CA 私钥、VPS token、R2 预签名 URL 属于敏感运行数据，不得提交 GitHub。按现有确认，受控运行日志可显示完整字段。

## 4. 试验环境验收

当前无 VPS 和 R2 时运行：
```bash
python -m pip install -r requirements/rc1-direct.txt
python -m pip install -e ./vps -e ./local
python scripts/rc1_offline.py
```

`local/tests/test_real_fullstack.py` 启动真实 mitmdump 和 TLS VPS（回环），注入 httpx MockTransport 取代外部 R2；测试 MISS/HIT、重试、实际 ETag、缓存唯一性。Runner 实际 Caddy/Mock R2 全栈验证可参见 [RUNNER_FULLSTACK_CN.md](RUNNER_FULLSTACK_CN.md)。完整外网验证、真实 Cloudflare R2 ETag 和性能报告必须等环境可用后再做。

## 5. P0 部署前安全检查

- 将 VPS Token 视为高敏感共享密钥；用密码学安全随机源生成，绝不在 shell history、CI 公开日志、Issue 或 PR 中输出；环境文件权限 `root:root 0600`。systemd 作为 `r2relay` 账户运行，**服务账户不能读取 root-only EnvironmentFile 本身，systemd 管理器负责加载环境**。
- VPS 凭据必须开启 TLS 验证；不要为“测试方便”启用 `ssl_insecure` 或 `verify=False`。
- 只通过 Caddy 暴露 `8443`，不放行 `18085`；按最小来源 IP 开放防火墙。
- 将 `R2_RELAY_MAX_PART_BYTES=33554432` 设置为与本地缓冲的 32 MiB 单片限制一致；超限 Prepare 明确返回 413，不创建任务。
- `R2_RELAY_CACHE_DIR` 所在文件系统应配置磁盘容量告警、配额与清理；`R2_RELAY_MIN_FREE_BYTES` 只是剩余空间保护，**不是缓存总容量配额**。
- Caddy 证书路径必须由 Caddy 运行账户可读；不得改变现有 `443` 监听。
- 生产前复核 systemd 单元与 Caddy 实际加载配置：`systemd-analyze verify`、`caddy validate`；目前只完成 Runner 模拟，不表示真实 VPS 验收通过。

## 6. 纯离线预部署检查

参见 [离线预部署检查与 SAFE 报告](DEPLOYMENT_PREFLIGHT_CN.md)。该检查不需 VPS/R2，可以在 GitHub Runner 校验 systemd/Caddy 语法、环境变量约束与权限策略，但不代表真实 VPS 已部署。

## 7. 回滚

停止本地代理：原 aiohttp 移除显式代理配置，即恢复既有上传路径。VPS 不可用时本地按约定只在明确连接失败时直传。关闭 VPS 服务使用 `systemctl stop r2-relay-vps`，不删除运行中的其他服务或端口配置。对正在执行的 Multipart 不保证无中断迁移。
