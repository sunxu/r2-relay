# 从 iota-tools 私有日志筛选、导出 R2 GET 请求

本工具只读取 Mac 本地 iota-tools 下载日志，供 R2 Relay 隔离验收复用历史签名请求。**不联网、不下载、不更改 IOTA、VPS 或 R2 对象**，不需要安装任何第三方 Python 包。

工具：local/tools/extract_iota_r2_request.py（Python 3.11+ 标准库）。

## 输入与隐私边界

- r2-download-requests.jsonl.1（轮转日志，可选）及 r2-download-requests.jsonl：请求 schema 为 iota_r2_download_request_v1，包含**完整签名 URL 和原始请求头**。
- r2-download-events.jsonl（可选）：download_summary 事件，schema 为 iota_r2_download_event_v1，通过 request_id 关联。IOTA 事件历史只保留最近 1,000 条，所以旧请求缺少对应事件是正常现象。
- 默认目录：~/Library/Application Support/iota-tools；如实际运行目录不同，用 --dir 指定。
- 要求日志和运行目录不允许其他用户读取；拒绝符号链接输入。**控制台仅显示请求 ID、剩余分钟、长度、状态和排除原因，不显示 URL、签名、请求头或凭据。**
- 不要将 JSONL 原文件、导出的私有 JSON 或完整敏感日志粘贴到聊天、Issue 或提交到 Git。即使有 .gitignore，私有输入也必须保存在仓库之外。

## 1. 离线筛选

在 r2-relay 仓库根目录执行：

    python3 local/tools/extract_iota_r2_request.py \
      --dir "$HOME/Library/Application Support/iota-tools" \
      --min-ttl-minutes 30 --limit 20

输出两类候选：

- SAFE_CANDIDATES：历史事件记录为完整 HTTP 206、响应超过24 MiB，并且原始单一闭区间 Range 长度与响应相符。这里的 SAFE **仅表示离线筛选等级**，不保证当前 R2 可访问。
- REPLAY_CANDIDATES：SigV4 时间尚未过期、X-Amz-SignedHeaders=host、无条件请求头、无重复 Range；允许后续独立验证前改变未签名的 Range。即使缺少 download_summary 也可入选。

EXPIRED 依据本机时钟和 X-Amz-Date + X-Amz-Expires 计算。签名时间未过期不等于仍能使用：临时会话凭据可能提前失效，R2 对象可能已经删除。session_token=YES 只表示请求 URL 中有会话 Token，不代表其仍有效。

## 2. 导出私有请求（不会覆盖）

    umask 077
    mkdir -p "$HOME/.config/r2-relay/private"
    chmod 700 "$HOME/.config/r2-relay/private"

    python3 local/tools/extract_iota_r2_request.py \
      --dir "$HOME/Library/Application Support/iota-tools" \
      --select '<列表中的16位request_id>' \
      --out "$HOME/.config/r2-relay/private/r2-selected-get.json"

导出文件只含 method、url、headers，使用独占创建写入并设置权限 0600；如果目标已经存在或输出目录权限过宽则拒绝，不会覆盖。

如果所选请求属于 REPLAY_CANDIDATES，且原签名仅包含 host，可以修改未签名的 Range：

    python3 local/tools/extract_iota_r2_request.py \
      --dir "$HOME/Library/Application Support/iota-tools" \
      --select '<列表中的16位request_id>' \
      --new-range 'bytes=0-25301338' \
      --out "$HOME/.config/r2-relay/private/r2-selected-get-25m.json"

--new-range 仅接受单一 bytes=start-end 闭区间，长度**大于24 MiB 且不超过64 MiB**；不会修改签名 URL。不能修改已签名的 Range，也不支持包含条件头或重复 Range 的重放请求。

## 3. 验收边界

**导出的只是原始请求，不是完整的 R2 实机验收 spec。** 必须另行直接访问真实 R2，验证 HTTP 206、Content-Range、Content-Length、正文总字节数及 SHA-256 后，才能建立验收基线。

SAFE_CANDIDATES 为0而 REPLAY_CANDIDATES 不为0时，并不意味着没有可用候选，可独立验证签名重放。如果两者都为0，需要新捕获有效的签名 GET；不能手动修改 URL 内的签名时间或伪造校验结果。

## 回归测试

    python3 -m pytest -q local/tests/test_extract_iota_r2_request.py

测试使用完全合成数据，不访问真实 IOTA 日志，不含生产签名 URL。
