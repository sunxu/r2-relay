# 真实 Cloudflare R2 GET 验收（手动、显式启用）

本文档用于在 VPS 下载服务部署并启用后，验证一条真实签名 R2 GET 是否能经 Mac RelayClient → VPS → Cloudflare R2 完整取回。CI 不执行本流程，不需要把真实 R2 凭据放入 GitHub。

## 1. 前置条件

- VPS 已部署包含 PR #66 的版本，且已按实际磁盘容量配置并启用 VPS `[download]`。
- Mac 已安装当前 Stage 2 代码；本地下载配置已启用。
- Mac → VPS HTTPS 证书验证正常；自签发证书通过 `R2_RELAY_VPS_CA_FILE` 指定。
- 准备一条新鲜、未过期、实际可用的 R2 GET 签名 URL，以及该请求必须携带的原始请求头。
- 准备已知的响应状态码、响应字节数和 SHA-256。若测试 Range，expected_size/hash 必须对应 Range 响应体，而不是整个对象。

**不要把签名 URL、Authorization、X-Amz-Signature 或其他临时凭据提交到仓库、Issue、PR 或 CI 日志。**

## 2. 准备本地资源配置

下载功能默认关闭。只有在确认 Mac 临时磁盘和内存预算后，才在 `~/.config/r2-relay/config.toml` 设置：

```toml
[download]
enabled = true
max_concurrency = 16
global_max_concurrency = 32
small_response_memory_mib = 32
max_temp_mib = 4096
temp_dir = "~/.cache/r2-relay/downloads"
```

以上 32 MiB 内存和 4096 MiB 磁盘只是示例预算，必须按实际机器资源调整。VPS 端也必须独立配置磁盘预算、剩余空间保护、活动任务上限和等待者上限。

## 3. 创建私有请求描述文件

在 Mac 本地创建文件，权限必须为 0600：

```bash
mkdir -p ~/.config/r2-relay/private
cat > ~/.config/r2-relay/private/real-get-spec.json <<'JSON'
{
  "url": "https://ACCOUNT_ID.r2.cloudflarestorage.com/BUCKET/OBJECT?SIGNED_QUERY",
  "headers": [
    ["Range", "bytes=0-26214399"],
    ["Authorization", "替换为该请求的真实 Authorization"],
    ["x-amz-date", "替换为该请求的真实签名时间"],
    ["x-amz-content-sha256", "替换为该请求的真实签名摘要"]
  ],
  "expected_status_code": 206,
  "expected_size": 26214400,
  "expected_sha256": "替换为响应体的64位SHA256"
}
JSON
chmod 600 ~/.config/r2-relay/private/real-get-spec.json
```

上面内容仅为字段结构示例，不是可直接使用的签名请求。必须从实际成功的 R2 GET 请求复制完整 URL 和所有必要签名头；不要自行修改 Range、URL 编码、查询参数或请求头大小写/值。

如果请求使用预签名 URL 且不需要 Authorization 头，`headers` 应只包含该请求实际需要的头。不要把无关或已经过期的签名头添加进去。

## 4. 执行验收

```bash
export R2_RELAY_VPS_URL="https://你的VPS地址:8443"
export R2_RELAY_TOKEN="你的Relay Token"
export R2_RELAY_VPS_CA_FILE="$HOME/.config/r2-relay/vps-ca.pem"
export R2_RELAY_CONFIG="$HOME/.config/r2-relay/config.toml"
export R2_RELAY_REAL_GET_SPEC="$HOME/.config/r2-relay/private/real-get-spec.json"
export R2_RELAY_HTTP2=1

PYTHONPATH=local python local/poc/real_r2_download_acceptance.py
```

脚本会：
- 读取权限为 0600 的请求描述文件。
- 通过 VPS 下载 API 发起一次真实 R2 GET。
- 取回小响应或 READY Parts，并执行长度与 SHA-256 校验。
- 比对预期 HTTP 状态码、响应长度和 SHA-256。
- 输出 `PASS_REAL` 或 `FAIL_REAL`，同时记录 Mac → VPS 实际协商的 HTTP 版本。

脚本不会输出或记录签名 URL、请求头、Token 或底层异常文本。

## 5. 验收判定

只有以下条件全部成立才算 `PASS_REAL`：

1. R2 返回状态码与预期一致（例如 Range GET 为 206）。
2. 实际响应体长度与预期一致。
3. 响应体 SHA-256 与独立计算的预期值一致。
4. Mac → VPS 下载请求正常完成；若要求 HTTP/2，报告中的版本必须包含 `HTTP/2`。
5. VPS 下载任务成功清理；本地临时文件和资源预算在响应消费完成后释放。

此脚本直接验证 RelayClient → VPS → 真实 R2，不经过 mitmproxy 的原始客户端接管层。mitmproxy GET 接管、Loopback response streaming 和不提前连接原始 R2 主机，已由隔离 Mock R2 的 Runner 全栈测试覆盖；如需对真实业务应用做最终验收，还必须让该应用经本地 18585 代理发起同一类真实 GET。

## 6. 安全收尾

完成后关闭下载功能或恢复原配置，并删除私有请求文件：

```bash
rm -f ~/.config/r2-relay/private/real-get-spec.json
```

如果签名 URL 或 Authorization 曾被复制到不可信位置，应按对应 R2 凭据管理流程轮换密钥或重新生成签名。
