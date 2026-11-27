#!/bin/bash
set -euo pipefail
umask 077
PATH=/usr/bin:/bin

if (( $# != 0 || UID == 0 )); then
    echo 'Run install_web.sh in your own Terminal without sudo. Set BOARDHUB_WEB_PORT or BOARDHUB_WEB_NAME to customize.' >&2
    exit 2
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ${source_dir##*/} == hub && $(basename "$(dirname "$source_dir")") == bridge ]]; then
    board_dir="$(dirname "$source_dir")/board"
else
    board_dir="${BOARDHUB_BOARD_DIR:-$HOME/boardhub/board}"
fi
runtime="$HOME/Library/Application Support/BoardHub"
applications="$HOME/Applications"
agents="$HOME/Library/LaunchAgents"
app="$applications/BoardHubWeb.app"
label=local.boardhub-web
plist="$agents/$label.plist"
port=${BOARDHUB_WEB_PORT:-8765}
name=${BOARDHUB_WEB_NAME:-user}
[[ -d "$board_dir" && ! -L "$board_dir" ]] || { echo "Board unavailable: $board_dir" >&2; exit 1; }
for directory in "$HOME/Library" "$HOME/Library/Application Support" "$applications" "$agents" "$runtime"; do
    if [[ -L "$directory" || ( -e "$directory" && ( ! -d "$directory" || ! -O "$directory" ) ) ]]; then
        echo "Unsafe directory: $directory" >&2
        exit 1
    fi
done
for file in "$runtime/web.log" "$runtime/web_url.txt"; do
    if [[ -L "$file" || ( -e "$file" && ( ! -f "$file" || ! -O "$file" ) ) ]]; then
        echo "Unsafe file: $file" >&2
        exit 1
    fi
done
mkdir -p "$applications" "$agents" "$runtime"
chmod 700 "$runtime"

stage=$(mktemp -d "$applications/.boardhub-web.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
cat > "$stage/Web.applescript" <<'APPLESCRIPT'
use scripting additions

property webPID : ""
property scriptPath : ""

on run
    if webPID is not "" then return
    set scriptPath to POSIX path of (path to me) & "Contents/Resources/boardhub-web/start.sh"
    set webPID to do shell script "umask 077; /bin/bash " & quoted form of scriptPath & " </dev/null >/dev/null 2>&1 & echo $!"
end run

on idle
    if webPID is "" then quit
    try
        do shell script "/bin/kill -0 " & quoted form of webPID
    on error
        quit
    end try
    return 5
end idle

on quit
    if webPID is not "" then
        try
            set executablePath to POSIX path of (path to me) & "Contents/Resources/boardhub-web/board_web.py"
            set observedCommand to do shell script "/bin/ps -p " & quoted form of webPID & " -o command="
            if observedCommand contains executablePath or observedCommand contains scriptPath then
                do shell script "/bin/kill -TERM " & quoted form of webPID
            end if
        end try
    end if
    continue quit
end quit
APPLESCRIPT

staged_app="$stage/BoardHubWeb.app"
/usr/bin/osacompile -s -o "$staged_app" "$stage/Web.applescript"
resources="$staged_app/Contents/Resources/boardhub-web"
mkdir "$resources"
for file in board_web.py board_web_data.py session_discovery.py claude_transcript.py board_web.html board_web.css board_web.js \
            board_lint.py board_state.py board_watch.py wake_routes.py; do
    install -m 600 "$source_dir/$file" "$resources/$file"
done
{
    printf '#!/bin/bash\nset -euo pipefail\numask 077\n'
    printf 'exec >>%q 2>&1\n' "$runtime/web.log"
    printf 'exec /usr/bin/python3 -B -I %q --board %q --runtime-dir %q --port %q --name %q\n' \
        "$app/Contents/Resources/boardhub-web/board_web.py" "$board_dir" "$runtime" "$port" "$name"
} > "$resources/start.sh"
chmod 700 "$resources/start.sh"
/usr/bin/python3 -B -I - "$stage/$label.plist" "$label" "$app" "$staged_app" <<'PY'
from pathlib import Path
import plistlib
import sys

info = Path(sys.argv[4]) / "Contents" / "Info.plist"
properties = plistlib.loads(info.read_bytes())
properties.update(CFBundleIdentifier=sys.argv[2] + ".app", LSUIElement=True)
info.write_bytes(plistlib.dumps(properties))
with open(sys.argv[1], "wb") as target:
    plistlib.dump({
        "Label": sys.argv[2],
        "ProgramArguments": ["/usr/bin/open", "-W", "-a", sys.argv[3]],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
    }, target)
PY
/usr/bin/codesign --force --sign - "$staged_app"

# Build and sign the replacement before stopping an existing web app.
if [[ -e "$app" || -L "$app" || -e "$plist" || -L "$plist" ]]; then
    /bin/bash "$source_dir/uninstall_web.sh"
fi
mv -- "$staged_app" "$app"
install -m 600 "$stage/$label.plist" "$plist"
/bin/launchctl bootstrap "gui/$UID" "$plist"
printf 'BoardHub Web installed. Open the local URL stored in %s/web_url.txt.\n' "$runtime"
