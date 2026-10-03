# TranslateAPP — legendas ao vivo em português do áudio do PC

Escuta o que sai do PC (Discord, Meet, Teams, Zoom, YouTube…), transcreve o **inglês** com Whisper, separa por
**locutor** e mostra a tradução em **português do Brasil** numa janela, com o menor atraso possível (medido com GPU NVIDIA:
a legenda definitiva sai ~0,45 s depois da frase terminar, até ~0,7 s quando a frase troca de locutor; a parcial, em cinza,
~0,13 s depois do áudio que ela cobre). Tudo roda local: nenhum áudio sai do computador.

> O Whisper só traduz *para* o inglês, nunca para o português. Por isso ele só transcreve o inglês, e a tradução
> EN → PT-BR é feita por um modelo local separado: o **OPUS-MT** (Helsinki-NLP/OPUS-MT, licença **CC-BY-4.0**), convertido
> para CTranslate2 em `models\mt-en-pt` (a origem, a licença e a atribuição ficam nessa pasta: `info.json`, `LICENSE`, `README.md`).
> Os outros modelos (Whisper, VAD, locutores) têm as licenças dos próprios autores.

## Baixar pronto (sem instalar Python)
Na página de **Releases** do GitHub há dois zips (gerados pelo Actions). Os modelos **não** vêm no pacote: na 1ª abertura o app
baixa tudo para a pasta de dados (Windows `%LOCALAPPDATA%\TranslateAPP`, macOS `~/Library/Application Support/TranslateAPP`)
mostrando o progresso na janela (Whisper, tradutor ~860 MB, locutores). Logs em `logs\app.log` dessa pasta.
- **Windows (.exe):** baixe e rode `TranslateAPP-Setup.exe` (instala por usuário, sem admin). Com GPU NVIDIA, o app
  baixa também cuBLAS/cuDNN (~1,3 GB) na 1ª vez; sem GPU (ou se falhar) roda na CPU com o Whisper small.en.
- **macOS (.app, só Apple Silicon/arm64 — Mac Intel não é suportado; macOS 13+):** baixe `TranslateAPP.dmg`, abra e arraste `TranslateAPP.app` para Aplicativos.
  O app é assinado só ad-hoc (sem conta de desenvolvedor): na 1ª vez, clique com o botão direito → **Abrir** → *Abrir mesmo assim*
  (ou Ajustes do Sistema › Privacidade e Segurança › *Abrir mesmo assim*). O áudio do sistema vem do ScreenCaptureKit: libere o
  app em **Ajustes do Sistema › Privacidade e Segurança › Gravação de Tela e Áudio do Sistema** e reabra (não grava a tela).
  Sem permissão a barra de status avisa. Alternativa sem permissão: escolha uma entrada de áudio (ex.: BlackHole) em *Dispositivo*.
  Cada build novo pode pedir a permissão de novo (assinatura ad-hoc). Na CPU o Whisper é o small.en (mais lento que na GPU).
- **Gerar você mesmo:** `scripts/build_mac.sh` (macOS; precisa de uv e Xcode CLT) → `dist/TranslateAPP.app` + `dist/TranslateAPP.dmg`;
  `scripts\build_win.ps1` (Windows; precisa do Inno Setup) → `dist\TranslateAPP-Setup.exe`. O CI (`.github/workflows/build.yml`) faz os dois, roda
  `TranslateAPP --selftest` no binário e, numa tag `v*`, anexa o `.exe` e o `.dmg` a uma Release.

## Instalar e rodar (a partir do código, Windows)
1. **Uma vez:** botão direito em `setup.ps1` → *Executar com o PowerShell* (ou `powershell -ExecutionPolicy Bypass -File .\setup.ps1`).
   Cria o `.venv` (Python 3.12 via uv), instala o `requirements.txt`, cria o atalho `TranslateAPP.lnk` e baixa os modelos para
   `models\` (alguns GB: Whisper distil-large-v3.5 e small.en, locutores, VAD, tradução). Pode repetir à vontade (só completa o que falta).
2. **Sempre:** duplo clique em `TranslateAPP.lnk` ou em `run.bat`. A janela abre sem console; erros e diagnóstico ficam em
   `logs\app.log` (para ver a mensagem no terminal: `.venv\Scripts\python.exe -m app.main`).
3. **O app já começa a escutar ao abrir** (`AUTOSTART = True` em `app\main.py`; usa o último dispositivo escolhido). Na barra de status:
   *Carregando modelos…* → *Pronto — escutando …*. Com `AUTOSTART = False` ele espera o clique em **Iniciar**.

## Botões
- **Dispositivo** — qual saída de áudio escutar (no macOS: *Áudio do sistema* ou uma entrada). *Padrão do Windows* acompanha a saída padrão (se ela mudar, a captura reabre sozinha); escolher um nome fixa aquela saída. **Atualizar** relê a lista (fone novo, por exemplo).
- **Iniciar / Parar** — Iniciar começa uma conversa nova (os locutores recomeçam em “Locutor 1”). Parar libera o áudio; os modelos continuam carregados.
- **Limpar** (Ctrl+L) apaga a tela. **Salvar…** (Ctrl+S) grava um `.txt` com horário, locutor, português e inglês.
- **A− / A+** tamanho da letra · **Mostrar inglês** · **Sempre no topo** · **Opacidade**. As preferências ficam em `settings.json`.
- Texto cinza em itálico = parcial (ainda sendo ouvido); vira definitivo quando a frase termina. “atraso ~X s” = média das últimas frases, do fim da fala até a legenda pronta.

## Locutores
Cada voz vira “Locutor 1, 2, …” na ordem em que aparece (embedding de voz TitaNet + comparação com as vozes já vistas).
Quando outra pessoa começa a falar logo depois da primeira (pausa menor que o silêncio de fim de frase, 0,45 s), a frase chega junta;
o pipeline chama `SpeakerTracker.segments()` no final, que acha a troca de voz, e cada trecho vira **uma linha**, transcrita só com o áudio dele
(a linha parcial cinza é trocada pela 1ª; as outras entram em seguida). Trecho curto demais para reconhecer a voz (< ~1 s, voz nova) fica como **Locutor ?**:
é melhor que um rótulo errado.

## Calibrar
Três dicionários no topo de `app\main.py`, vazios = padrão do módulo. Preencha, salve e reabra o app.
- **Silêncio de fim de frase** — `SEG_KW = {"end_silence_ms": 350}` (padrão 450 ms; `end_silence_long_ms`, padrão 250 ms, vale depois de 6 s de fala).
  Quanto menor, mais rápido a frase fecha, mas pausas curtas passam a cortar frases em duas. Quanto maior, frases mais inteiras e legenda definitiva mais lenta.
- **Limiar de locutor** — `DIAR_KW = {"threshold": 0.45}` (padrão **0,40**, calibrado para o TitaNet-small; similaridade mínima para ser “a mesma pessoa”).
  Mesma pessoa virando dois locutores → **baixe**; duas pessoas viraram uma → **suba**. Mexa de 0,05 em 0,05 (em fala limpa só 0,35–0,45 funciona bem; fora disso a acurácia cai rápido).
  Voz de call (Discord/Opus, microfone ruim) separa menos que o áudio de teste. Trocou de microfone no meio da call? **Parar** e **Iniciar** zera os locutores.
- **Modelo do Whisper** — `ASR_KW = {"model": "small.en"}` troca por um menor (mais rápido e leve, erra mais; também roda na CPU). Padrão na GPU: `distil-large-v3.5` (float16), versão destilada do Whisper large-v3 feita pela Hugging Face (a mais rápida e a de menor erro nos testes). Para usar o Whisper original da OpenAI: `ASR_KW = {"model": "large-v3-turbo"}` (baixa ~1,6 GB na 1ª vez).
- **Medir** — `tests\e2e_report.py` reproduz os áudios de teste em tempo real pelo app inteiro (sem tocar som) e mostra acurácia de locutor, erro de transcrição (WER) e atrasos;
  `--kw diar.threshold=0.35` (ou `seg.…`, `asr.…`) testa uma calibração sem editar nada; `--fast` faz uma varredura rápida (sem atrasos) e `--secs 30` só os primeiros 30 s.
  Atrasos reais por frase em `logs\latency.csv` (`utt_id,dur_s,asr_ms,spk_ms,mt_ms,lag_ms`; `lag_ms` = fim do áudio → legenda pronta, ±16 ms):
  `Import-Csv logs\latency.csv | Measure-Object lag_ms -Average -Maximum`.

## Problemas
- **Nada aparece:** o app só escuta a saída escolhida *enquanto algo toca nela*. Toque um vídeo e confira se o dispositivo selecionado é aquele por onde o som realmente sai (fone ≠ monitor ≠ caixas).
- **Discord (ou outro app) com saída diferente da padrão do Windows:** veja em Discord → Configurações → Voz e vídeo → *Dispositivo de saída* e selecione o **mesmo nome** aqui (não “Padrão do Windows”).
- **Fica em “Carregando modelos…” ou dá erro de modelo:** rode `setup.ps1` de novo (precisa de internet na primeira vez) e veja `logs\app.log`.
- **Legenda atrasada:** GPU ocupada por outro programa (jogo, render) — feche-o ou use um Whisper menor (acima). Sem GPU NVIDIA o app cai para CPU, bem mais lento. Se o atraso é quase sempre ~0,5 s, é o silêncio de fim de frase (acima).
- **Locutores trocados:** a mesma pessoa com 2 rótulos → baixe o `threshold`; duas pessoas num rótulo só → suba. “Locutor ?” = trecho curto demais para reconhecer a voz.
- **Só inglês:** o áudio de entrada precisa estar em inglês; a saída é sempre português do Brasil.
