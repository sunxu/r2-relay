# 离线预部署检查（不需要 VPS / Cloudflare R2）

RC1 主线新增 `scripts/deployment_preflight.py` 和 `deployment-preflight.yml`。

**本检查不连接公网 VPS、Cloudflare R2，不安装任何服务、不修改配置、不发送请求。** 它是配置检查，而非真实运行验收。

## 无凭据模式：本地和 GitHub Runner

```bash
python3 scripts/deployment_preflight.py --mode template --report /tmp/r2-preflight-safe.json
python3 -m pytest -q tests/test_deployment_preflight.py
```

检查对象包括 systemd 回环监听 `127.0.0.1:18085`、最小权限、唯一 Worker、缓存写入目录；Caddy `8443`、外部证书与回环反代；本地 `https://HOST:8443`、最大 32 MiB 分片、执行/内存容量等。模板占位 Token 是预期值，不视为真实部署凭据。

GitHub Actions 的 `Offline deployment preflight` 还以独立工具运行 `systemd-analyze verify` 和 Caddy 容器 `caddy validate`，采用 Runner 自签名测试证书，仅用于语法解析。自动生成不包含任何 Token 或证书私钥的 SAFE JSON artifact。Docker 拉取镜像仅是工具依赖，不表示连接 VPS/R2。

## 未来 VPS 可用后：只检查私有文件（仍不部署）

```bash
python3 scripts/deployment_preflight.py --mode live-files \
  --vps-env /path/to/private-vps.env \
  --local-env /path/to/private-local.env \
  --report /tmp/r2-preflight-safe.json
```

这个模式检查两份本地配置文件权限（不得开放给组/其他用户）、Token 是否匹配且非占位值、URL 与容量配置；它不校验运行账户、文件属主、真实 TLS 证书链或 systemd 是否已加载。**不打印 Token 内容，也不读取 TLS 私钥正文**。不要把真实凭据放进仓库、Issues、CI secrets 以外的日志或 SAFE 报告。

前置检查的通过仅代表指定的静态策略匹配；仍然不能证明 VPS 上安装路径、用户、systemd 权限、Caddy TLS 证书、真实 R2 SigV4 或网络吞吐已验证。后续依 [Issue #16](https://github.com/sunxu/r2-relay/issues/16) 与 [Issue #17](https://github.com/sunxu/r2-relay/issues/17) 完成。

## 状态定义

- `PASS`：被检查的模板或私有文件满足本脚本明确列出的静态规则。
- `FAIL`：存在不符合策略的配置项，脚本非零退出。
- `real_vps_tested=false`、`real_r2_tested=false`：真实服务始终未测试。
