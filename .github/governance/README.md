# GitHub 需求与开发治理

## 需求编号与状态
- `docs/requirements/REQUIREMENTS.md` 中的 `RLY-001` 等为长期稳定需求 ID；GitHub Issue 编号是开发任务编号，不替代 RLY ID。
- `docs/requirements/ACCEPTANCE.md` 的 `T01` 等是验收用例 ID。每项任务至少标注需求 ID、验收用例和相关文件。
- `docs/requirements/CHANGELOG.md` 按版本记录已批准的需求变更。需求、验收或 ADR 修改需要 Owner 审核。
- 工作顺序：提出 Issue → 确认需求 → 功能分支与 PR → 自动测试/验收 → 审核合并 → 关闭 Issue。
- 禁止仅因 PR 合并就声称性能、部署或生产验收通过；必须有测试证据。

## Labels
规范由 `.github/governance/config.json` 定义，包含：
- type: feature / bug / research / docs / test
- priority: P0 / P1 / P2
- status: blocked / needs-review
- area: local / vps / protocol / test

优先级说明：P0 为核心交付阻断项；P1 为重要完整性/发布工作；P2 为后续优化。

## Milestones
- M1 - VPS Core
- M2 - Local Proxy
- M3 - Integration
- M4 - Release v1.0

## 实施机制
- `.github/workflows/governance-bootstrap.yml`：main 分支上的治理配置变更或手动触发时，初始化/更新 Labels、Milestones，并依据 Issue 正文内的 `<!-- governance: milestone=M1; labels=type:feature,priority:P0,area:vps -->` 元数据归类已有 Issues。
- `.github/workflows/pr-governance.yml`：PR 必须包含至少一个 `RLY-xxx`、至少一个 `Txx` 和 `Closes #N` 或 `Refs #N`。
- CI 只验证引用格式与 Issue 是否存在，不宣称功能或验收通过；如需强制阻止合并，请在 GitHub branch ruleset 中将 `PR governance` 配置为 required check。
- GitHub Actions 需要仓库策略允许运行，且 GITHUB_TOKEN 具有相应 Issues 写权限。若自动初始化被禁用，应从 Actions 手动运行 `Governance bootstrap` 并检查日志。

## 新任务模板
标题：`[模块][RLY-xxx] 动作`；正文包含 RLY ID、Txx 验收项、测试方式、边界和 governance 元数据。任何 Issue 关闭前都应关联实现 PR 和可复核测试证据。
