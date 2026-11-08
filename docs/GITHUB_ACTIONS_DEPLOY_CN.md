# R2 Relay：由 ChatGPT 控制的 GitHub Actions VPS 部署

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

先把本仓库当前已审核版本中的以下两个文件传到 VPS 的临时目录：

- `ops/bootstrap_github_actions_deploy.py`
- `ops/r2relay_deploy_receiver.py`

使用已有可信 Root SSH 管理通道执行 bootstrap。示例（在 Mac 仓库根目录）：

```bash
git checkout main
git pull --ff-only
scp ops/bootstrap_github_actions_deploy.py \
    ops/r2relay_deploy_receiver.py \
    root@104.224.168.192:/root/
ssh root@104.224.168.192 \
  "python3 /root/bootstrap_github_actions_deploy.py \
   --public-key '$(cat ~/.ssh/r2-relay-github-actions.pub)' --apply"
```

上面的示例中，公钥必须在 Mac 本地展开后作为参数传入；如果 shell 无法展开远端命令中的本地路径，请先设置：

```bash
PUBKEY="$(cat ~/.ssh/r2-relay-github-actions.pub)"
ssh root@104.224.168.192 \
  "python3 /root/bootstrap_github_actions_deploy.py --public-key '$PUBKEY' --apply"
```

Bootstrap 会：
- 创建独立系统账号 `r2relay-deploy`，Home 为 `/var/lib/r2relay-deploy`，不使用 Root SSH 身份；
- 把 Root receiver 安装到 `/usr/local/libexec/r2relay_deploy_receiver.py`；
- 仅向部署账号的 `~/.ssh/authorized_keys` 追加专用公钥；
- 对该 key 设置固定 Forced Command：`sudo -n /usr/bin/python3 /usr/local/libexec/r2relay_deploy_receiver.py`（sudoers 同时要求命令不带额外参数）；
- 在 `/etc/sudoers.d/r2-relay-deploy` 中只允许不带任何额外参数地执行这一条固定命令，并仅保留 receiver 所需的 `SSH_ORIGINAL_COMMAND` 环境变量；
- 若同名部署账号已存在，必须已经无密码登录能力且没有额外组；未锁定账号直接拒绝，不擅自改变其账户状态；若账号不存在但残留同名 Home 目录，也直接拒绝，避免误删既有数据；
- 使用 `visudo -cf` 验证 sudoers；备份原有 authorized_keys、sudoers 和 receiver；
- 启用 OpenSSH `restrict`，禁用 PTY、端口转发、Agent 转发和 X11 转发；
- 不修改 sshd 配置，不重启 sshd，不修改 Caddy、443、8443、Token 或缓存。

部署密钥不能获得 Root Shell，也不能执行其他 sudo 命令。它只能通过固定 Root receiver 完成受限发布。现有 Root 管理密钥不受影响。

## ChatGPT 触发部署

PR 合并且完成一次性 bootstrap、GitHub Secrets 配置后，告诉 ChatGPT：

```text
将 sunxu/r2-relay 的 Commit <完整40位SHA> 部署到生产 VPS。
```

ChatGPT 在 Issue #41 留下 `/deploy <SHA>` 评论。该评论就是触发事件；无需额外 Dispatch 服务或 MCP。

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

任一条件不满足时，receiver fail-closed，不会切换版本。当前只读 VPS 检查曾报告 `/etc/r2-relay/vps.env` 不存在，而 API 进程仍为 active；在核实实际运行进程的环境来源并安全恢复真实配置前，禁止 bootstrap 和首次部署。不要把真实 Token 发到 Issue、PR 或 ChatGPT。

## 手工回滚

回滚也是部署一个已知、CI 通过的旧 Commit：

```text
/deploy <previous-commit-sha>
```

与正常部署使用同一条受限路径和验收机制。

## 状态边界

GitHub Actions Workflow 和部署代码合并，不等于 VPS 已完成 bootstrap，也不等于 GitHub Secrets 已配置。第一次生产部署必须等这两项完成后再由 ChatGPT 触发。
