#!/bin/bash
set -euo pipefail
umask 077
PATH=/usr/bin:/bin

if (( $# != 0 || UID == 0 )); then
    echo 'Run uninstall_web.sh in your own Terminal without sudo.' >&2
    exit 2
fi

label=local.boardhub-web
app="$HOME/Applications/BoardHubWeb.app"
plist="$HOME/Library/LaunchAgents/$label.plist"
runtime="$HOME/Library/Application Support/BoardHub"
for directory in "$HOME/Applications" "$HOME/Library" "$HOME/Library/LaunchAgents" "$HOME/Library/Application Support" "$runtime"; do
    if [[ -L "$directory" || ( -e "$directory" && ( ! -d "$directory" || ! -O "$directory" ) ) ]]; then
        echo "Unsafe directory: $directory" >&2
        exit 1
    fi
done
if [[ -L "$app" || -L "$plist" || -L "$runtime/web_url.txt" ]]; then
    echo 'A web installation path is a symbolic link.' >&2
    exit 1
fi
if [[ -e "$app" ]] && [[ ! -d "$app" || ! -O "$app" || \
    $(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist") != "$label.app" ]]; then
    echo "Unexpected app at $app" >&2
    exit 1
fi
if [[ -e "$plist" ]] && [[ ! -f "$plist" || ! -O "$plist" || \
    $(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist") != "$label" ]]; then
    echo "Unexpected LaunchAgent at $plist" >&2
    exit 1
fi

if /bin/launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    /bin/launchctl bootout "gui/$UID/$label"
fi
if [[ -d "$app" ]]; then
    /usr/bin/osascript -l JavaScript - "$label.app" "$app" <<'JXA'
ObjC.import("AppKit");
function run(argv) {
    const apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier(argv[0]);
    const owned = [];
    for (let i = 0; i < apps.count; i++) {
        const app = apps.objectAtIndex(i);
        if (ObjC.unwrap(app.bundleURL.path) === argv[1]) {
            app.terminate;
            owned.push(app);
        }
    }
    for (let i = 0; i < 50 && owned.some((app) => !app.terminated); i++) {
        $.NSThread.sleepForTimeInterval(0.1);
    }
    if (owned.some((app) => !app.terminated)) throw Error("BoardHub Web is still running.");
}
JXA
    rm -rf -- "$app"
fi
rm -f -- "$plist" "$runtime/web_url.txt"
echo 'BoardHub Web has been removed. Runtime history remains available.'
