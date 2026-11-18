# VPS Core — M1 开发版本（未生产验收）

实现内容：MD5-only 固定 1800 秒磁盘缓存、重启扫描、Prepare/MISS/HIT API、持久化数据上传、R2 预签名 PUT Worker、最多额外 3 次重试、真实 HTTP 状态/ETag/响应体查询。

**仍不建议生产接入：** 这是 M1 原型，尚未完成公网 HTTPS :8443 部署、资源和执行任务限额、原始 Python HTTPS 代理、真实 R2 完整链路测试、原始响应兼容性和故障注入验收。当前只有模拟 R2 的自动化测试。

本地运行测试：
```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e './vps[test]'
cd vps && pytest -q
```

仅在隔离开发环境启动：
```bash
export R2_RELAY_TOKEN='dev-only-token'
export R2_RELAY_CACHE_DIR=/tmp/r2-relay-cache
uvicorn r2relay_vps.app:create_app --factory --app-dir vps --host 127.0.0.1 --port 8000
```

生产网络入口端口要求为 VPS HTTPS :8443，但此开发命令只在 loopback 暴露明文 HTTP，不能直接公网开放。


## R2 下载中继 VPS 核心（Stage 1）

下载能力与现有 UploadPart PUT 缓存完全隔离，新增独立的任务目录、状态和 API。当前代码已具备：

- 每个 request_id 对应一个 R2 GET 任务；幂等重试不会重复发起上游 GET。
- VPS 对 R2 只发起一次 GET；不做 R2 Range 并发，也不自动跟随重定向。
- 响应体超过 24 MiB 时，直接从上游流生成 16 MiB Part。
- Part 写入 `N.part.tmp`，完整写入、fsync、计算 SHA-256 后原子发布为 `N.part`。
- manifest 暴露 WRITING/READY 状态；Part 未 READY 时等待，等待者有数量上限和超时。
- <=24 MiB 响应保存为独立小响应文件，通过 body API 流式取回。
- 上游失败时清理临时文件和局部 Part；完成后 600 秒空闲 TTL 兜底。
- 删除幂等；活动 writer/reader 未退出前不会删除正在使用的文件。

### VPS TOML 配置

配置文件：`/etc/r2-relay/config.toml`。包内默认配置为 `vps/r2relay_vps/defaults.toml`，下载默认关闭：

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

**不要直接把默认的零预算改成启用。** 必须先检查 VPS 实际磁盘容量，再填写正值的 `max_cache_bytes`、`min_free_bytes`、`max_active_tasks`、`max_retained_tasks` 和 `max_waiters`，最后才可将 `enabled` 改为 `true`。配置类型、范围或未知键不合法时 fail-closed。16/24 MiB 核心参数从 TOML 加载，不使用环境变量维护。

### 下载 API

- `POST /v1/downloads`：创建任务，JSON 字段为 `schema_version`、`request_id`、`url`、`headers`。
- `GET /v1/downloads/{task_id}`：查询 manifest。
- `GET /v1/downloads/{task_id}/parts/{part_number}`：等待并流式取回 READY Part。
- `GET /v1/downloads/{task_id}/body`：流式取回 <=24 MiB 小响应。
- `DELETE /v1/downloads/{task_id}`：幂等删除或取消。

所有端点要求 `Authorization: Bearer ...`。下载 API 复用现有 Bearer Token 和 HTTPS 入口；不记录签名 URL 或请求凭据。

### 重要边界

- 下载功能默认关闭；当前未部署 VPS，也没有修改现有 VPS 服务。
- manifest 与幂等索引保存在进程内存。VPS 重启后旧 task_id 不恢复，启动时会清理下载目录中的孤儿数据。
- 下载目录用 Linux 文件锁保证单 worker 独占；关停时先停止 writer、等待 reader 退出，再释放锁。
- 生产启用前仍需先读取 VPS 磁盘容量，确定资源预算；不得凭空填入生产容量值。
