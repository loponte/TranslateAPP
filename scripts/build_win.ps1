# Gera dist\TranslateAPP\TranslateAPP.exe e dist\TranslateAPP-Windows.zip. Precisa de uv. Rode da raiz ou de qualquer pasta.
# Sem as nvidia-* (o .exe baixa cuBLAS/cuDNN no 1º uso, só se houver GPU NVIDIA).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot)
if (-not (Test-Path .venv)) { uv venv --python 3.12 .venv; if ($LASTEXITCODE) { throw "uv venv falhou" } }
$py = ".venv\Scripts\python.exe"
$req = Join-Path $env:TEMP "req-build.txt"
Get-Content requirements.txt | Where-Object { $_ -notmatch "^nvidia-" } | Set-Content $req
uv pip install --python $py -r $req pyinstaller; if ($LASTEXITCODE) { throw "uv pip install falhou" }
& $py -m PyInstaller packaging\TranslateAPP.spec --noconfirm --distpath dist --workpath build\pyi
if ($LASTEXITCODE) { throw "PyInstaller falhou" }
if (Test-Path dist\TranslateAPP-Windows.zip) { Remove-Item dist\TranslateAPP-Windows.zip }
Compress-Archive -Path dist\TranslateAPP -DestinationPath dist\TranslateAPP-Windows.zip
Write-Host "ok: dist\TranslateAPP-Windows.zip"
