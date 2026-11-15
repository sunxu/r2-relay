#!/usr/bin/env python3
"""Manage the user-scoped R2 Relay LaunchAgent on macOS.

No operation uses sudo, handles R2 upload traffic, or changes the VPS.
"""
from __future__ import annotations

import argparse
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


def check_config(p):
    f = p['config']
    st = f.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise RuntimeError(f'配置必须是当前用户拥有且不允许组/其他人访问的普通文件：{f}')
    if not (p['config_dir'].stat().st_mode & 0o077) == 0:
        raise RuntimeError('配置目录权限应为 0700')
    # Do not parse/echo tokens. The runtime shell validates required values.


def link_target(path):
    if not path.is_symlink():
        return None
    return path.resolve(strict=True)


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
    repo = repo.expanduser().resolve(strict=True)
    if not (repo / '.git').exists():
        raise RuntimeError(f'不是 Git 工作仓库：{repo}')
    dirty = run(['git', '-C', repo, 'status', '--porcelain'], capture_output=True, text=True).stdout
    if dirty.strip():
        raise RuntimeError('工作区有未提交改动，拒绝构建以免丢失已验证的并发优化；请先整理和提交')
    sha = run(['git', '-C', repo, 'rev-parse', '--verify', f'{sha}^{{commit}}'], capture_output=True, text=True).stdout.strip()
    if not SHA_PATTERN.fullmatch(sha):
        raise RuntimeError('无法解析完整 Commit SHA')
    dest = p['releases'] / sha
    if dest.exists():
        rel_sha(p, sha)
        print(f'RELEASE_ALREADY_PRESENT {sha}')
        return sha
    p['releases'].mkdir(parents=True, exist_ok=True, mode=0o700)
    # Clone-free, pinned-tree deployment: never modify the active working checkout.
    archive = run(['git', '-C', repo, 'archive', '--format=tar', sha], capture_output=True).stdout
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=p['releases']) as temp:
        stage = Path(temp)
        run(['tar', '-xf', '-', '-C', stage], input=archive)
        python = shutil.which('python3.11')
        if not python:
            if sys.version_info >= (3, 11):
                python = sys.executable
            else:
                raise RuntimeError('需要 Python 3.11+，请安装 python3.11')
        run([python, '-m', 'venv', stage / '.venv'])
        run([stage / '.venv/bin/python', '-m', 'pip', 'install', '-e', str(stage / 'local')])
        run([stage / '.venv/bin/python', '-m', 'pip', 'check'])
        run([stage / '.venv/bin/python', '-c', 'import r2relay_local.relay; import r2relay_local.__main__'])
        if not (stage / '.venv/bin/mitmdump').is_file():
            raise RuntimeError('未找到 mitmdump 可执行文件')
        (stage / '.deployment-commit').write_text(sha + '\n')
        stage.rename(dest)
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


def install(args, p):
    if p['current'].exists() or p['current'].is_symlink():
        raise RuntimeError('已有 current 版本链接；拒绝覆盖现有部署')
    if p['plist'].exists():
        raise RuntimeError('已有 LaunchAgent，拒绝覆盖；请使用 update/activate，或先核实旧安装')
    sha = stage_release(args.repo, args.sha, p)
    p['bin'].mkdir(parents=True, exist_ok=True, mode=0o700)
    p['config_dir'].mkdir(parents=True, exist_ok=True, mode=0o700)
    p['config_dir'].chmod(0o700)
    p['logs'].mkdir(parents=True, exist_ok=True, mode=0o700)
    p['plist'].parent.mkdir(parents=True, exist_ok=True)
    if not p['config'].exists():
        p['config'].write_text('''# 仅保存在 Mac 本机。修改后需 restart；勿提交 Git。
R2_RELAY_VPS_URL='https://104.224.168.192:8443'
R2_RELAY_TOKEN='CHANGE_ME'
R2_RELAY_VPS_CA_FILE="$HOME/.config/r2-relay/root.crt"
R2_RELAY_BUFFER_BUDGET='536870912'
R2_RELAY_HTTP2='1'
''')
        p['config'].chmod(0o600)
    check_config(p)
    make_runner(p)
    manager = Path(__file__).resolve()
    cmd = f'exec {shlex.quote(sys.executable)} {shlex.quote(str(manager))} "$@"\n'
    p['cli'].write_text('#!/bin/sh\n' + cmd)
    p['cli'].chmod(0o700)
    for name in ('stdout.log', 'stderr.log'):
        f = p['logs'] / name
        f.touch(exist_ok=True)
        f.chmod(0o600)
    data = {
        'Label': LABEL,
        'ProgramArguments': [str(p['run'])],
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'StandardOutPath': str(p['logs'] / 'stdout.log'),
        'StandardErrorPath': str(p['logs'] / 'stderr.log'),
    }
    with p['plist'].open('wb') as f:
        plistlib.dump(data, f)
    atomic_link(p, 'current', rel_sha(p, sha))
    print(f'INSTALLED_NOT_STARTED {sha}')
    print(f'编辑 {p["config"]}，核对 CA；确认当前18585无上传后执行 r2-relay start')


def start(p):
    if is_loaded():
        raise RuntimeError('LaunchAgent 已加载，无需重复启动')
    check_config(p)
    if not p['plist'].exists() or link_target(p['current']) is None:
        raise RuntimeError('请先 install')
    if not port_available():
        raise RuntimeError('18585 已被其他进程占用；请先确认并安全关闭旧代理')
    run(['launchctl', 'bootstrap', domain(), p['plist']])
    if not wait_port(should_listen=True):
        print('WARNING: launchctl 已加载，但18585未监听，请检查 stderr.log', file=sys.stderr)
        raise RuntimeError('启动后未检测到监听端口')
    print('SERVICE_STARTED')


def stop(p):
    if not is_loaded():
        print('SERVICE_ALREADY_STOPPED')
        return
    run(['launchctl', 'bootout', service()])
    if not wait_port(should_listen=False):
        raise RuntimeError('服务已卸载，但18585仍被占用；请排查其他进程')
    print('SERVICE_STOPPED')


def activate(p, sha, confirm_idle):
    if not confirm_idle:
        raise RuntimeError('必须显式提供 --confirm-idle，确认所有上传已结束')
    target = rel_sha(p, sha)
    old = link_target(p['current'])
    if old == target:
        print(f'ALREADY_ACTIVE {sha}')
        return
    loaded = is_loaded()
    if not loaded and not port_available():
        raise RuntimeError('检测到未受本服务管理的18585进程，拒绝切换')
    if loaded:
        stop(p)
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
    current = link_target(p['current'])
    previous = link_target(p['previous'])
    print('service_loaded=', is_loaded())
    print('port_18585_listening=', not port_available())
    print('current_sha=', current.name if current else 'NONE')
    print('previous_sha=', previous.name if previous else 'NONE')
    print('config_exists=', p['config'].exists())
    print('logs=', p['logs'])
    if is_loaded():
        run(['launchctl', 'print', service()], check=False)


def uninstall(p, confirm_idle):
    if not confirm_idle:
        raise RuntimeError('必须提供 --confirm-idle，避免中断正在上传的分片')
    if is_loaded():
        stop(p)
    p['plist'].unlink(missing_ok=True)
    p['cli'].unlink(missing_ok=True)
    p['run'].unlink(missing_ok=True)
    print('UNINSTALLED service definitions only; releases, config, CA and logs retained')


def main(argv=None):
    parser = argparse.ArgumentParser(description='R2 Relay macOS 用户级 LaunchAgent 管理器')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('install', 'update'):
        cmd = sub.add_parser(name, help='按固定 SHA 预安装；不会中断当前服务')
        cmd.add_argument('sha' if name == 'update' else '--sha', default='HEAD' if name == 'install' else None)
        cmd.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
        if name == 'update':
            cmd.add_argument('--activate', action='store_true')
            cmd.add_argument('--confirm-idle', action='store_true')
    for name in ('start', 'stop', 'restart', 'status', 'logs', 'rollback', 'activate', 'uninstall'):
        cmd = sub.add_parser(name)
        if name in ('restart', 'rollback', 'activate', 'uninstall'):
            cmd.add_argument('--confirm-idle', action='store_true')
        if name == 'activate':
            cmd.add_argument('sha')
    args = parser.parse_args(argv)
    ensure_macos()
    p = paths()
    try:
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
            previous = link_target(p['previous'])
            if not previous:
                raise RuntimeError('没有上一个已记录版本')
            activate(p, previous.name, args.confirm_idle)
        elif args.command == 'start':
            start(p)
        elif args.command == 'stop':
            stop(p)
        elif args.command == 'restart':
            if not args.confirm_idle:
                raise RuntimeError('请先确认无在途上传，并添加 --confirm-idle')
            stop(p)
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
