#!/bin/sh
# Run from the release root after building; output stays outside the release.
set -eu
git rev-parse HEAD
git status --short
uname -a
cat /etc/os-release
cmake --version
cc --version
"${PYTHON:-python3}" --version
"${PYTHON:-python3}" -m ddt --version
"${PYTHON:-python3}" -m pip freeze --all
if command -v apk >/dev/null 2>&1; then apk info -v; fi
sha256sum build/bin/sweep requirements.txt config/detector.json
ldd build/bin/sweep
