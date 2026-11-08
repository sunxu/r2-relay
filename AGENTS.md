# R2 Relay 开发 Agent 规范

## 权威来源
- `docs/requirements/REQUIREMENTS.md`：已确认需求，修改需 Owner 批准。
- `docs/requirements/ACCEPTANCE.md`：验收条件与需求 ID。
- `docs/architecture/ARCHITECTURE.md`、`API_PROTOCOL.md`：实施设计。
- `docs/adr/`：已批准的技术决策。
- GitHub Issues 跟踪任务，PR 留存实现与测试证据。

## 开发规则
1. 开发前阅读需求、验收、相关 ADR；用简体中文撰写文档、Issue、PR、错误说明，专业名词可保留英文。
2. 不得擅自变更：本地代理 `127.0.0.1:18585`、VPS HTTPS `8443`、MD5 唯一缓存键、固定 1800 秒 TTL、纯内存索引+磁盘重建、额外最多 3 次 R2 重试、VPS 故障直传回退、不接管合并。
3. 对 R2 UploadPart 必须先获得真实 R2 成功结果才能向客户端返回成功；缓存落盘不等于 R2 上传成功。
4. 非目标请求必须正常透传；不得关闭 TLS 证书校验，不得改变原始 S3 签名需要的请求信息。
5. 按需求编写自动化测试、故障注入与性能基准；没有证据不得宣称已经达到 100 Mbps。
6. 需求调整先提 Issue，更新需求和 ADR，再写实现；每个 PR 关联需求 ID 和验收项。
7. 不得未经授权部署生产、修改第三方服务端或提交真实密钥。完整签名 URL 可以在受控本地日志中展示，但不可提交为仓库测试样例。
8. RC1 离线版本已通过 PR #15 合并 `main`（`18e068a923238bccb83b2e8a77fe25ac4f7abb32`）；禁止将 Runner 模拟结果写成真实 VPS/R2 生产验收。真实协议与网络性能的后续工作以 Issues #16、#17 为准。

## GitHub 治理自动检查
- 参阅 `.github/governance/README.md`；RLY 和 Txx 编号不可随意重新分配。
- Issue 正文包含治理元数据以分配 Milestone 与 Labels；不要直接删除此字段。
- PR 标题或正文必须出现有效 RLY-xxx，正文必须列出 Txx 与 `Refs #N` / `Closes #N`。
- Actions 检查只保证引用格式，不表示测试通过；合并前仍需人工核实证据。
