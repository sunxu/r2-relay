# R2 Relay 部署说明（预备材料，当前未部署）

本文件是部署物料，不代表真实 VPS/R2 已验证。本地代理默认 `127.0.0.1:18585`，VPS HTTPS `8443`（443 已被占用）；只部署经过审核的正式 Commit，生产执行必须另行授权。

## 1. VPS Python API

Linux 上配置专用服务账户、缓存目录和 Python 虚拟环境。以下命令是供运维审核的示例，不会自动执行：

```bash
sudo useradd --system --home /var/lib/r2-relay --shell /usr/sbin/nologin r2relay
sudo install -d -o r2relay -g r2relay -m 0700 /var/lib/r2-relay/cache
sudo install -d -o root -g root -m 0700 /etc/r2-relay
# 将已审查代码放到 /opt/r2-relay
python3.11 -m venv /opt/r2-relay/.venv
/opt/r2-relay/.venv/bin/pip install -e /opt/r2-relay/vps
# 将 deploy/vps.env.example 复制为 /etc/r2-relay/vps.env，填写随机 token
sudo chmod 0600 /etc/r2-relay/vps.env
sudo cp deploy/vps.service /etc/systemd/system/r2-relay-vps.service
sudo systemctl daemon-reload
sudo systemctl enable --now r2-relay-vps
```

Systemd 只监听 `127.0.0.1:18085`，不能把明文 HTTP 18085 暴露公网。

## 2. VPS HTTPS 8443

`deploy/Caddyfile.example` 在 8443 接收 HTTPS/2，并反代 loopback API。配置 `R2_RELAY_HOST`、`R2_RELAY_TLS_CERT`、`R2_RELAY_TLS_KEY`，证书必须匹配主机名；使用现有已签发证书，不依赖占用中的 443 或 HTTP-01。安装和启用 Caddy 的操作应与 VPS 现有配置隔离，不能覆盖正在运行的 443 服务。

Caddy 只监听 `8443`，防火墙仅允许预期来源。验证 `curl --fail --cacert <CA> https://HOST:8443/healthz`，再测试认证拒绝和 Prepare/MISS/HIT。不要开启 TLS 验证绕过。

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
python -m pip install -e './vps[test]' -e './local[test]'
cd vps && python -m pytest -q
cd ../local && python -m pytest -q
```

`local/tests/test_real_fullstack.py` 启动真实 mitmdump 和 TLS VPS（回环），注入 httpx MockTransport 取代外部 R2；测试 MISS/HIT、重试、实际 ETag、缓存唯一性。完整外网验证、真实 ETag 和性能报告必须等环境可用后再做。

## 5. 回滚

停止本地代理：原 aiohttp 移除显式代理配置，即恢复既有上传路径。VPS 不可用时本地按约定只在明确连接失败时直传。关闭 VPS 服务使用 `systemctl stop r2-relay-vps`，不删除运行中的其他服务或端口配置。对正在执行的 Multipart 不保证无中断迁移。
