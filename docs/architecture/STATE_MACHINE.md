# 状态机

## 缓存（按 MD5）
`ABSENT → RECEIVING → STAGED → EXPIRED`；RECEIVING 期间不能命中；STAGED 从首次完整落盘时间起 1800 秒后 EXPIRED。HIT 不改变截止时间。进程重启扫描缓存目录后只恢复未过期 STAGED；临时文件不可恢复为有效缓存。

## 当前 R2 请求执行
`QUEUED → RUNNING → SUCCEEDED` 或 `RUNNING → RETRY_WAIT → RUNNING`，耗尽最多 3 次额外重试进入 FAILED。执行信息仅保存在内存；不同原签名 URL/目标不能共享执行结果。数据缓存状态和 R2 执行状态互相独立。

## 客户端回退
VPS 健康或连通性故障触发 `RELAY_UNAVAILABLE → DIRECT_R2`；R2 本身的业务失败不触发该回退。
