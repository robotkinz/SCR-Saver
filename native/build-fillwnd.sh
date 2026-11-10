#!/bin/sh
# Build the 32-bit helper that keeps a screensaver window at screen size.
set -e
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SRC="$ROOT/native/scrsaver_fillwnd.c"
OUT="$ROOT/native/scrsaver_fillwnd.exe"
OBJ="${TMPDIR:-/tmp}/scrsaver_fillwnd.$$.obj"
INC_WINE="${WINE_INCDIR:-/usr/include/wine}"
LIB_I386="${WINE_I386_LIBS:-/usr/lib/wine/i386-windows}"
CLANG="${CLANG:-clang}"
LLD_LINK="${LLD_LINK:-lld-link}"

if ! command -v "$CLANG" >/dev/null 2>&1 || ! command -v "$LLD_LINK" >/dev/null 2>&1; then
    echo "clang and lld-link are needed to rebuild scrsaver_fillwnd.exe" >&2
    exit 1
fi
if [ ! -d "$INC_WINE/windows" ] || [ ! -d "$LIB_I386" ]; then
    echo "Wine i386 headers/libs not found under $INC_WINE and $LIB_I386" >&2
    exit 1
fi

"$CLANG" -c -target i686-windows-msvc -nostdinc \
    -isystem "$INC_WINE/msvcrt" \
    -isystem "$INC_WINE/windows" \
    -o "$OBJ" "$SRC"

"$LLD_LINK" -subsystem:windows -entry:entry -nodefaultlib -machine:x86 -safeseh:no \
    -out:"$OUT" "$OBJ" \
    "$LIB_I386/libuser32.a" \
    "$LIB_I386/libkernel32.a"

rm -f "$OBJ"
echo "Wrote $OUT"
