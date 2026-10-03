@echo off
rem TranslateAPP: duplo clique. O app abre sem console (pythonw) e esta janelinha fecha em seguida.
rem Erros e diagnostico ficam em logs\app.log; para ver a mensagem no terminal: .venv\Scripts\python.exe -m app.main
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Ambiente nao encontrado. Rode setup.ps1 primeiro: clique direito nele e escolha Executar com o PowerShell.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m app.main
