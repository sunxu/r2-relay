# 本地代理 ↔ VPS API v1（设计草案）

所有接口通过 HTTPS `:8443`，使用 Bearer Token。请求与响应 JSON UTF-8；正文传输采用原始 application/octet-stream。建议 HTTP/2 长连接复用。接口字段和错误码应在实现联调测试后冻结。

## POST /v1/parts/prepare
请求示例：
```json
{
  "schema_version":1,
  "request_id":"req-001",
  "md5":"41986b09b7833d3bdbd4ab3b068746d6",
  "size":10468231,
  "r2_request":{
    "method":"PUT",
    "url":"https://ACCOUNT.r2.cloudflarestorage.com/BUCKET/KEY?uploadId=UPLOAD_ID&partNumber=10&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=...",
    "headers":[["Host","ACCOUNT.r2.cloudflarestorage.com"],["Content-Type","application/octet-stream"]]
  }
}
```
- HIT → `{"status":"HIT","execution_id":"exec-001"}`
- MISS → `{"status":"MISS","transfer_id":"transfer-001","execution_id":"exec-001"}`
- HIT 不传文件正文；MISS 才使用 transfer_id。
- MD5 是唯一内容缓存键；R2 请求 URL 始终是每次请求专属。完整 URL/签名字段必须按原始值保留、且验证目标 Host 在允许列表内。
- Prepare 同样为任务创建执行 ID，但不代表 R2 成功。

## PUT /v1/parts/data/{transfer_id}
Content-Type: application/octet-stream；X-Content-MD5-Hex: 32 位 MD5；正文为完整分片。接收端写入临时文件，完成后持久化并原子发布；返回 `{"status":"STAGED","execution_id":"exec-001"}`。仅 STAGED 后可运行该请求的 R2 Worker。相同 MD5 首次并发不专门处理，但任何未完成文件都不得返回 HIT。

## GET /v1/executions/{execution_id}
```json
{
  "status":"SUCCEEDED",
  "attempts":2,
  "upstream_response":{
    "status_code":200,
    "headers":[["ETag","\"41986b09b7833d3bdbd4ab3b068746d6\""]],
    "body_base64":""
  }
}
```
状态：QUEUED、RUNNING、RETRY_WAIT、SUCCEEDED、FAILED。允许长轮询形式以减少查询开销。只有真实 R2 HTTP 响应才设置 upstream_response；本地代理故障采用明确分类的 relay_error。返回原 aiohttp 时过滤逐跳头，并重新计算必要的 Content-Length。

## 失败语义
- VPS 连接/握手/健康/临时存储故障 → 本地直传 R2。
- VPS 正常返回 R2 的 4xx/5xx → 交给原客户端，不在本地另开并发直传。
- 未得到任何 R2 HTTP 响应 → 不伪造 R2 错误来源；代理返回可诊断错误。
- 预签名 URL 过期 → 不尝试延长或重签；由原业务提供新 URL。
- GET/POST/PUT 都需鉴权；对 request_id、transfer_id、execution_id 做过期管理与最小权限检查。

## 兼容要求
原始 PUT 的 Host、路径、查询、签名相关头必须保持 R2 验证所需的内容；避免把 hop-by-hop 头复制到 R2 或回传给客户端。对 R2 ETag 保持原样，包括双引号。
