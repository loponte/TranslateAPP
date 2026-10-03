# Gera dist\TranslateAPP\TranslateAPP.exe e o instalador dist\TranslateAPP-Setup.exe (Inno Setup). Precisa de uv e ISCC. Rode da raiz ou de qualquer pasta.
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
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
if (-not $iscc) { throw "Inno Setup (ISCC.exe) não encontrado: instale com 'choco install innosetup' ou 'winget install JRSoftware.InnoSetup'" }
& $iscc /Q packaging\TranslateAPP.iss
if ($LASTEXITCODE) { throw "Inno Setup falhou" }
Write-Host "ok: dist\TranslateAPP-Setup.exe"
