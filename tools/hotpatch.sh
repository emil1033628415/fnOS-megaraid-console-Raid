#!/bin/bash
# Hot-patch an installed megaraid-console with the sources in this repository.
#
# This is a development aid, not the release path: it overwrites app/server and
# app/ui in place so a change can be exercised without rebuilding an fpk. The
# normal upgrade is `fnpack build -d .` followed by installing the fpk.
#
# Must run as root: the app runs as root and its files are root-owned.
# It never calls storcli, so it cannot trigger a rebuild or any state change.
#
#   sudo ./tools/hotpatch.sh              # install and restart
#   sudo ./tools/hotpatch.sh --no-restart # install only
#   sudo ./tools/hotpatch.sh --dry-run    # print the plan
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP_WRAPPER="/var/apps/megaraid-console"
DO_RESTART=1
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --no-restart) DO_RESTART=0 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run this as root (sudo $0)" >&2
    exit 1
fi
[ -d "$APP_WRAPPER" ] || { echo "error: $APP_WRAPPER not found -- is the app installed?" >&2; exit 1; }

APPDEST="$(readlink -f "$APP_WRAPPER/target")"
PKGETC="$(readlink -f "$APP_WRAPPER/etc")"
PKGVAR="$(readlink -f "$APP_WRAPPER/var")"
MANIFEST="$APP_WRAPPER/manifest"

echo "installed tree : $APPDEST"
echo "config         : $PKGETC"
echo "runtime data   : $PKGVAR"

[ -f "$APPDEST/server/raidwatchd.py" ] || { echo "error: unexpected layout at $APPDEST" >&2; exit 1; }

echo "== validating repository sources"
PYTHONDONTWRITEBYTECODE=1 python3 - "$SRC" <<'PY' || { echo "error: python sources do not parse" >&2; exit 1; }
import ast, os, sys
root = sys.argv[1]
for name in ("raidcore.py", "raidwatchd.py", "pam_verify.py"):
    path = os.path.join(root, "app", "server", name)
    with open(path, encoding="utf-8") as fh:
        ast.parse(fh.read(), filename=path)
print("   python syntax OK")
PY
if command -v node >/dev/null 2>&1; then
    node --check "$SRC/app/ui/app.js"
fi
PYTHONDONTWRITEBYTECODE=1 python3 "$SRC/tests/test_regression.py" >/dev/null \
    || { echo "error: regression tests failed -- not installing" >&2; exit 1; }
echo "   sources OK, tests OK"

TS="$(date '+%Y%m%d-%H%M%S')"
BACKUP="$PKGETC/backup-$TS"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "== dry run: would back up to $BACKUP and install app/server/*.py + app/ui/*"
    exit 0
fi

echo "== backing up to $BACKUP"
mkdir -p "$BACKUP/server" "$BACKUP/ui"
cp -a "$APPDEST/server/." "$BACKUP/server/"
cp -a "$APPDEST/ui/app.js" "$APPDEST/ui/index.html" "$APPDEST/ui/style.css" "$BACKUP/ui/"
cp -a "$MANIFEST" "$BACKUP/manifest"

echo "== installing files"
install -o root -g root -m 755 "$SRC"/app/server/raidcore.py   "$APPDEST/server/raidcore.py"
install -o root -g root -m 755 "$SRC"/app/server/raidwatchd.py "$APPDEST/server/raidwatchd.py"
install -o root -g root -m 755 "$SRC"/app/server/pam_verify.py "$APPDEST/server/pam_verify.py"
install -o root -g root -m 644 "$SRC"/app/ui/app.js            "$APPDEST/ui/app.js"
install -o root -g root -m 644 "$SRC"/app/ui/index.html        "$APPDEST/ui/index.html"
install -o root -g root -m 644 "$SRC"/app/ui/style.css         "$APPDEST/ui/style.css"
rm -rf "$APPDEST/server/__pycache__"

if [ "$DO_RESTART" -eq 1 ]; then
    echo "== restarting the app"
    export TRIM_APPDEST="$APPDEST" TRIM_PKGETC="$PKGETC" TRIM_PKGVAR="$PKGVAR"
    "$APP_WRAPPER/cmd/main" stop || true
    sleep 1
    "$APP_WRAPPER/cmd/main" start
    for _ in $(seq 1 20); do
        [ -S "$APPDEST/app.sock" ] && break
        sleep 0.5
    done
    "$APP_WRAPPER/cmd/main" status && echo "   app is running"
fi

echo
echo "rollback: sudo cp -a $BACKUP/server/. $APPDEST/server/ && sudo cp -a $BACKUP/ui/. $APPDEST/ui/"
echo "note: this does not update manifest/version -- use the fpk for a real upgrade."
