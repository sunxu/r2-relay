#!/usr/bin/env bash
# Isolated Mac workspace for PR #82. Never edits the production config/proxy.
set -euo pipefail
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
mode="${1:-setup}"
if (( $# > 0 )); then shift; fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
expected_root="$HOME/workspace/r2-relay-local-range-test"
branch="feat/local-httpx-range-backend"
[[ "$root" == "$expected_root" ]] ||
    fail "Use a separate clone at $expected_root (refusing to touch another checkout)"
[[ "$(git -C "$root" symbolic-ref --quiet --short HEAD)" == "$branch" ]] ||
    fail "This workspace must be checked out on $branch"

private_dir="$HOME/.local/share/r2-relay/private/local-range-acceptance"
config="$private_dir/config.toml"
cache_dir="$HOME/.cache/r2-relay/local-range-acceptance/downloads"
venv="$root/.venv"

validate_config() {
    [[ -f "$config" && ! -L "$config" ]] || fail "Missing private, regular TOML file"
    [[ -x "$venv/bin/python" ]] || fail "Missing isolated Python environment"
    R2_RELAY_CONFIG="$config" "$venv/bin/python" - "$cache_dir" <<'PY'
import sys
from pathlib import Path
from r2relay_local.config import load_settings
s = load_settings()
target = Path(sys.argv[1])
if not (s.download_enabled and s.download_backend == "local"
        and s.download_temp_dir == target
        and s.download_local_chunk_bytes == 2 * 1024 * 1024
        and s.download_local_max_connections_per_get == 8
        and s.download_local_global_max_connections == 128
        and s.download_max_temp_bytes == 64 * 1024 * 1024):
    raise SystemExit("Local acceptance config does not match isolated limits")
print("CONFIG_OK: local / 2 MiB / 8 per GET / 128 global / 64 MiB temp")
PY
}

smoke() {
    validate_config
    (
        cd "$root/local"
        # Unit tests use their own settings/fixtures; do not let the
        # acceptance-only Local config accidentally initialize a real
        # downloader (and acquire its exclusive cache lock).
        env -u R2_RELAY_CONFIG -u R2_RELAY_ENABLE_ADDON \
            -u R2_RELAY_TOKEN -u R2_RELAY_VPS_URL -u R2_RELAY_VPS_CA_FILE \
            "$venv/bin/python" -m pytest -q \
            tests/test_config.py tests/test_addon.py \
            tests/test_local_range.py tests/test_real_r2_download_acceptance.py
    )
}

case "$mode" in
    setup)
        (( $# == 0 )) || fail "setup takes no arguments"
        for dir in "$private_dir" "$cache_dir"; do
            [[ ! -L "$dir" ]] || fail "Refusing symlink directory: $dir"
            mkdir -p "$dir"
            chmod 700 "$dir"
        done
        if [[ -e "$config" || -L "$config" ]]; then
            [[ -f "$config" && ! -L "$config" ]] ||
                fail "Refusing to overwrite non-regular/symlink config"
            printf 'Reusing existing private config (will validate; not overwrite).\n'
        else
            # The user profile path is embedded as a TOML quoted string.
            [[ "$cache_dir" != *\"* && "$cache_dir" != *\\* && "$cache_dir" != *$'\n'* ]] ||
                fail "Unsupported home directory path characters"
            cat > "$config" <<TOML
schema_version = 1

[download]
enabled = true
backend = "local"
max_temp_mib = 64
temp_dir = "$cache_dir"

[download.local]
chunk_size_mib = 2
min_size_mib = 2
max_connections_per_get = 8
global_max_connections = 128
TOML
            chmod 600 "$config"
        fi
        if [[ ! -x "$venv/bin/python" ]]; then
            if command -v python3.11 >/dev/null 2>&1; then
                py="python3.11"
            else
                py="python3"
            fi
            "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
                fail "Python >= 3.11 is required"
            "$py" -m venv "$venv"
        fi
        "$venv/bin/python" -m pip install --disable-pip-version-check \
            -e "$root/local[test]" -e "$root/vps[test]"
        smoke
        printf '\nWORKSPACE_READY\n'
        printf 'code: %s\nconfig: %s\ncache: %s\npython: %s\n' \
            "$root" "$config" "$cache_dir" "$venv/bin/python"
        printf 'The private signed-GET spec is NOT created or copied.\n'
        printf 'No proxy, R2 request, IOTA, VPS service, or production config was changed.\n'
        ;;
    smoke)
        (( $# == 0 )) || fail "smoke takes no arguments"
        smoke
        ;;
    offline)
        (( $# == 1 )) || fail "Usage: $0 offline /absolute/path/to/r2_local_range_offline_test_data.zip"
        archive="$1"
        [[ "$archive" = /* && -f "$archive" && ! -L "$archive" ]] ||
            fail "Pass the synthetic offline ZIP as a regular file by absolute path"
        validate_config
        (
            cd "$root/local"
            # Explicitly do not load private request samples, tokens, or
            # workstation production/Local config during offline Mock tests.
            env -u R2_RELAY_CONFIG -u R2_RELAY_REAL_GET_SPEC \
                -u R2_RELAY_ENABLE_ADDON -u R2_RELAY_TOKEN \
                -u R2_RELAY_VPS_URL -u R2_RELAY_VPS_CA_FILE \
                R2_LOCAL_RANGE_OFFLINE_ZIP="$archive" \
                "$venv/bin/python" -m pytest -q tests/test_local_range.py \
                -k optional_offline_archive_real_shape_and_sha256
        )
        ;;
    real)
        (( $# == 1 )) || fail "Usage: $0 real /absolute/path/to/existing-private-0600.json"
        spec="$1"
        [[ "$spec" = /* && -f "$spec" && ! -L "$spec" ]] ||
            fail "Pass an existing, regular private request spec via an absolute path"
        validate_config
        # The existing acceptance script checks the private spec is mode 0600.
        cd "$root"
        env -u R2_RELAY_ENABLE_ADDON -u R2_RELAY_TOKEN -u R2_RELAY_VPS_URL -u R2_RELAY_VPS_CA_FILE \
            R2_RELAY_CONFIG="$config" R2_RELAY_REAL_GET_SPEC="$spec" \
            "$venv/bin/python" local/poc/real_r2_download_acceptance.py
        ;;
    *)
        fail "Usage: $0 {setup|smoke|offline /absolute/synthetic.zip|real /absolute/private/0600.json}"
        ;;
esac
