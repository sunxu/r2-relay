#!/usr/bin/env python3
"""One-file HTTPS control repair bootstrap; preserves previous backups."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.request import urlopen

REPO_FILE="ops/r2_https_ip_control.py"
TARGET=Path("/usr/local/libexec/r2_https_ip_control.py")
BACKUP_ROOT=Path("/var/backups/vps-mcp")
SHA=re.compile(r"[0-9a-f]{40}\\Z")

def install(commit,apply=False):
    if os.geteuid()!=0:
        raise ValueError("root_required")
    if not SHA.fullmatch(commit):
        raise ValueError("full_sha_required")
    if TARGET.is_symlink() or not TARGET.is_file() or TARGET.stat().st_uid!=0:
        raise ValueError("unsafe_target")
    if BACKUP_ROOT.is_symlink() or not BACKUP_ROOT.is_dir():
        raise ValueError("backup_root_missing")
    url="https://raw.githubusercontent.com/sunxu/r2-relay/"+commit+"/"+REPO_FILE
    with urlopen(url,timeout=25) as res:
        data=res.read(200001)
    if len(data)>200000:
        raise ValueError("file_too_large")
    compile(data,REPO_FILE,"exec")
    if "https_start_stage_" not in data.decode("utf-8"):
        raise ValueError("not_readiness_fix")
    if not apply:
        return {"status":"HTTPS_FIX_DRY_RUN_PASS","target":str(TARGET),"modified":False}
    # Unique, non-overwriting backup. Never delete backups, restart daemon, or alter 443.
    backup=Path(tempfile.mkdtemp(prefix="r2-https-readiness-",dir=BACKUP_ROOT))
    os.chmod(backup,0o700)
    old=backup/"r2_https_ip_control.py"
    shutil.copy2(TARGET,old)
    fd,name=tempfile.mkstemp(prefix=".r2-https-",dir=TARGET.parent)
    try:
        with os.fdopen(fd,"wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chown(name,0,0)
        os.chmod(name,0o644)
        os.replace(name,TARGET)
    finally:
        if os.path.exists(name): os.unlink(name)
    return {"status":"HTTPS_FIX_APPLIED","backup_dir":str(backup),
            "commit":commit,"services_restarted":False,
            "sha256":hashlib.sha256(data).hexdigest()}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--commit",required=True)
    p.add_argument("--apply",action="store_true")
    a=p.parse_args()
    try:
        result=install(a.commit,a.apply)
    except Exception as exc:
        result={"status":"FAILED","error":type(exc).__name__}
    print(json.dumps(result))
    return 0 if result["status"] in ("HTTPS_FIX_DRY_RUN_PASS","HTTPS_FIX_APPLIED") else 1

if __name__=="__main__":
    raise SystemExit(main())
