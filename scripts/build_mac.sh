#!/bin/bash
# Gera dist/TranslateAPP.app (ad-hoc) e dist/TranslateAPP-macOS.zip. Precisa de uv e Xcode CLT (swiftc).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt pyinstaller
mkdir -p build
swiftc -O native/sck_audio.swift -o build/sck_audio
.venv/bin/pyinstaller packaging/TranslateAPP.spec --noconfirm --distpath dist --workpath build/pyi
codesign --force --deep -s - dist/TranslateAPP.app
rm -f dist/TranslateAPP-macOS.zip
ditto -c -k --keepParent dist/TranslateAPP.app dist/TranslateAPP-macOS.zip
echo "ok: dist/TranslateAPP.app e dist/TranslateAPP-macOS.zip"
