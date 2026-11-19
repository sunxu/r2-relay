# R2 Relay：由 ChatGPT 控制的 GitHub Actions VPS 部署

## Python package build isolation

The release tree is intentionally owned by `root:r2relay` with source directories `0750` and files `0640` (executable files `0750`). The service account must not write into the deployed source tree. Since setuptools may create `build/` beneath the local project during `pip install`, the receiver copies `vps/` into a disposable `/var/tmp/r2-relay-build-*` directory, makes only that copy writable by `r2relay`, and installs from the copy. The temporary build tree is removed on success or failure. Repository-controlled build hooks continue to run as the unprivileged `r2relay` account; the immutable release source remains root-owned.

## 目标

代码 Push / PR 合并只运行 CI，不自动部署生产。ChatGPT 选择完整 Commit SHA，并在本仓库的固定部署控制 Issue #41 留下精确命令：

```text
/deploy <40-char-lowercase-commit-sha>
```

GitHub Actions 收到该 Issue 评论后，验证发起人、Issue、Commit 是否属于 main、目标 Commit 的 CI 是否全部完成且通过，然后通过 VPS 上的受限 SSH forced-command key 发送源码归档。VPS 接收器完成版本安装、原子切换、systemd 重启、健康检查和失败回滚。

不需要单独的 vps-deployment-manager，不需要额外 MCP Server，不使用 push/PR 自动部署，也不使用 workflow_dispatch。

## GitHub 配置（一次性）

在仓库 Settings → Secrets and variables → Actions → Secrets 新增：

| Secret | 内容 |
|---|---|
| `VPS_DEPLOY_SSH_KEY` | 专用 Ed25519 私钥全文 |
| `VPS_SSH_KNOWN_HOSTS` | VPS SSH host key 的已验证 known_hosts 行 |

VPS 地址和端口固定为 `104.224.168.192:22`，SSH 登录账号固定为 `r2relay-deploy`，不接受 Issue 评论修改目标主机或登录账号。

### 生成专用密钥

在可信 Mac 上执行：

```bash
ssh-keygen -t ed25519 -a 64 -N "" \
  -f ~/.ssh/r2-relay-github-actions \
  -C r2-relay-github-actions
```

把私钥 `~/.ssh/r2-relay-github-actions` 放入 GitHub Secret `VPS_DEPLOY_SSH_KEY`。公钥只用于 VPS 一次性 bootstrap。不要把私钥提交到仓库、Issue 或日志。

### 固定 VPS Host Key

从 Mac 已经可信的 `~/.ssh/known_hosts` 中取得 VPS 对应的 host key 行；或者通过可信 VPS 控制台核对 SSH host key 指纹后生成。把验证过的行放入 `VPS_SSH_KNOWN_HOSTS`。

不要只运行 `ssh-keyscan` 就盲目信任结果。Workflow 使用 `StrictHostKeyChecking=yes`，不接受关闭验证。

## VPS 一次性 bootstrap

> **职责边界：这是一项由 Owner 执行的一次性初始化，不纳入 GitHub Actions 自动化范围。** Owner 可以通过现有可信 Root SSH 管理通道执行下面的 Bootstrap；完成后，日常部署不再需要人工登录 VPS 或执行命令。无需新增 Bootstrap Workflow、VPS MCP Server 或独立管理项目。


**必须使用 PR #71 合并后记录的精确 Merge Commit SHA，不要直接使用不断变化的 main HEAD。** 本轮固定 Bootstrap SHA 为 `12de7b52ba5e824455da15640f0cba8b6454ea08`。先在可信 Mac 上检出该固定提交，并确认工作区：

```bash
git fetch origin main
git checkout --detach 12de7b52ba5e824455da15640f0cba8b6454ea08
test "$(git rev-parse HEAD)" = "12de7b52ba5e824455da15640f0cba8b6454ea08"
git status --short
```

把这个提交中的以下两个文件传到 VPS 临时目录：

- `ops/bootstrap_github_actions_deploy.py`
- `ops/r2relay_deploy_receiver.py`

使用已有可信 Root SSH 管理通道执行 Bootstrap（在 Mac 仓库根目录）：

```bash
scp ops/bootstrap_github_actions_deploy.py \
    ops/r2relay_deploy_receiver.py \
    root@104.224.168.192:/root/

PUBKEY="$(cat ~/.ssh/r2-relay-github-actions.pub)"
# 先做只读预检（不带 --apply）
ssh root@104.224.168.192 \
  "python3 /root/bootstrap_github_actions_deploy.py --public-key '$PUBKEY'"

# 确认 DRY_RUN_PASS 且 fingerprint 与本机一致后，再实际应用
ssh root@104.224.168.192 \
  "python3 /root/bootstrap_github_actions_deploy.py --public-key '$PUBKEY' --apply"
```

不要将私钥或 Token 复制到 VPS。

Bootstrap 会：
- 创建独立系统账号 `r2relay-deploy`，Home 为 `/var/lib/r2relay-deploy`，不使用 Root SSH 身份；
- 把 Root receiver 安装到 `/usr/local/libexec/r2relay_deploy_receiver.py`；
- 仅管理部署账号的专用公钥；升级时只识别并替换本项目此前精确生成的旧 ForcedCommand 行，保留相同公钥和限制选项，拒绝未知 key 或授权选项；
- 对该 key 设置固定 Forced Command：`sudo -n /usr/local/libexec/r2relay_deploy_receiver.py`；receiver 使用固定 `/usr/bin/python3` shebang，sudoers 对这个可执行文件路径设置 `NOPASSWD` 并以 `""` 限定零参数。不要把 `/usr/bin/python3` 本身列为 sudo 授权命令，否则可能扩大为任意 Root Python 执行。
- 在 `/etc/sudoers.d/r2-relay-deploy` 中只允许不带任何额外参数地执行这一条固定命令，并仅保留 receiver 所需的 `SSH_ORIGINAL_COMMAND` 环境变量；
- 若同名部署账号已存在，必须已经无密码登录能力且没有额外组；未锁定账号直接拒绝，不擅自改变其账户状态；若账号不存在但残留同名 Home 目录，也直接拒绝，避免误删既有数据；
- 使用 `visudo -cf` 验证 sudoers；备份原有 authorized_keys、sudoers 和 receiver。升级时只接受脚本已知的精确旧版 sudoers 文本或当前新版文本，其他自定义规则一律拒绝，不会覆盖未知授权；
- 启用 OpenSSH `restrict`，禁用 PTY、端口转发、Agent 转发和 X11 转发；
- 不修改 sshd 配置，不重启 sshd，不修改 Caddy、443、8443、Token 或缓存。

部署密钥不能获得 Root Shell，也不能执行其他 sudo 命令。它只能通过固定 Root receiver 完成受限发布。现有 Root 管理密钥不受影响。

### Bootstrap v6 前的目录恢复

在可信 Root SSH 通道先恢复私有环境目录，不要移动或读取 Token：

```bash
sudo chown root:root /etc/r2-relay
sudo chmod 0700 /etc/r2-relay
sudo namei -l /etc/r2-relay/vps.env
```

确认 `/etc/r2-relay` 为 `root:root 0700`、`vps.env` 仍为 `root:root 0600` 后，再按本次 PR 合并后的精确 SHA 执行 Bootstrap v6。新 receiver 会在 `/download-config disabled <SHA>` 时自动创建独立 drop-in 目录，不需要手工开放旧目录。

### Bootstrap 后的无部署连通性验证

在 GitHub Secrets 配置完成前，可用专用私钥向受限入口发送一个**空归档**，验证 SSH 公钥认证、ForcedCommand、sudoers 和 Root receiver 全链路。该命令不会进入 `deploy()`，不会切换版本或重启服务：

```bash
MERGE_SHA="12de7b52ba5e824455da15640f0cba8b6454ea08"
set +e
printf '' | ssh -T \
  -i ~/.ssh/r2-relay-github-actions \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o ClearAllForwardings=yes \
  r2relay-deploy@104.224.168.192 \
  "deploy $MERGE_SHA"
SSH_STATUS=$?
set -e
printf 'ssh_exit=%s\\n' "$SSH_STATUS"
```

预期 receiver build ID 为 `r2-relay-deploy-receiver-v6-isolated-download-config-dir`，返回 `{"reason":"empty_archive","status":"REJECTED_OR_FAILED"}` 且 SSH 退出码为 1（这是故意的拒绝结果）。若出现 SSH authentication、locked account、sudo 或 receiver 执行错误，则先停止，不要触发生产部署。


## ChatGPT 触发部署

PR 合并、Owner 完成一次性 VPS Bootstrap，并配置好两个 GitHub Actions Secrets 后，告诉 ChatGPT。Bootstrap 是唯一预先约定的人工 VPS 操作；此后指定 Commit 的部署、验收、失败回执和自动回滚均由 GitHub Actions 完成：

```text
将 sunxu/r2-relay 的 Commit <完整40位SHA> 部署到生产 VPS。
```

ChatGPT 在 Issue #41 留下 `/deploy <SHA>` 评论。该评论就是触发事件；无需额外 Dispatch 服务或 MCP。

应用下载配置使用同一个固定控制 Issue 和同一把受限 SSH key，命令为：

```text
/download-config disabled <40-char-lowercase-commit-sha>
```

该命令只支持 `disabled`，不提供启用命令、任意 TOML 输入或 `workflow_dispatch`。目标 SHA 必须属于 main、通过 deploy-tests，并在 Git tree 中包含 `deploy/download-config-dropin-v1` 能力标记；配置可以先于应用版本部署，确保新版本第一次启动前下载已明确关闭。Workflow 通过固定 Root receiver 写入独立的 `/etc/r2-relay-download/download.toml`，不覆盖现有 `/etc/r2-relay/config.toml`：

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

该文件由 Root 原子写入，权限为 `root:r2relay 0640`；其独立父目录 `/etc/r2-relay-download` 为 `root:r2relay 0710`（服务账户仅有目录搜索权限）。应用前备份旧文件，重启仅限 `r2-relay-vps.service`，健康检查失败则恢复旧配置并再次重启。回执明确报告 `download_enabled=false`。配置应用不修改 Token、EnvironmentFile、上传缓存、Caddy 或 443/8443。当前命令只能关闭下载，后续若要启用，必须另行审查容量预算和授权，不属于本次授权。

**目录隔离要求：** `/etc/r2-relay` 同时包含私有 `vps.env`，必须保持 `root:root 0700`；不得为了读取下载 drop-in 而放宽该目录权限。下载 drop-in 独立存放在 `/etc/r2-relay-download/download.toml`，由 v6 receiver 在受控配置应用时创建并校验目录为 `root:r2relay 0710`、文件为 `root:r2relay 0640`。此前遗留的 `/etc/r2-relay/download.toml` 不再被新版本读取，可留在 root-only 目录内，不要将其父目录重新开放。

生产配置加载器默认只读取包内 `defaults.toml` 和独立下载 drop-in；不会探测 `/etc/r2-relay/config.toml`。只有显式传入通用配置路径的调用方才会加载该文件。

新部署的 Release 会包含能力标记；旧 Release 仍可按原流程部署/回滚，但不能对不含该标记的旧 Release 应用下载配置。新增此操作需要把 Root receiver 升级到 build ID `r2-relay-deploy-receiver-v6-isolated-download-config-dir`。这仍使用原有 `r2relay-deploy` ForcedCommand 和零参数 sudo 授权，不扩大 SSH/sudo 权限；但因 receiver 是独立安装的，合并后 Owner 需通过可信 Root SSH 通道再运行一次现有 Bootstrap（先 dry-run、核对原有 Ed25519 指纹，再 apply）。Bootstrap 只原子更新 Root receiver / sudoers / authorized_keys，不重启 R2 Relay、Caddy 或 sshd。升级后用空归档 `deploy <SHA>` 烟测，预期 v6 回执 `reason=empty_archive`、SSH exit 1。

推荐顺序：先通过 `/download-config disabled <目标 SHA>` 应用关闭配置（目标 SHA 必须已在 main 且含 drop-in 能力标记）；再通过 `/deploy <同一 SHA>` 部署应用。这样新版本第一次启动时就会读取 `enabled=false`。两条命令都只接受 Issue #41 上由 `sunxu` 发布的精确评论。

控制 Workflow 只接受：
- Issue #41（固定控制 Issue，不能在 PR 评论中触发）；
- 评论作者 `sunxu`；
- 完全匹配的 `/deploy <40位小写 SHA>`；
- 目标 Commit 必须是 `main` 的祖先；
- 目标 Commit 必须存在最新的 `deploy-tests` Check Run，且状态为 `completed`、结论严格为 `success`。该工作流在 PR 上运行，也会在每次 main Push 后运行，确保合并后的目标 Commit 本身有可验证的部署专项结果。

## 部署行为和保护范围

- 新版本安装到 `/opt/r2-relay-releases/<SHA>`。
- `/opt/r2-relay-current` 原子指向当前版本。
- systemd 仅重启 `r2-relay-vps.service`。
- 生产环境文件 `/etc/r2-relay/vps.env` 不上传、不覆盖、不读取。
- 缓存 `/var/lib/r2-relay/cache` 不迁移、不删除。
- Caddy、443、8443、自签名证书、MCP 服务和 Tunnel 不修改。
- 新版本通过 `http://127.0.0.1:18085/healthz` 检查后才报告成功。
- 失败时恢复旧 systemd unit 和旧 current symlink，再重启旧版本。
- 如果目标 SHA 已有 release，receiver 会先逐项检查目录/文件所有权、权限、符号链接和 Python runtime import；通过后直接复用，不重复安装，也不覆盖已有版本。这使自动回滚后的同 SHA 重试和已安装旧版本的手工回滚可用。
- 旧 release 和备份保留，不自动清理。

VPS receiver 会拒绝超限归档、路径穿越、符号链接、硬链接、设备文件、危险权限、Commit marker 不匹配以及非批准 systemd unit 模板。Python 依赖构建在 `r2relay` 非特权账户下执行，不以 Root 运行仓库构建代码。

## 首次部署的前置条件

首次执行前，必须确认：
- `r2relay` 用户和组存在；
- `/etc/r2-relay/vps.env` 是 root 拥有且权限不宽于 0600 的普通文件；
- `/var/lib/r2-relay/cache` 是 `r2relay:r2relay` 所有的目录；
- 当前 `r2-relay-vps.service` 与仓库已知旧模板完全一致且处于 active；
- 必须从实际 systemd unit 核实 `EnvironmentFile` 的真实路径；该文件必须存在、root 拥有且权限不宽于 0600，不能创建空文件或占位 Token；
- VPS 有 Python 3.11、venv、pip 和访问 PyPI 的网络；
- GitHub Secrets 已设置，SSH host key 已核验。

任一条件不满足时，receiver fail-closed，不会切换版本。生产 EnvironmentFile 已由 Owner 在 VPS 上直接核实：`systemctl show` 指向 `/etc/r2-relay/vps.env (ignore_errors=no)`；`stat` 确认普通文件、`root:root`、`0600`、462 bytes；只检查 `/proc/<MainPID>/environ` 的变量名，确认运行进程已加载 `R2_RELAY_TOKEN`，未读取值。因此 EnvironmentFile 缺失这一部署阻塞已解除。此前 `deployment_inspect` 报告文件不存在属于检查器误报：旧实现把所有 `OSError` 都当作不存在；PR 分支已修复为区分 `ENOENT`、权限错误和其他 OS 错误，并通过 5 项检查测试。不要把真实 Token 发到 Issue、PR 或 ChatGPT。

## 手工回滚

回滚也是部署一个已知、CI 通过的旧 Commit。若该 SHA 的版本目录仍存在且通过完整性检查，会直接切回该版本；不存在时才重新构建：

```text
/deploy <previous-commit-sha>
```

与正常部署使用同一条受限路径和验收机制。

## 状态边界

GitHub Actions Workflow 和部署代码合并，不等于 VPS 已完成 bootstrap，也不等于 GitHub Secrets 已配置。第一次生产部署必须等这两项完成后再由 ChatGPT 触发。

## Deployment failure diagnostics

Every receiver response includes `receiver_build_id`. The release group-read permission fix is identified by `r2-relay-deploy-receiver-v3-release-group-read-permissions`; the Python venv `lib64 -> lib` symlink allowlist is identified by `r2-relay-deploy-receiver-v4-venv-lib64-symlink`. Empty-archive smoke tests prove the exact root-owned receiver is installed without accepting a deployment archive. Runtime preparation failures identify the exact fixed step (`venv_create`, `pip_requirements`, `pip_project`, or `runtime_import`) and exit code. Raw subprocess stdout/stderr is deliberately not returned in the SSH receipt, avoiding accidental disclosure of package-index credentials or environment data.

## Release source permissions

The deployed release source is owned by `root:r2relay`. Regular files use `0640`, executable files use `0750`, and directories use `0750`: the service account can read/traverse source through its group but cannot modify it. Do not use `0600` for regular release files; that denies the `r2relay` group read access and prevents the service account from reading the requirements lockfile and Python modules. The disposable package-build copy remains owned by `r2relay` with private writable modes.

## Python virtual-environment symlink policy

The receiver allows exactly one directory symlink generated by the platform Python venv: `.venv/lib64 -> lib`. The target must be the real `.venv/lib` directory within the same release. This link is not traversed or chowned. All other directory symlinks remain rejected. The existing narrowly scoped `.venv/bin/python*` interpreter links remain validated against `/usr/bin/python3.11` during release reuse/rollback validation.


## Current read-only status verification

The production service runs from the immutable release layout:

- `/opt/r2-relay-current` is the root-owned current-version symlink.
- `/opt/r2-relay-releases/<40-char-commit-sha>` contains immutable release files and `.deployment-commit`.
- `/var/backups/r2-relay/deploy/` contains pre-switch unit/state backups.
- The active systemd unit uses `WorkingDirectory=/opt/r2-relay-current` and the release-local venv, while preserving `/etc/r2-relay/vps.env` and `/var/lib/r2-relay/cache`.

The root-only `vps_r2_service_verify` check validates the active release symlink and commit marker, release ownership/modes, unit bytes against the active release's `deploy/vps.service`, `WorkingDirectory`, `ExecStart`, private EnvironmentFile permissions and required values, and the listener contract: **443 present, 8443 present (Caddy), and 18085 bound only to 127.0.0.1 when the relay is active**. It does not start or restart the production service during verification. Environment values are validated in memory but never returned by the tool.

The unified `vps_r2_deployment_status` entry point now invokes this fixed root-only verifier directly. It no longer depends on a second legacy Inspector or compares two independently maintained implementations. A PASS means the fixed release, configuration, systemd and listener checks passed; it does **not** prove a real Cloudflare R2 upload.

The one-time bootstrap updates the fixed root helper, loopback verifier, generic MCP module and deployment receiver, then restarts only `r2-rootd.service` and `r2-mcp.service`. It does not restart `r2-relay-vps.service`, Caddy or the MCP tunnel. A backup is created under `/var/backups/vps-mcp/r2-loopback-control-upgrade/`.

The retired legacy inspector source and its tests/docs are removed from this repository. Any already-installed legacy file or MCP tool registration on the VPS is intentionally left untouched and must be handled in a separate, explicitly approved maintenance window.
