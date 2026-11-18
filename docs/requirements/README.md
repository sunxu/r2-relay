# 需求管理索引

权威需求见 [REQUIREMENTS.md](REQUIREMENTS.md)，可测验收条件见 [ACCEPTANCE.md](ACCEPTANCE.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)。

完整的 GitHub 需求治理、Labels、Milestones、PR 自动检查规则见 [GitHub 治理规范](../../.github/governance/README.md)。

流程：需求变更 Issue → Owner 决策 → 需求与 ADR 更新 → 实现 PR → 测试验收 → 合并。每项需求使用固定 `RLY-xxx` 标识；Issue 编号只用于任务跟踪，不能代替需求编号。

当前上传基线：v1.0～v1.5；下载独立基线：R2 Download Relay v1（Issue #63）。下载状态：Owner 已确认设计，尚未实现/部署。下载先通过 mitmproxy 磁盘响应 PoC，再按 VPS Core、Local Proxy、Integration、真实 R2 Acceptance 分阶段实施。
