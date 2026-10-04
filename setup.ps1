# TranslateAPP: prepara o ambiente. Pode rodar de novo quando quiser (só completa o que falta).
#   1) cria .venv com Python 3.12 (via uv)   2) instala o requirements.txt   3) cria o atalho TranslateAPP.lnk
#   4) baixa os modelos (alguns GB, só na 1ª vez)
# Uso: botão direito > "Executar com o PowerShell"   ou   powershell -ExecutionPolicy Bypass -File .\setup.ps1
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$falhou = $false

function Passo($texto) { Write-Host "`n== $texto" -ForegroundColor Cyan }
function Checa($texto) { if ($LASTEXITCODE -ne 0) { throw "$texto (código $LASTEXITCODE)" } }

try {
    Passo "uv"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "uv não encontrado, instalando..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-RestMethod https://astral.sh/uv/install.ps1 -ErrorAction Stop | Invoke-Expression
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "uv não ficou disponível. Instale com 'winget install astral-sh.uv' e rode de novo"
        }
    }

    Passo "Python 3.12 (.venv)"
    if (Test-Path $py) { Write-Host ".venv já existe" } else { uv venv --python 3.12 .venv; Checa "uv venv falhou" }

    Passo "Dependências (requirements.txt)"
    uv pip install --python $py -r requirements.txt
    Checa "uv pip install falhou"

    Passo "Atalho (TranslateAPP.lnk)"
    # abre o app sem console: pythonw -m app.main, com a raiz do projeto como pasta de trabalho (ícone padrão)
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $PSScriptRoot "TranslateAPP.lnk"))
    $lnk.TargetPath = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
    $lnk.Arguments = "-m app.main"
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.Description = "TranslateAPP - legendas ao vivo em português"
    $lnk.Save()
    Write-Host "  ok: TranslateAPP.lnk"

    Passo "Modelos (fala, locutores, VAD, tradução)"
    $env:PYTHONPATH = $PSScriptRoot
    # se um download falhar, só avisa (o app tenta de novo ao abrir). Texto sem acento: o console lê o python em outra codificação
    @'
import importlib
falhou = 0


def passo(nome, fn, opcional=False):
    global falhou
    try:
        fn()
        print("  ok: " + nome)
    except Exception as e:
        falhou += not opcional
        print("  AVISO: %s nao foi baixado: %s: %s" % (nome, type(e).__name__, e))


asr, diar, mt = (importlib.import_module("app." + m) for m in ("asr", "diar", "mt"))
passo("Whisper large-v3-turbo (GPU, ~1,6 GB)", lambda: asr.ensure_model("models", "large-v3-turbo"))
passo("Parakeet v3 int8 (CPU, ~490 MB)", lambda: asr.ensure_model("models", "parakeet-v3"))
passo("locutores (embedding de voz + segmentacao)", diar.ensure_model)
passo("VAD (Silero)", lambda: importlib.import_module("app.segmenter").Segmenter())  # copia o modelo do faster-whisper
passo("traducao EN->PT-BR (OPUS-MT, ~860 MB)", mt.ensure_model)
# opcional: so a legenda em ingles usa; sem ele o app baixa na 1a vez que precisar
passo("traducao PT->EN (OPUS-MT, ~280 MB)", lambda: mt.ensure_model("models", "pt-en"), opcional=True)
raise SystemExit(1 if falhou else 0)
'@ | & $py -
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Algum modelo não foi baixado (veja acima). Confira a internet e rode setup.ps1 de novo."
        $falhou = $true
    }
} catch {
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    $falhou = $true
}

Write-Host ""
if ($falhou) { Write-Host "Terminou com problemas (veja as mensagens acima)." -ForegroundColor Yellow }
else { Write-Host "Tudo pronto. Abra o app com TranslateAPP.lnk (ou run.bat)." -ForegroundColor Green }
if (-not [Console]::IsInputRedirected) { Read-Host "Enter para fechar" | Out-Null }  # janela do botão direito fecha sozinha
exit ([int]$falhou)
