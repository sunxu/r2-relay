#!/usr/bin/env python3
"""Manage the user-scoped R2 Relay LaunchAgent on macOS.

No operation uses sudo, handles R2 upload traffic, or changes the VPS.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

LABEL = 'com.sunxu.r2-relay.local'
SHA_PATTERN = re.compile(r'^[0-9a-f]{40}$')


def paths():
    home = Path.home()
    return {
        'home': home,
        'data': home / '.local/share/r2-relay',
        'manager': home / '.local/share/r2-relay/manage.py',
        'plist_template': home / '.local/share/r2-relay/agent.plist',
        'releases': home / '.local/share/r2-relay/releases',
        'current': home / '.local/share/r2-relay/current',
        'previous': home / '.local/share/r2-relay/previous',
        'bin': home / '.local/bin',
        'run': home / '.local/bin/r2-relay-run',
        'cli': home / '.local/bin/r2-relay',
        'config_dir': home / '.config/r2-relay',
        'config': home / '.config/r2-relay/client.env',
        'logs': home / 'Library/Logs/r2-relay',
        'plist': home / 'Library/LaunchAgents' / (LABEL + '.plist'),
    }


def run(cmd, *, check=True, **kwargs):
    return subprocess.run([str(v) for v in cmd], check=check, **kwargs)


def ensure_macos():
    if sys.platform != 'darwin':
        raise RuntimeError('仅支持 macOS；请勿在 VPS/Linux 上安装 LaunchAgent')
    if os.geteuid() == 0:
        raise RuntimeError('禁止 root/sudo 运行 Mac 用户级服务管理器')


def domain():
    return f'gui/{os.getuid()}'


def service():
    return f'{domain()}/{LABEL}'


def is_loaded():
    return run(['launchctl', 'print', service()], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def port_available():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', 18585))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_port(*, should_listen: bool, seconds: float = 12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (not port_available()) == should_listen:
            return True
        time.sleep(0.2)
    return (not port_available()) == should_listen


def managed_listener():
    """Verify the TCP listener belongs to launchd's actual child PID."""
    if not is_loaded():
        return False
    info = run(['launchctl', 'print', service()], check=False,
               capture_output=True, text=True)
    match = re.search(r'(?m)^\s*pid\s*=\s*(\d+)\s*$', info.stdout)
    if not match:
        return False
    listener = run(['/usr/sbin/lsof', '-nP', '-t', '-iTCP:18585',
                    '-sTCP:LISTEN'], check=False, capture_output=True, text=True)
    if listener.returncode != 0:
        return False
    pids = {int(line) for line in listener.stdout.splitlines() if line.isdigit()}
    return pids == {int(match.group(1))}


def wait_managed_listener(seconds: float = 12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if managed_listener():
            return True
        time.sleep(0.2)
    return managed_listener()


@contextmanager
def operation_lock(p):
    """Prevent concurrent CLI install/update/activation transactions."""
    p['data'].mkdir(parents=True, exist_ok=True, mode=0o700)
    if p['data'].is_symlink() or p['data'].stat().st_uid != os.getuid():
        raise RuntimeError('数据目录不可信，拒绝管理')
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(p['data'] / '.manage.lock', flags, 0o600)
    try:
        if (not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid()
                or stat.S_IMODE(os.fstat(fd).st_mode) & 0o077):
            raise RuntimeError('管理锁文件不可信')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('另一个 R2 Relay 管理操作正在进行') from exc
        yield
    finally:
        os.close(fd)


def check_config(p):
    f = p['config']
    st = f.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        raise RuntimeError(f'配置必须是当前用户拥有且不允许组/其他人访问的普通文件：{f}')
    if not (p['config_dir'].stat().st_mode & 0o077) == 0:
        raise RuntimeError('配置目录权限应为 0700')
    # Do not parse/echo tokens. The runtime shell validates required values.


def link_target(path):
    if not path.is_symlink():
        return None
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError('版本链接损坏，拒绝操作') from exc


def installed_link(p, name):
    target = link_target(p[name])
    if target is None:
        return None
    if target.parent != p['releases'] or not SHA_PATTERN.fullmatch(target.name):
        raise RuntimeError(f'{name} 版本链接超出受管理的 release 目录')
    rel_sha(p, target.name)
    return target


def rel_sha(p, value):
    if not SHA_PATTERN.fullmatch(value):
        raise RuntimeError('必须使用完整40位小写 Commit SHA')
    dest = p['releases'] / value
    if (not dest.is_dir() or dest.is_symlink()
            or not (dest / '.venv/bin/mitmdump').is_file()
            or not (dest / 'local/addon_entry.py').is_file()
            or not (dest / '.deployment-commit').is_file()
            or (dest / '.deployment-commit').read_text().strip() != value):
        raise RuntimeError(f'目标版本未完整安装：{value}')
    return dest


def atomic_link(p, which, destination):
    link = p[which]
    tmp = link.with_name(link.name + f'.tmp-{os.getpid()}')
    try:
        tmp.symlink_to(destination)
        os.replace(tmp, link)
    finally:
        tmp.unlink(missing_ok=True)


def stage_release(repo, sha, p):
    if not SHA_PATTERN.fullmatch(sha):
        raise RuntimeError('必须使用完整40位小写 Commit SHA，不能使用 HEAD 或分支名')
    repo = repo.expanduser().resolve(strict=True)
    if not (repo / '.git').exists():
        raise RuntimeError(f'不是 Git 工作仓库：{repo}')
    dirty = run(['git', '-C', repo, 'status', '--porcelain'],
                capture_output=True, text=True).stdout
    if dirty.strip():
        raise RuntimeError('工作区有未提交改动，拒绝构建；先冻结并提交当前五并发代码')
    resolved = run(['git', '-C', repo, 'rev-parse', '--verify', f'{sha}^{{commit}}'],
                   capture_output=True, text=True).stdout.strip()
    if resolved != sha:
        raise RuntimeError('指定 SHA 不是可用的完整提交标识')
    dest = p['releases'] / sha
    if dest.exists() or dest.is_symlink():
        rel_sha(p, sha)
        print(f'RELEASE_ALREADY_PRESENT {sha}')
        return sha
    p['releases'].mkdir(parents=True, exist_ok=True, mode=0o700)
    if p['releases'].is_symlink():
        raise RuntimeError('Release 根目录不允许是符号链接')
    archive = run(['git', '-C', repo, 'archive', '--format=tar', sha],
                  capture_output=True).stdout
    # Extract first; create the venv only *after* moving the source to its final
    # absolute path. Otherwise pip/editable .pth and mitmdump shebangs break.
    with tempfile.TemporaryDirectory(prefix='.source-', dir=p['releases']) as temp:
        source = Path(temp) / 'source'
        source.mkdir()
        run(['tar', '-xf', '-', '-C', source], input=archive)
        if not (source / 'local/pyproject.toml').is_file() or not (source / 'local/addon_entry.py').is_file():
            raise RuntimeError('Git 版本缺少本地代理源码')
        if dest.exists() or dest.is_symlink():
            raise RuntimeError('目标 Release 在构建期间被其他操作创建')
        source.rename(dest)
    try:
        python = shutil.which('python3.11')
        if not python:
            raise RuntimeError('需要安装 Python 3.11')
        run([python, '-m', 'venv', dest / '.venv'])
        run([dest / '.venv/bin/python', '-m', 'pip', 'install', '-e', str(dest / 'local')])
        run([dest / '.venv/bin/python', '-m', 'pip', 'check'])
        run([dest / '.venv/bin/python', '-c',
             'import r2relay_local.relay; import r2relay_local.__main__'])
        if not (dest / '.venv/bin/mitmdump').is_file():
            raise RuntimeError('未找到 mitmdump 可执行文件')
        (dest / '.deployment-commit').write_text(sha + '\n')
    except BaseException:
        shutil.rmtree(dest)
        raise
    print(f'RELEASE_STAGED {sha}')
    return sha


def make_runner(p):
    # Shell script contains no secrets; the protected configuration is sourced at runtime.
    s = '''#!/bin/zsh
set -eu
umask 077
CONFIG="$HOME/.config/r2-relay/client.env"
CURRENT="$HOME/.local/share/r2-relay/current"
[[ -f "$CONFIG" && -O "$CONFIG" ]] || { print -u2 'R2 Relay 配置不存在或所有者错误'; exit 1; }
[[ ! -L "$CONFIG" ]] || { print -u2 'R2 Relay 配置不允许使用符号链接'; exit 1; }
[[ "$(/usr/bin/stat -f %Lp "$CONFIG")" == 600 ]] || { print -u2 'R2 Relay 配置权限必须为 600'; exit 1; }
set -a
source "$CONFIG"
set +a
[[ -n "${R2_RELAY_TOKEN:-}" && "$R2_RELAY_TOKEN" != CHANGE_ME ]] || { print -u2 'R2 Relay Token 未配置'; exit 1; }
[[ "${R2_RELAY_VPS_URL:-}" == https://* ]] || { print -u2 'R2 Relay VPS URL 必须使用 HTTPS'; exit 1; }
[[ -f "${R2_RELAY_VPS_CA_FILE:-}" ]] || { print -u2 'R2 Relay VPS CA 文件不存在'; exit 1; }
exec "$CURRENT/.venv/bin/mitmdump" \\
  --mode regular --listen-host 127.0.0.1 --listen-port 18585 \\
  --allow-hosts '.*\\.r2\\.cloudflarestorage\\.com(:443)?$' \\
  --set ssl_insecure=false --set flow_detail=0 \\
  -s "$CURRENT/local/addon_entry.py"
'''
    p['run'].write_text(s)
    p['run'].chmod(0o700)



def agent_definition(p):
    return {
        'Label': LABEL,
        'ProgramArguments': [str(p['run'])],
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'StandardOutPath': str(p['logs'] / 'stdout.log'),
        'StandardErrorPath': str(p['logs'] / 'stderr.log'),
    }


def validate_agent_file(p, path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise RuntimeError('LaunchAgent 定义不可信，拒绝覆盖或卸载')
    with path.open('rb') as f:
        if plistlib.load(f) != agent_definition(p):
            raise RuntimeError('LaunchAgent 定义与受管理模板不一致')


def enable_login_autostart(p):
    """Publish the plist only at explicit start; install alone must not auto-start at login."""
    validate_agent_file(p, p['plist_template'])
    if p['plist'].exists() or p['plist'].is_symlink():
        raise RuntimeError('LaunchAgent 文件已存在，拒绝覆盖')
    p['plist'].parent.mkdir(parents=True, exist_ok=True)
    temp = p['plist'].with_name(p['plist'].name + f'.tmp-{os.getpid()}')
    try:
        shutil.copyfile(p['plist_template'], temp)
        temp.chmod(0o600)
        os.replace(temp, p['plist'])
    finally:
        temp.unlink(missing_ok=True)


def disable_login_autostart(p):
    if p['plist'].exists() or p['plist'].is_symlink():
        validate_agent_file(p, p['plist'])
        p['plist'].unlink()


def install(args, p):
    if p['current'].exists() or p['current'].is_symlink():
        raise RuntimeError('已有 current 版本链接；拒绝覆盖现有部署')
    if p['plist'].exists() or p['plist'].is_symlink() or is_loaded():
        raise RuntimeError('已有 LaunchAgent，拒绝覆盖；请先核实旧安装')
    for name in ('cli', 'run', 'manager', 'plist_template'):
        if p[name].exists() or p[name].is_symlink():
            raise RuntimeError(f'已存在管理文件，拒绝覆盖：{p[name]}')
    sha = stage_release(args.repo, args.sha, p)
    p['bin'].mkdir(parents=True, exist_ok=True, mode=0o700)
    p['config_dir'].mkdir(parents=True, exist_ok=True, mode=0o700)
    p['config_dir'].chmod(0o700)
    p['logs'].mkdir(parents=True, exist_ok=True, mode=0o700)
    p['plist'].parent.mkdir(parents=True, exist_ok=True)
    if not p['config'].exists() and not p['config'].is_symlink():
        p['config'].write_text('''# 仅保存在 Mac 本机；勿提交 Git。五并发由调用方/已验证配置管理。
R2_RELAY_VPS_URL='https://104.224.168.192:8443'
R2_RELAY_TOKEN='CHANGE_ME'
R2_RELAY_VPS_CA_FILE="$HOME/.config/r2-relay/root.crt"
R2_RELAY_BUFFER_BUDGET='536870912'
R2_RELAY_HTTP2='1'
''')
        p['config'].chmod(0o600)
    check_config(p)
    make_runner(p)
    # Never rely on the original Git checkout for the installed CLI.
    p['manager'].write_bytes(Path(__file__).resolve().read_bytes())
    p['manager'].chmod(0o700)
    python = shutil.which('python3.11')
    if not python:
        raise RuntimeError('需要安装 Python 3.11')
    cmd = f'exec {shlex.quote(python)} {shlex.quote(str(p["manager"]))} "$@"\n'
    p['cli'].write_text('#!/bin/sh\n' + cmd)
    p['cli'].chmod(0o700)
    for name in ('stdout.log', 'stderr.log'):
        f = p['logs'] / name
        f.touch(exist_ok=True)
        f.chmod(0o600)
    # A staged install is inert even across logout/login. Only start()
    # publishes a plist in ~/Library/LaunchAgents.
    with p['plist_template'].open('wb') as f:
        plistlib.dump(agent_definition(p), f)
    p['plist_template'].chmod(0o600)
    atomic_link(p, 'current', rel_sha(p, sha))
    print(f'INSTALLED_NOT_STARTED {sha}')
    print(f'编辑 {p["config"]}，核对 CA；确认当前18585无上传后执行 r2-relay start')


def start(p):
    if is_loaded():
        raise RuntimeError('LaunchAgent 已加载，无需重复启动')
    check_config(p)
    if not p['plist_template'].exists() or installed_link(p, 'current') is None:
        raise RuntimeError('请先 install')
    if not port_available():
        raise RuntimeError('18585 已被其他进程占用；请先确认并安全关闭旧代理')
    enable_login_autostart(p)
    try:
        run(['launchctl', 'bootstrap', domain(), p['plist']])
        if not wait_managed_listener():
            raise RuntimeError('启动后未验证到受 launchd 管理的监听进程')
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        if is_loaded():
            run(['launchctl', 'bootout', service()], check=False)
        disable_login_autostart(p)
        print('WARNING: 启动失败，已移除登录自启定义；请检查 stderr.log', file=sys.stderr)
        raise
    print('SERVICE_STARTED')


def stop(p, *, confirm_idle=False):
    if not confirm_idle:
        raise RuntimeError('停止服务必须指定 --confirm-idle，确认全部上传已结束')
    if is_loaded():
        # Fail closed when a foreign or modified plist occupies our label.
        validate_agent_file(p, p['plist'])
        run(['launchctl', 'bootout', service()])
        disable_login_autostart(p)
        if not wait_port(should_listen=False):
            raise RuntimeError('服务已卸载，但18585仍被占用；请排查其他进程')
        print('SERVICE_STOPPED')
    else:
        disable_login_autostart(p)
        print('SERVICE_ALREADY_STOPPED')


def activate(p, sha, confirm_idle):
    if not confirm_idle:
        raise RuntimeError('必须显式提供 --confirm-idle，确认所有上传已结束')
    target = rel_sha(p, sha)
    old = installed_link(p, 'current')
    if old == target:
        print(f'ALREADY_ACTIVE {sha}')
        return
    loaded = is_loaded()
    if not loaded and not port_available():
        raise RuntimeError('检测到未受本服务管理的18585进程，拒绝切换')
    if loaded:
        stop(p, confirm_idle=confirm_idle)
    try:
        atomic_link(p, 'current', target)
        if loaded:
            start(p)
    except Exception:
        if is_loaded():
            run(['launchctl', 'bootout', service()], check=False)
        if old:
            atomic_link(p, 'current', old)
            if loaded:
                try:
                    start(p)
                    print('ROLLBACK_SERVICE_RESTORED', file=sys.stderr)
                except Exception as exc:
                    print(f'CRITICAL: 原版本启动失败：{type(exc).__name__}', file=sys.stderr)
        raise
    if old:
        atomic_link(p, 'previous', old)
    print(f'ACTIVATED {sha}')


def status(p):
    current = installed_link(p, 'current')
    previous = installed_link(p, 'previous')
    print('service_loaded=', is_loaded())
    print('login_autostart_enabled=', p['plist'].is_file() and not p['plist'].is_symlink())
    print('port_18585_listening=', not port_available())
    print('port_18585_managed=', managed_listener())
    print('current_sha=', current.name if current else 'NONE')
    print('previous_sha=', previous.name if previous else 'NONE')
    print('config_exists=', p['config'].exists())
    print('logs=', p['logs'])
    if is_loaded():
        run(['launchctl', 'print', service()], check=False)


def uninstall(p, confirm_idle):
    if not confirm_idle:
        raise RuntimeError('必须提供 --confirm-idle，避免中断正在上传的分片')
    for name in ('current', 'previous'):
        link = p[name]
        if link.exists() and not link.is_symlink():
            raise RuntimeError(f'版本链接不是符号链接，拒绝卸载：{link}')
    stop(p, confirm_idle=confirm_idle)
    p['plist_template'].unlink(missing_ok=True)
    p['cli'].unlink(missing_ok=True)
    p['run'].unlink(missing_ok=True)
    p['manager'].unlink(missing_ok=True)
    # Keep release directories and secret material, but remove activation
    # symlinks so a later clean install is possible.
    p['current'].unlink(missing_ok=True)
    p['previous'].unlink(missing_ok=True)
    print('UNINSTALLED; releases, config, CA and logs retained')


def main(argv=None):
    parser = argparse.ArgumentParser(description='R2 Relay macOS 用户级 LaunchAgent 管理器')
    sub = parser.add_subparsers(dest='command', required=True)
    installer = sub.add_parser('install', help='按固定 SHA 安装定义但不启动')
    installer.add_argument('--sha', required=True)
    installer.add_argument('--repo', type=Path, required=True)
    updater = sub.add_parser('update', help='按固定 SHA 预装，默认不切换')
    updater.add_argument('sha')
    updater.add_argument('--repo', type=Path, required=True)
    updater.add_argument('--activate', action='store_true')
    updater.add_argument('--confirm-idle', action='store_true')
    for name in ('start', 'stop', 'restart', 'status', 'logs', 'rollback', 'activate', 'uninstall'):
        cmd = sub.add_parser(name)
        if name in ('stop', 'restart', 'rollback', 'activate', 'uninstall'):
            cmd.add_argument('--confirm-idle', action='store_true')
        if name == 'activate':
            cmd.add_argument('sha')
    args = parser.parse_args(argv)
    ensure_macos()
    p = paths()
    lock = operation_lock(p) if args.command not in ('status', 'logs') else nullcontext()
    try:
        with lock:
            if args.command == 'install':
                install(args, p)
            elif args.command == 'update':
                sha = stage_release(args.repo, args.sha, p)
                if args.activate:
                    activate(p, sha, args.confirm_idle)
                else:
                    print('STAGED_ONLY: 无中断；上线请执行 r2-relay activate SHA --confirm-idle')
            elif args.command == 'activate':
                activate(p, args.sha, args.confirm_idle)
            elif args.command == 'rollback':
                previous = installed_link(p, 'previous')
                if not previous:
                    raise RuntimeError('没有上一个已记录版本')
                activate(p, previous.name, args.confirm_idle)
            elif args.command == 'start':
                start(p)
            elif args.command == 'stop':
                stop(p, confirm_idle=args.confirm_idle)
            elif args.command == 'restart':
                if not args.confirm_idle:
                    raise RuntimeError('请先确认无在途上传，并添加 --confirm-idle')
                stop(p, confirm_idle=True)
                start(p)
            elif args.command == 'status':
                status(p)
            elif args.command == 'logs':
                run(['tail', '-n', '100', '-f', p['logs'] / 'stderr.log'])
            elif args.command == 'uninstall':
                uninstall(p, args.confirm_idle)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
