# R2 下载中继 VPS 核心（Stage 1）

## 当前状态

- 关联需求：RLY-020～RLY-039
- 跟踪 Issue：[Issue #63：R2 下载中继 v1](https://github.com/sunxu/r2-relay/issues/63)
- Stage 0：1 GiB 磁盘响应 PoC 已通过。
- Stage 1：VPS 核心 API、任务管理和 Part 存储已进入实现 PR。
- 尚未部署 VPS；下载功能默认关闭。

## 1. TOML 配置

VPS 系统配置路径：`/etc/r2-relay/config.toml`。

包内默认值：

```toml
schema_version = 1

[download]
enabled = false
part_size_mib = 16
split_threshold_mib = 24
cache_ttl_seconds = 600
max_cache_bytes = 0
min_free_bytes = 0
max_active_tasks = 0
max_retained_tasks = 0
max_waiters = 0
wait_timeout_seconds = 30
```

| 配置键 | 含义 | 当前规则 |
|---|---|---|
| `enabled` | 是否启用下载 API | 默认 false；未设置资源预算不得启用 |
| `part_size_mib` | Part 大小 | 固定需求值 16 MiB |
| `split_threshold_mib` | 启用分片的阈值 | 严格大于 24 MiB 才分片 |
| `cache_ttl_seconds` | 完成任务空闲 TTL | 600 秒 |
| `max_cache_bytes` | 下载缓存总磁盘预算 | 需根据 VPS 磁盘实测填写 |
| `min_free_bytes` | 磁盘最低剩余空间保护值 | 需根据 VPS 磁盘实测填写 |
| `max_active_tasks` | 同时运行的 GET 任务上限 | 需按 VPS 能力确定 |
| `max_retained_tasks` | 内存中保留的任务记录上限 | 需按 VPS 能力确定 |
| `max_waiters` | 等待 READY Part 的连接总上限 | 需按 VPS 能力确定 |
| `wait_timeout_seconds` | 单个 Part 等待超时 | 默认 30 秒，可在 1～300 范围内配置 |

下载功能默认关闭。启用前必须检查 VPS 实际磁盘容量和现有缓存占用，填写上述资源预算；配置错误或预算未设置时，服务拒绝启用下载，不影响现有上传 API。

16 MiB、24 MiB、600 秒来自已确认需求。缓存容量、最低剩余空间、活动任务数、保留任务数和等待者上限尚未根据 VPS 实际磁盘/负载确定，不能将测试值当成生产值。

## 2. API

所有端点使用现有 `Authorization: Bearer <token>` 鉴权。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/downloads` | 创建任务 |
| GET | `/v1/downloads/{task_id}` | 查询 manifest |
| GET | `/v1/downloads/{task_id}/parts/{part_number}` | 等待并读取一个 READY Part |
| GET | `/v1/downloads/{task_id}/body` | 读取 <=24 MiB 小响应 |
| DELETE | `/v1/downloads/{task_id}` | 幂等删除或取消 |

创建请求示例：

```json
{
  "schema_version": 1,
  "request_id": "client-generated-idempotency-key",
  "url": "https://<account>.r2.cloudflarestorage.com/<bucket>/<object>",
  "headers": [
    ["Range", "bytes=0-1048575"],
    ["If-Range", "\"etag-value\""]
  ]
}
```

实际请求中的 URL 和签名请求头由客户端原样提供；示例中的占位符不能直接发送。API 不返回 URL、请求头或 Bearer Token。

## 3. Part 生命周期

1. 每个 GET 只创建一个独立任务，并由 VPS 向 R2 发起一次 GET。
2. 已知响应体大于 24 MiB 时直接流式写入 Part。
3. 未知长度或声明长度较小但实际超过阈值时，先用有界临时 spool 暂存前缀；跨过 24 MiB 后将前缀交给 Part writer，继续消费同一条 R2 响应流。
4. Part 写入 `N.part.tmp`，完整写入、fsync 并计算 SHA-256 后原子改名为 `N.part`。
5. 只有原子发布后的 Part 状态为 READY；未完成 Part 不可读取。
6. 本地请求未 READY 的 Part 时等待；等待连接受 `max_waiters` 和 `wait_timeout_seconds` 限制。
7. 小响应保存在独立 body 文件中，只有任务 COMPLETE 后才允许读取。
8. 上游失败时任务 FAILED，删除临时文件和已发布的局部 Part，不允许交付部分对象。
9. 成功读取活动会刷新空闲 TTL；删除请求不会在 writer 或 reader 活动期间移除文件。

## 4. 任务状态与重启

任务状态：`QUEUED`、`RECEIVING`、`COMPLETE`、`FAILED`、`CANCELLED`。

Part 状态：`WRITING`、`READY`、`FAILED`。

当前 manifest 与 request_id 幂等索引保存在 VPS 进程内存。VPS 重启后不恢复旧任务；启动时清理下载目录中的孤儿文件，避免把未完成任务当作完整对象。客户端必须创建新的 request_id 和任务。

## 5. 隔离和安全

- 下载使用独立目录 `/var/lib/r2-relay/downloads`，不使用上传 MD5 缓存目录。
- 不改动上传 API、上传 MD5 缓存或 1800 秒 TTL。
- 只允许 HTTPS 443 的 `.r2.cloudflarestorage.com` endpoint。
- 过滤 hop-by-hop 请求/响应头，同时保留签名相关 Host、查询参数和端到端响应头。
- 不自动跟随 3xx；保留 R2 真实状态码和原始响应体。
- 不记录签名 URL、请求 Authorization 或 Bearer Token；错误响应不回显 HTTP 客户端异常文本。
- URL 长度、请求头数量及总长度有明确上限；Host 若由签名请求显式提供，必须匹配 URL 主机名。
- 当前无 VPS 部署授权，下载功能保持默认关闭。
