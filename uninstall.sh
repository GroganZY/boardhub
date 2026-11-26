#!/bin/bash
set -euo pipefail
umask 077
PATH=/usr/bin:/bin

usage() {
    cat <<'EOF'
Usage: bash uninstall.sh [--runtime-dir PATH]

Environment overrides:
  BOARDHUB_RUNTIME_DIR  Private runtime directory (default: $HOME/Library/Application Support/BoardHub)
EOF
}

if (( UID == 0 || EUID == 0 )); then
    echo 'Run uninstall.sh in your own Terminal without sudo.' >&2
    exit 1
fi
if [[ -z ${HOME:-} || -L "$HOME" || ! -d "$HOME" || ! -O "$HOME" ]]; then
    echo 'HOME must be set to an existing non-symlink directory owned by the current user.' >&2
    exit 1
fi

runtime_dir="${BOARDHUB_RUNTIME_DIR:-$HOME/Library/Application Support/BoardHub}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime-dir)
            [[ $# -ge 2 ]] || { echo '--runtime-dir requires a path argument.' >&2; exit 2; }
            runtime_dir="$2"
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

label=local.boardhub-watch
agents_dir="$HOME/Library/LaunchAgents"
agent="$agents_dir/$label.plist"

for dir in "$HOME/Library" "$HOME/Library/Application Support" "$runtime_dir" "$agents_dir"; do
    if [[ -L "$dir" || ( -e "$dir" && ! -d "$dir" ) ]]; then
        echo "Unsafe directory path: $dir" >&2
        exit 1
    fi
done

for file in \
    "$runtime_dir/board_lint.py" \
    "$runtime_dir/board_state.py" \
    "$runtime_dir/wake_routes.py" \
    "$runtime_dir/board_watch.py" \
    "$runtime_dir/board_chat_v2.py" \
    "$agent"
do
    if [[ -L "$file" || ( -e "$file" && ! -f "$file" ) ]]; then
        echo "Unsafe file path: $file" >&2
        exit 1
    fi
done

jobs=$(launchctl list)
if awk -v label="$label" '$3 == label { found = 1 } END { exit !found }' <<< "$jobs"; then
    launchctl bootout "gui/$UID/$label"
fi

rm -f -- \
    "$agent" \
    "$runtime_dir/board_lint.py" \
    "$runtime_dir/board_state.py" \
    "$runtime_dir/wake_routes.py" \
    "$runtime_dir/board_watch.py" \
    "$runtime_dir/board_chat_v2.py"

echo "Unloaded $label. Runtime manifest, ledgers, receipts, and logs in $runtime_dir were preserved."
