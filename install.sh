#!/bin/bash
set -euo pipefail
umask 077
PATH=/usr/bin:/bin

usage() {
    cat <<'EOF'
Usage: bash install.sh [--board PATH] [--runtime-dir PATH] [--human ROLE]

Environment overrides:
  BOARDHUB_BOARD_DIR    Board directory (default: $HOME/boardhub/board)
  BOARDHUB_RUNTIME_DIR  Private APFS runtime directory (default: $HOME/Library/Application Support/BoardHub)
  BOARDHUB_HUMAN        Human approver role name (default: owner)
EOF
}

if (( UID == 0 || EUID == 0 )); then
    echo 'Run install.sh in your own Terminal without sudo.' >&2
    exit 1
fi
if [[ -z ${HOME:-} || -L "$HOME" || ! -d "$HOME" || ! -O "$HOME" ]]; then
    echo 'HOME must be set to an existing non-symlink directory owned by the current user.' >&2
    exit 1
fi

board_dir="${BOARDHUB_BOARD_DIR:-$HOME/boardhub/board}"
runtime_dir="${BOARDHUB_RUNTIME_DIR:-$HOME/Library/Application Support/BoardHub}"
human_role="${BOARDHUB_HUMAN:-owner}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --board)
            [[ $# -ge 2 ]] || { echo '--board requires a path argument.' >&2; exit 2; }
            board_dir="$2"
            shift 2
            ;;
        --runtime-dir)
            [[ $# -ge 2 ]] || { echo '--runtime-dir requires a path argument.' >&2; exit 2; }
            runtime_dir="$2"
            shift 2
            ;;
        --human)
            [[ $# -ge 2 ]] || { echo '--human requires a role argument.' >&2; exit 2; }
            human_role="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "$human_role" =~ ^[a-z0-9_]{1,32}$ ]] || [[ "$human_role" =~ ^(all|none|claude|claude_main|claude_side|codex|gpt|gemini|dsh)$ ]]; then
    echo "Invalid human role name: $human_role" >&2
    exit 2
fi
if [[ -L "$board_dir" || ! -d "$board_dir" ]]; then
    echo "Board directory must exist and not be a symlink: $board_dir (pass --board or set BOARDHUB_BOARD_DIR)." >&2
    exit 1
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
label=local.boardhub-watch
agents_dir="$HOME/Library/LaunchAgents"
agent="$agents_dir/$label.plist"

for dir in "$HOME/Library" "$HOME/Library/Application Support" "$runtime_dir" "$agents_dir"; do
    if [[ -L "$dir" || ( -e "$dir" && ! -d "$dir" ) ]]; then
        echo "Unsafe directory path: $dir" >&2
        exit 1
    fi
done
if [[ -e "$runtime_dir" && ! -O "$runtime_dir" ]]; then
    echo "The runtime directory must be owned by the current user: $runtime_dir" >&2
    exit 1
fi

# Verify runtime directory parent lives on APFS rather than exFAT.
mount_probe="$runtime_dir"
while [[ ! -e "$mount_probe" ]]; do
    mount_probe=$(dirname -- "$mount_probe")
done
mount_dev=$(df -P "$mount_probe" | awk 'NR==2 {print $1}')
if ! /sbin/mount | awk -v dev="$mount_dev" '$1 == dev && /\(apfs[,)]/ { found = 1 } END { exit !found }'; then
    echo "BoardHub runtime directory must reside on APFS." >&2
    exit 1
fi

for file in \
    "$runtime_dir/board_lint.py" \
    "$runtime_dir/board_state.py" \
    "$runtime_dir/wake_routes.py" \
    "$runtime_dir/board_watch.py" \
    "$runtime_dir/board_chat_v2.py" \
    "$runtime_dir/manifest.json" \
    "$runtime_dir/baseline.json" \
    "$runtime_dir/housekeeping.json" \
    "$runtime_dir/routes.json" \
    "$runtime_dir/registry.json" \
    "$runtime_dir/wake_ledger.json" \
    "$runtime_dir/.wake_ledger.initialized" \
    "$runtime_dir/mail_ledger.json" \
    "$runtime_dir/mail_pending.json" \
    "$runtime_dir/health.json" \
    "$runtime_dir/last_published.json" \
    "$runtime_dir/watch.lock" \
    "$runtime_dir/watch.log" \
    "$agent"
do
    if [[ -L "$file" || ( -e "$file" && ! -f "$file" ) ]]; then
        echo "Unsafe file path: $file" >&2
        exit 1
    fi
    if [[ -e "$file" && ! -O "$file" ]]; then
        echo "Managed file must be owned by current user: $file" >&2
        exit 1
    fi
done

# Preserve the immutability and delivery history of an existing installation.
if [[ -e "$runtime_dir/manifest.json" || -e "$runtime_dir/baseline.json" || -e "$runtime_dir/last_published.json" ]]; then
    for name in manifest.json baseline.json wake_ledger.json mail_ledger.json; do
        if [[ ! -f "$runtime_dir/$name" ]]; then
            echo "Restore missing runtime state before upgrading: $runtime_dir/$name" >&2
            exit 1
        fi
    done
fi

jobs=$(launchctl list)
if awk -v label="$label" '$3 == label { found = 1 } END { exit !found }' <<< "$jobs"; then
    launchctl bootout "gui/$UID/$label"
fi

mkdir -p "$runtime_dir" "$agents_dir"
chmod 700 "$runtime_dir"

install -m 600 "$source_dir/board_lint.py" "$runtime_dir/board_lint.py"
install -m 600 "$source_dir/board_state.py" "$runtime_dir/board_state.py"
install -m 600 "$source_dir/wake_routes.py" "$runtime_dir/wake_routes.py"
install -m 700 "$source_dir/board_watch.py" "$runtime_dir/board_watch.py"
install -m 700 "$source_dir/board_chat_v2.py" "$runtime_dir/board_chat_v2.py"

tmp_plist=$(mktemp "$agents_dir/.${label}.plist.XXXXXX")
trap 'rm -f -- "$tmp_plist"' EXIT
/usr/bin/python3 -I - "$source_dir/$label.plist" "$tmp_plist" "$runtime_dir" "$board_dir" "$human_role" <<'PY'
import plistlib
import sys
from pathlib import Path

src, dst, rt, bd, human = sys.argv[1:]
with open(src, "rb") as f:
    doc = plistlib.load(f)
replacements = {
    "__BOARDHUB_RUNTIME_DIR__": rt,
    "__BOARDHUB_BOARD_DIR__": bd,
    "__BOARDHUB_HUMAN__": human,
}
args = []
for item in doc["ProgramArguments"]:
    for key, val in replacements.items():
        item = item.replace(key, val)
    args.append(item)
doc["ProgramArguments"] = args
with open(dst, "wb") as f:
    plistlib.dump(doc, f, fmt=plistlib.FMT_XML, sort_keys=False)
PY
chmod 600 "$tmp_plist"
mv -f "$tmp_plist" "$agent"
trap - EXIT

if [[ ! -e "$runtime_dir/manifest.json" ]]; then
    /usr/bin/python3 -I "$runtime_dir/board_watch.py" \
        --board "$board_dir" \
        --runtime-dir "$runtime_dir" \
        --human "$human_role" \
        --bootstrap
fi

launchctl load "$agent"
launchctl list | awk -v label="$label" '$3 == label { print; found = 1 } END { exit !found }'
shasum -a 256 \
    "$runtime_dir/board_lint.py" \
    "$runtime_dir/board_state.py" \
    "$runtime_dir/wake_routes.py" \
    "$runtime_dir/board_watch.py" \
    "$runtime_dir/board_chat_v2.py"
