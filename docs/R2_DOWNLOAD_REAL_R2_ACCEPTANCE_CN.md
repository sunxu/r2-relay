# 真实 Cloudflare R2 GET 验收（手动、显式启用）

本文档用于在 VPS 下载服务部署并启用后，验证一条真实签名 R2 GET 是否能经 Mac RelayClient → VPS → Cloudflare R2 完整取回。CI 不执行本流程，不需要把真实 R2 凭据放入 GitHub。

## 1. 前置条件

- VPS 已部署包含 PR #66 的版本，且已按实际磁盘容量配置并启用 VPS `[download]`。
- Mac 已安装当前 Stage 2 代码；本地下载配置已启用。
- Mac → VPS HTTPS 证书验证正常；自签发证书通过 `R2_RELAY_VPS_CA_FILE` 指定。
- 准备一条新鲜、未过期、实际可用的 R2 GET 签名 URL，以及该请求必须携带的原始请求头。
- 准备已知的响应状态码、响应字节数和 SHA-256。若测试 Range，expected_size/hash 必须对应 Range 响应体，而不是整个对象。

**不要把签名 URL、Authorization、X-Amz-Signature 或其他临时凭据提交到仓库、Issue、PR 或 CI 日志。**

## 1.1 准备一个真实的 25 MiB R2 测试对象

推荐使用专用测试 bucket 或现有 bucket 下的临时测试前缀，不要选生产对象。25 MiB 大于 24 MiB 阈值，预期 VPS 生成两个 Part：16 MiB + 9 MiB。

以下示例使用 AWS CLI 的独立 profile（例如 `r2-test`）。先运行 `aws configure --profile r2-test`，按提示输入 R2 Access Key ID 和 Secret Access Key；再运行 `aws configure set region auto --profile r2-test`。该 profile 应只拥有测试 bucket 的对象读写权限，凭据保存在本机 `~/.aws/credentials`，不要放进仓库。Cloudflare R2 的 S3 endpoint 使用 `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`，签名 URL 可通过 AWS CLI `s3 presign` 生成。签名位于 URL 查询参数中，预签名 GET 通常不需要额外 Authorization 请求头。参考：[Cloudflare R2 预签名 URL 文档](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)、[R2 S3 API 文档](https://developers.cloudflare.com/r2/get-started/s3/)。

在 Mac 终端执行：

```bash
set +x
export AWS_PROFILE="r2-test"
export R2_ACCOUNT_ID="你的 Cloudflare Account ID"
export R2_BUCKET="你的测试 Bucket"
export R2_KEY="r2-relay-acceptance/real-get-25mib.bin"
export R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export SOURCE="$HOME/r2-relay-25mib.bin"
export BASELINE="$HOME/r2-relay-25mib-direct.bin"
export BASE_HEADERS="$HOME/r2-relay-25mib-direct.headers"

# 生成恰好 25 MiB 的随机测试文件
python3 - "$SOURCE" <<'PY'
import os, sys
with open(sys.argv[1], "wb") as f:
    for _ in range(25):
        f.write(os.urandom(1024 * 1024))
PY

# 上传测试对象到 R2
aws --profile "$AWS_PROFILE" --region auto s3 cp \
  "$SOURCE" "s3://$R2_BUCKET/$R2_KEY" \
  --endpoint-url "$R2_ENDPOINT"

# 生成 1 小时有效的 GET 预签名 URL，不要 echo 或粘贴到聊天
export SIGNED_URL="$(
  aws --profile "$AWS_PROFILE" --region auto s3 presign \
    "s3://$R2_BUCKET/$R2_KEY" \
    --expires-in 3600 --endpoint-url "$R2_ENDPOINT"
)"

# 先绕过 Relay 直接访问 R2，建立可信基线；Range 覆盖完整 25 MiB 对象
curl --silent --show-error --fail \
  --range 0-26214399 \
  --dump-header "$BASE_HEADERS" \
  --output "$BASELINE" \
  "$SIGNED_URL"

STATUS="$(awk 'index($1, "HTTP") == 1 {code=$2} END {print code}' "$BASE_HEADERS")"
test "$STATUS" = "206"
grep -qi '^Content-Range: bytes 0-26214399/26214400' "$BASE_HEADERS"
grep -qi '^Content-Length: 26214400' "$BASE_HEADERS"
cmp -s "$SOURCE" "$BASELINE"
echo "Direct R2 baseline: PASS (HTTP 206, Content-Range/Length, bytes identical)"
```

如果直接 R2 基线不是 HTTP 206，或 `cmp` 失败，先不要测 Relay；先排查 URL、权限、对象和 Range 行为。

## 1.2 创建本地私有验收 spec

继续在同一个终端执行。命令不会打印签名 URL：

```bash
export SPEC="$HOME/.config/r2-relay/private/real-get-spec.json"
mkdir -p "$(dirname "$SPEC")"
umask 077

python3 - "$SOURCE" "$SPEC" "$BASE_HEADERS" <<'PY'
import hashlib, json, os, sys
source, target, headers_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(source, "rb") as f:
    digest = hashlib.file_digest(f, "sha256").hexdigest()
response_headers = {}
for line in open(headers_path, encoding="latin-1"):
    if ":" in line and not line.startswith("HTTP/"):
        name, value = line.split(":", 1)
        response_headers[name.strip().lower()] = value.strip()
expected_headers = {
    "content-range": "bytes 0-26214399/26214400",
    "content-length": "26214400",
}
if "etag" in response_headers:
    expected_headers["etag"] = response_headers["etag"]
data = {
    "url": os.environ["SIGNED_URL"],
    "headers": [["Range", "bytes=0-26214399"]],
    "expected_status_code": 206,
    "expected_size": 25 * 1024 * 1024,
    "expected_sha256": digest,
    "expected_headers": expected_headers,
}
with open(target, "w", encoding="utf-8") as f:
    json.dump(data, f)
os.chmod(target, 0o600)
PY

unset SIGNED_URL
ls -l "$SPEC"
```

权限必须显示为仅文件所有者可读写（`-rw-------`）。预签名 URL 已经包含授权信息，因此这个示例不添加 `Authorization`、`x-amz-date` 等额外请求头；如果你测试的是 Header 签名请求，才把实际签名所需的请求头放进 spec。不要改动已签名 URL 的查询参数或编码。

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

脚本不会输出或记录签名 URL、请求头、Token 或底层异常文本。`PASS_REAL` 强制要求 Mac → VPS 实际协商 HTTP/2。

## 4.1 再经真实 mitmproxy 18585 走一次完整客户端链路

上面的验收脚本直接调用 Mac RelayClient → VPS → R2，**不经过 mitmproxy**。Stage 2 的 mitmproxy 接管目前由 Mock R2 全栈 CI 验证。要把真实 R2 也走过本地代理，再做下面这项人工验收。

先确认 Mac 的 `~/.config/r2-relay/config.toml` 已启用下载，并按本机真实资源填写预算，例如：

```toml
[download]
enabled = true
max_concurrency = 16
global_max_concurrency = 32
small_response_memory_mib = 32
max_temp_mib = 4096
temp_dir = "~/.cache/r2-relay/downloads"
```

这里的 4096 MiB 只是示例，不能在未检查 Mac 磁盘空间时照抄。VPS 的 `/etc/r2-relay/config.toml` 也必须配置并启用独立的 `[download]` 资源预算。

在已安装当前 Stage 2 分支的仓库根目录，打开一个终端启动本地代理：

```bash
export R2_RELAY_VPS_URL="https://你的VPS地址:8443"
export R2_RELAY_TOKEN="你的Relay Token"
export R2_RELAY_VPS_CA_FILE="$HOME/.config/r2-relay/vps-ca.pem"
export R2_RELAY_CONFIG="$HOME/.config/r2-relay/config.toml"
export R2_RELAY_HTTP2=1
python -m r2relay_local --listen-host 127.0.0.1 --listen-port 18585
```

首次启动后，mitmproxy 会生成本地客户端 CA 公共证书：`~/.mitmproxy/mitmproxy-ca-cert.pem`。不要分享或提交 CA 私钥。

在另一个终端中恢复签名 URL 到当前 shell（不打印它），再经 18585 发起相同 Range GET：

```bash
export SOURCE="$HOME/r2-relay-25mib.bin"
export SPEC="$HOME/.config/r2-relay/private/real-get-spec.json"
export SIGNED_URL="$(python3 - "$SPEC" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["url"])
PY
)"
curl --silent --show-error --fail \
  --proxy http://127.0.0.1:18585 \
  --cacert "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" \
  --range 0-26214399 \
  --dump-header "$HOME/r2-relay-25mib-proxy.headers" \
  --output "$HOME/r2-relay-25mib-proxy.bin" \
  "$SIGNED_URL"
unset SIGNED_URL

STATUS="$(awk 'index($1, "HTTP") == 1 {code=$2} END {print code}' "$HOME/r2-relay-25mib-proxy.headers")"
test "$STATUS" = "206"
grep -qi '^Content-Range: bytes 0-26214399/26214400' "$HOME/r2-relay-25mib-proxy.headers"
grep -qi '^Content-Length: 26214400' "$HOME/r2-relay-25mib-proxy.headers"
cmp -s "$SOURCE" "$HOME/r2-relay-25mib-proxy.bin"
echo "mitmproxy real R2 GET: PASS (HTTP 206, Content-Range/Length, bytes identical)"
```

这一步同时验证：原始客户端 CONNECT → mitmproxy addon 接管 → VPS 下载 API → Cloudflare R2 → Mac Part 组装 → Loopback response streaming → 原始客户端。若代理返回 502，不要改为绕过代理直连来宣称通过；保留状态码和脱敏日志，定位接管或 VPS 错误。

## 5. 验收判定

只有以下条件全部成立才算 `PASS_REAL`：

1. R2 返回状态码与预期一致（例如 Range GET 为 206）。
2. 实际响应体长度与预期一致。
3. 响应体 SHA-256 与独立计算的预期值一致。
4. Mac → VPS 下载请求正常完成，报告中的 `http2_ok` 必须为 true，协商版本必须包含 `HTTP/2`。
5. VPS 下载任务 DELETE 成功（或已不存在）；报告中的 `vps_task_cleanup_ok` 和 `local_temp_cleanup_ok` 均为 true；本地临时文件和内存/磁盘预算在响应消费完成后释放。

此脚本直接验证 RelayClient → VPS → 真实 R2，不经过 mitmproxy 的原始客户端接管层。mitmproxy GET 接管、Loopback response streaming 和不提前连接原始 R2 主机，已由隔离 Mock R2 的 Runner 全栈测试覆盖；如需对真实业务应用做最终验收，还必须让该应用经本地 18585 代理发起同一类真实 GET。

## 6. 安全收尾

完成后关闭下载功能或恢复原配置，并删除私有请求文件及本地基线文件：

```bash
rm -f ~/.config/r2-relay/private/real-get-spec.json \
  "$HOME/r2-relay-25mib.bin" "$HOME/r2-relay-25mib-direct.bin" \
  "$HOME/r2-relay-25mib-direct.headers"
```

如果签名 URL 或 Authorization 曾被复制到不可信位置，应按对应 R2 凭据管理流程轮换密钥或重新生成签名。
