# 系统架构

## 数据路径
```text
原始 aiohttp 上传程序
  └─ 显式 HTTP 代理 127.0.0.1:18585
        ├─ 非目标流量 → 常规 CONNECT/TCP 隧道 / HTTP 转发
        └─ 目标 R2 UploadPart → TLS MITM → 完整读入内存 → MD5
                 └─ HTTPS 连接池 → VPS :8443
                      ├─ HIT → 复用磁盘缓存
                      └─ MISS → 上传完整分片 → 磁盘原子发布
                           └─ R2 Worker → 原始预签名 PUT → 真实响应
```

本地 100 Mbps、VPS 标称 1000 Mbps；缓存首次写入后即可开始对应 R2 PUT，实现跨分片流水线。原客户端仍等待对应 R2 真正上传完成才获得成功响应。合并请求不进入缓存或任务协调。

## 组件边界
- `local/proxy`：CONNECT、目标证书、HTTP 请求分类；非目标请求必须透明。
- `local/buffer`：完整缓冲（限制单请求/总预算），计算 MD5；合法超限内容预先选择流式直传。
- `local/relay_client`：VPS Prepare、MISS 正文传输、结果等待，复用 HTTP/2。
- `local/fallback`：只在 VPS 服务不可用时直传 R2，使用原 URL 与原请求字节。
- `vps/cache`：MD5 → 完整磁盘文件索引，固定 TTL，启动扫描恢复。
- `vps/executions`：每个当前 R2 目标独立执行，记录响应；短期进程内生命周期。
- `vps/r2_worker`：按当前签名 URL PUT，额外最多三次可重试错误；每次重新读取完整缓存数据。

## 正确性边界
- 只有真正的 R2 成功 HTTP 响应可使代理返回成功。
- 缓存 HIT 仅代表存在字节，不代表当前 R2 UploadId 已经有分片。
- 对同一目标尽量合并仍在运行的执行，避免 VPS 自己的重叠 PUT；但无法保证 R2 中一个已返回 502 的旧请求已完全终止。
- 不管理 UploadId、CompleteMultipartUpload、ListParts、Abort；不能在合并失败后主动修复，除非外部重新请求 UploadPart。
- 重启只恢复缓存索引，不恢复网络等待者、内存队列或已消失的签名 URL。
- 30 分钟固定到期可终止缓存命中及后续重试。
- 允许完整凭证日志仅限受控环境；禁止写入仓库。
- VPS 中继仅允许预期 R2 Host/HTTPS scheme，认证后处理请求，防 SSRF 与任意代理滥用。

## 操作模式
初期单 VPS 状态协调进程，避免多个进程各自维护不同内存索引。VPS 前可有 TLS 网关；不能在不共享状态的情况下直接横向扩展多个 API Worker。
