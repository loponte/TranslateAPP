# TranslateAPP — arquitetura e contratos entre módulos

App (Windows e macOS) que captura o áudio que sai do computador — o som inteiro ou só um app (Windows: WASAPI loopback e
Process Loopback; macOS: ScreenCaptureKit) —, transcreve **inglês ou português** (idioma detectado por frase), separa por
**locutor**, traduz para o idioma da legenda (**pt-BR** ou **inglês**) e mostra a legenda ao vivo numa janela flutuante.
100 % local (sem APIs externas, sem Claude), foco em **menor latência possível**.

> Modelos: ASR = **Whisper large-v3-turbo** (GPU) ou **Parakeet-TDT-0.6B-v3 int8** (CPU/Mac); tradução = **OPUS-MT** em
> CTranslate2 (CC-BY-4.0, atribuição ao Helsinki-NLP/OPUS-MT em `models\mt-en-pt` e `models\mt-pt-en`), um modelo por direção.
> Só 1 tradutor por sessão: `outro(sub_lang) → sub_lang`; fala no idioma da legenda não é traduzida.

## Ambiente
- Windows 11 · i7-14700K (28 threads) · 32 GB · **RTX 4080 SUPER 16 GB** · saídas de áudio: Realtek, LG HDR WFHD (HDMI/DP), NVIDIA HDMI, Voicemeeter.
- Python 3.12 em `.venv` → `.venv\Scripts\python.exe`. Rode sempre a partir da raiz do projeto (`python -m app.x`, `python tests\x.py` com `PYTHONPATH` = raiz). `setup.ps1` cria o `.venv`, instala o `requirements.txt`, cria o atalho `TranslateAPP.lnk` e baixa os modelos; `run.bat` / o atalho abrem o app sem console (`pythonw -m app.main`, log em `logs\app.log`).
- Pacotes (`requirements.txt`, só o que `app\` importa; **nenhum pacote novo nesta versão**): numpy 2.5, faster-whisper 1.2.1 (ctranslate2 4.8.2 com CUDA), onnxruntime 1.30, sherpa-onnx 1.13.8 (locutores e Parakeet), PyAV (`av`, reamostragem), pyaudiowpatch, sentencepiece, pyyaml (conversão do OPUS-MT), nvidia-cublas-cu12, nvidia-cudnn-cu12; tkinter (stdlib). A captura por app e o vidro do overlay usam só ctypes.
- CUDA no Windows: `from app.cuda_dlls import setup_cuda_dlls; setup_cuda_dlls()` **antes** de importar ctranslate2/faster_whisper.
- Pacote extra (sacrebleu, pillow, torch/transformers…) só em ambiente descartável: `uv run --no-project --with <pkg> python script.py`; o `.venv` fica só com o `requirements.txt`.
- Modelos em `models\` (raiz do projeto), baixados por `setup.ps1` / `ensure_model()`, nunca versionados:
  `whisper\` (large-v3-turbo, 1,6 GB), `parakeet-v3-int8\` (~670 MB extraído, CC-BY-4.0 NVIDIA, `info.json`), `spk\` (`nemo_en_titanet_small.onnx` 38 MB = embedding; `pyannote-segmentation-3-0.onnx` 6 MB = troca de locutor), `silero_vad.onnx` (copiado do faster-whisper), `mt-en-pt\` (OPUS-MT tc-big, float16, ~450 MB), `mt-pt-en\` (OPUS-MT por-eng, float16, 148 MB; só baixado se a legenda for EN). Não usados mais (podem ser apagados): `whisper\…distil-large-v3.5`, `whisper\…small.en` (~2 GB), `spk\3dspeaker_speech_eres2net…` e `_cand\` (~27 GB).
- Fixtures de teste em `tests\data\` (ver `tests\data\README.md`): EN `conv_2spk.wav`, `conv_4spk.wav`, `conv_2spk_noisy.wav` (16 kHz mono), `conv_2spk_48k_stereo.wav`; PT (`tests\data\make_pt.py`, edge-tts, cache em `_cache_pt\`) `conv_pt_2spk` (102,4 s, 21 turnos), `conv_pt_3spk` (70,6 s, 17 turnos), `conv_pt_2spk_noisy` (12 dB). Cada um com `<nome>.json` = gabarito `{"voices":…, "turns":[{"speaker","text","start","end"}]}`.

## Plataformas, pastas de dados e empacotamento
- `app/paths.py`: `DATA_DIR`, `MODELS`, `LOGS`, `SETTINGS` e `download(url, sha256, dst, on_progress)` (helper comum ao asr e ao mt). Empacotado (PyInstaller, `sys.frozen`): `%LOCALAPPDATA%\TranslateAPP` (Win) ou `~/Library/Application Support/TranslateAPP` (mac); rodando do código: a raiz do repo. `main.py` faz `os.chdir(DATA_DIR)`, então `models/` e `logs/` relativos continuam valendo. `settings.json` (UI) e `speakers.json` (vozes fixadas) ficam em `DATA_DIR` e no `.gitignore`, junto com o `selftest.txt`.
- Primeiro uso: modelos ausentes são baixados pelos próprios `ensure_model(..., on_progress)` (asr/mt/diar; o callback só dispara se há download; `None` = sem %). O `Pipeline._load` converte isso em `Status("Baixando reconhecimento de fala… (1/4)" | "Baixando tradutor… (2/4)" | "Baixando modelo de locutores… (3/4)", "download", progress)`; `ready` limpa. No .exe com NVIDIA, `cuda_dlls.ensure_cuda()` baixa cuBLAS/cuDNN (versões fixas, SHA-256 conferido) para `DATA_DIR/cuda.tmp` e renomeia para `cuda` só no fim; falhou = o pipeline força `device="cpu"` no ASR e no tradutor; sem GPU = CPU (Parakeet).
- `app/audio.py` escolhe o backend por `sys.platform` (mesma interface; o `_pump` mono/16 kHz/grade de 32 ms é comum). macOS (`app/audio_mac.py`): "Áudio do sistema" e captura por app = helper Swift `native/sck_audio.swift` (SCStream, só áudio, 48 kHz estéreo f32 no stdout); os outros dispositivos = entradas via `sounddevice` (ex.: BlackHole). O helper é compilado com `swiftc` no build e vai em `Contents/Frameworks` do .app.
- Build: `packaging/TranslateAPP.spec` (onedir, sem console; sem nvidia-*, sem modelos; inclui `app/assets` e os hiddenimports `app.ui_overlay`/`app.ui_i18n`; no Mac `ATSApplicationFontsPath = app/assets`, não testado; versão 0.3.0), `scripts/build_mac.sh` (swiftc + pyinstaller + `codesign -s -` ad-hoc + dmg) e `scripts/build_win.ps1` (Inno Setup); CI em `.github/workflows/build.yml` (matriz windows/macos: testes, build, `--selftest`, artefatos; tag `v*` = Release). `main.py --selftest` importa tudo, cria o VAD, lista dispositivos **e apps** (sem áudio não falha) e abre/fecha um Tk oculto. `multiprocessing.freeze_support()` em `main.py` é obrigatório.

## Fluxo
```
LoopbackCapture(device | app) ─chunks 32 ms (16 kHz mono f32)→ Segmenter ─SegEvent(start|partial|final)→ Pipeline (worker único) ─Update/Status→ queue.Queue → UI (tkinter)

parcial: ASR (beam 1; detecta en/pt ou herda o prior; o idioma trava na utterance com ≥ 1,5 s) → identify(final=False) (palpite) → MT se o idioma ≠ legenda
final:   com texto no último parcial (fala confirmada):
           SpeakerTracker.segments() → 1 trecho : ASR (beam 5, áudio todo) → MT? → 1 Update (utt_id do evento)
                                     → N trechos: ASR (beam 5) de cada trecho (cada um detecta o idioma) → MT? → N Updates
         sem texto no parcial: ASR do áudio todo primeiro; vazio = ruído → descarta sem consultar os locutores; senão, como acima
```

## Convenções
- Áudio interno: `np.float32`, mono, 16 kHz, [-1, 1]. Tempo: `time.monotonic()`. Tipos compartilhados em `app/events.py` (`Lang`, `SR`, `SegEvent`, `Update`, `Status`) — não editar sem avisar.
- Código enxuto: stdlib/pacotes instalados primeiro; sem abstrações além do contrato (sem classes base, sem factory, sem config global — cada módulo recebe kwargs com defaults); sem dependência nova sem necessidade.
- Cada módulo deixa UM check executável (`tests\test_<modulo>.py` com `assert`, ou `if __name__ == "__main__"`).
- Comentários em pt-BR, curtos. Identificadores em inglês. Textos de UI/status com acentuação correta. Sem emoji em `print` (console cp1252) nem como ícone na UI.
- Simplificação consciente com limite conhecido → comentário `# ponytail: <limite>, <upgrade>`.
- Cada agente edita **só os seus arquivos** (tabela abaixo); mudança em arquivo alheio = pedido ao dono. `app/cuda_dlls.py` e `app/__init__.py` são do orquestrador.

| Agente | Arquivos |
|---|---|
| Forja-Idiomas (ASR, MT, pipeline, integração) | `app/events.py`, `app/asr.py`, `app/mt.py`, `app/pipeline.py`, `app/main.py`, `app/paths.py`, `tests/bench_asr.py`, `tests/eval_mt.py`, `tests/test_pipeline.py`, `tests/e2e_report.py`, `tests/data/make_pt.py` + fixtures PT, `scripts/convert_mt.py`, `scripts/build_*`, `packaging/`, `.github/`, `run.bat`, `setup.ps1`, `requirements.txt`, `.gitignore`, `README.md`, `ARCHITECTURE.md`, `docs/app-live.png` |
| Forja-Captura | `app/audio.py`, `app/audio_mac.py`, `native/sck_audio.swift`, `tests/test_audio.py` |
| Forja-Locutores | `app/diar.py`, `tests/eval_diar.py`, `tests/eval_split.py` |
| Vitral (UI) | `app/ui.py`, `app/ui_overlay.py`, `app/ui_i18n.py`, `app/assets/` (Outfit.ttf + OFL.txt), `docs/ui-*.png` |
| Segmentação (sem dono nesta rodada) | `app/segmenter.py`, `tests/test_segmenter.py` |

## Contratos

### `app/events.py`
```python
Lang = Literal["en", "pt"]          # idiomas suportados (falado e legenda)

@dataclass
class Update:   # Pipeline -> UI. Mesmo utt_id = mesma linha (a parcial é substituída pela final); final com orig == "" = descarte
    utt_id: int
    speaker: int | None  # id estável >= 0 (pode passar de 7 com vozes fixadas); None = desconhecido/provisório
    orig: str            # transcrição no idioma falado
    sub: str             # legenda; = orig se o falado já é o idioma da legenda; "" se a MT falhou (a UI mostra o orig)
    final: bool
    t0: float; t_end: float; t_ready: float   # atraso = t_ready - t_end
    lang: str = "en"     # idioma falado desta linha: "en" | "pt"
# SegEvent e Status sem mudança (Status.level: info|warn|error|ready|download; progress 0..1 no download)
```

### `app/audio.py` / `app/audio_mac.py` / `native/sck_audio.swift`
```python
@dataclass
class LoopbackDevice:
    index: int; name: str; is_default: bool
@dataclass
class AudioApp:
    id: str       # Windows: exe ("Discord.exe"); mac: bundle id ("com.hnc.Discord")
    name: str     # FileDescription do exe ("Discord", "Google Chrome") / applicationName
    active: bool  # tocando agora (Windows); mac: False
APP_CAPTURE: bool                                       # win: build >= 19041; mac: True
def list_loopback_devices() -> list[LoopbackDevice]     # saídas com loopback; padrão primeiro
def list_audio_apps() -> list[AudioApp]                 # quem toca primeiro; [] se não suportado; qualquer thread (inicializa COM)

class LoopbackCapture:
    def __init__(self, on_audio, device: str | None = None, on_status=None, block_ms: int = 32, app: str | None = None): ...
    def start(self) -> None      # não bloqueia
    def stop(self) -> None       # idempotente; espera as threads
    device_name: str             # dispositivo/app realmente em uso (preenchido quando a captura abre de fato)
```
- `on_audio(chunk, t_end)`: `chunk` float32 mono 16 kHz com exatamente `block_ms*16` amostras (512 @ 32 ms, o que o Silero quer); `t_end` = `time.monotonic()` do fim do chunk, numa grade ancorada no relógio. Chamado numa thread de captura; deve voltar rápido.
- Loopback **não entrega nada quando nada toca** → o módulo gera chunks de zeros no ritmo do relógio (a linha do tempo nunca para; o Segmenter depende disso). Salto do relógio (suspensão) realinha a grade.
- **`app=None`** (saída inteira): `device=None` → saída padrão (se mudar, reabre sozinho); `device="texto"` → nome exato, senão substring. Dispositivo some/erro → tenta reabrir a cada 1–2 s e avisa por `on_status`.
- **`app="X.exe"` (Windows):** Process Loopback via ctypes (`ActivateAudioInterfaceAsync("VAD\\Process_Loopback")` com `INCLUDE_TARGET_PROCESS_TREE`), float32 48 kHz estéreo. Uma thread "audio-app" põe `(t, 48000, 2, bytes)` em `_q`; o `_pump` é o mesmo. A `_manage_app` acha o processo raiz pelo exe, sem diferenciar maiúsculas (primeiro o que está tocando, depois o que tem sessão, depois qualquer um; sobe enquanto o pai tem o mesmo exe), vigia o processo a cada 0,5 s com um handle SYNCHRONIZE (o Process Loopback entrega zeros até para um pid morto) e, se ele morre, procura de novo e reabre; app ausente = nova tentativa a cada 1,5 s. `device` é ignorado; `device_name` = nome do app.
- **Lista no Windows:** sessões de áudio de **todas** as saídas ativas (o Discord pode tocar num fone que não é o padrão), sem os sons do sistema, agrupadas pelo exe raiz. Uma sessão some depois de um tempo sem tocar, por isso a UI tem o botão de atualizar e o app é salvo pelo exe. `# ponytail:` dois exe iguais e independentes viram um item só (a captura pega o que está tocando).
- **Mensagens (`on_status`, pt-BR):** `Capturando áudio de: <nome>` a cada (re)abertura (o pipeline transforma em "Pronto — escutando <nome>"); `<nome> não está aberto — esperando…` uma vez, com a linha do tempo seguindo em zeros; `A captura de <nome> parou — reabrindo…`; `Falha na captura por app (<nome>: <erro>) — use "Saída inteira"` (ativação recusada: o pipeline manda como `Status` **error**, a UI encerra a sessão e oferece "Usar a saída inteira"). As demais viram `warn`.
- **Mac:** helper com `--list` (`pid\tbundle\tnome`) e `--app <bundleID>` (`SCContentFilter(display:including:)` com o app mais os processos `<id>.*`, os `.helper`); códigos de saída 0 ok, 2 o stream caiu, 3 sem permissão, 4 sem display, 5 app não encontrado, 6 o app fechou (vigia com `kill(pid, 0)` numa thread, sem run loop). `audio_mac`: 5 e 6 viram "não está aberto — esperando…" com nova tentativa a cada 1,5 s; `list_apps()` tira os `.helper` por prefixo.
- Qualquer nº de canais/taxa → mono (média; 5.1/7.1: FL/FR + 0,7·centro) → 16 kHz com **PyAV/swr** (~1 ms de atraso fixo).

### `app/segmenter.py`
```python
class Segmenter:
    def __init__(self, *, vad_threshold=0.5, end_silence_ms=450, end_silence_long_ms=250, long_after_s=6.0,
                 max_utt_s=15.0, min_speech_ms=250, preroll_ms=250, tail_ms=150,
                 partial_every_ms=600, min_partial_ms=500): ...
    def feed(self, chunk: np.ndarray, t_end: float) -> list[SegEvent]
    def reset(self) -> None
```
- Síncrono, sem threads, determinístico (testável offline alimentando wav em chunks de 32 ms com timestamps sintéticos). Aceita chunks de qualquer tamanho (re-bufferiza janelas de 512 para o Silero).
- VAD: Silero VAD v6 ONNX (o do faster-whisper, copiado para `models/silero_vad.onnx`) via onnxruntime direto, 1 thread.
- Eventos por utterance (`utt_id` incremental desde 0, **continua entre `reset()`s**; `t0` = início da fala; `t_end` = fim do último áudio incluído): `start` (1×, após `min_speech_ms` de fala; o pipeline ignora); `partial` (a cada `partial_every_ms` de fala, só após `min_partial_ms`; `audio` = utterance inteira até agora, com preroll); `final` (silêncio ≥ `end_silence_ms` — `end_silence_long_ms` depois de `long_after_s` — ou utterance ≥ `max_utt_s`: corta na última pausa ≥ ~100 ms dos últimos ~4 s, senão corte seco). `final.audio` = preroll + fala + `tail_ms`. Rajadas < `min_speech_ms` = ruído, sem eventos.

### `app/asr.py`
```python
class Transcriber:
    def __init__(self, *, model="auto", device="auto", compute_type="auto", models_dir="models", cpu_threads=0, on_progress=None): ...
    def warmup(self) -> None                       # inferências dummy pelo caminho de detecção
    def transcribe(self, audio, final=False, language: str | None = None, prior: str = "en") -> tuple[str, str]   # (texto, "en"|"pt")
    model_name: str; device: str; compute_type: str
def ensure_model(models_dir="models", model="auto", on_progress=None) -> str   # idempotente
```
- `model="auto"`: GPU → **large-v3-turbo float16** (models/whisper, via HF); CPU → **"parakeet-v3"** (sherpa-onnx int8, sempre CPU, models/parakeet-v3-int8; download de 487 MB com SHA-256 `5793d0fd…16bf`, só os 4 arquivos são extraídos).
- **Caminho fused (GPU):** 1 encoder → `detect_language` comparando só `<|en|>` e `<|pt|>` → `generate`. O `avg_logprob` (score·len/(len+1)) e o `compression_ratio` são recalculados como no faster-whisper e os tokens suprimidos vêm de `get_suppressed_tokens`, então os filtros antigos continuam valendo. Detecção custa +3 ms (o `language=None` do faster-whisper custaria +73 ms e confundiria "Oi, gente." com espanhol).
- **Idioma:** `language` fixo = sem detecção. `None` = detecta {en, pt}; fala < `LID_MIN_S` = 1,5 s ou margem `|p_en − p_pt| / (p_en + p_pt)` < `LID_MARGIN` = 0,3 → herda o `prior`. Parakeet: idioma pelo texto (`text_lid`: palavras funcionais + acentos; empate = prior).
- `final=False` (parcial): beam 1, sem timestamps, temperatura 0. `final=True`: beam 5.
- **Filtros:** áudio < 0,3 s ou RMS < 0,0015, texto vazio, `avg_logprob` < −1, `compression_ratio` > 2,4. Sempre descartadas: alucinações fixas ("thanks for watching", "legendas pela comunidade amara org", "inscreva se", "obrigado por assistir"…) e hesitação sozinha (uh, um, hmm, mm, ah, eh). Só com o modelo inseguro (logprob < −0,3): "thank you", "bye", "obrigado", "obrigada", "tchau", "e aí" (o turbo inventa "E aí" em ruído, como o "you" do EN). A comparação é sem acento e sem pontuação. O Parakeet não tem logprob: as frases "fracas" nunca somem nele.

### `app/diar.py`
```python
class SpeakerTracker:
    def __init__(self, *, threshold=0.40, min_audio_s=1.0, max_speakers=8, confirm_s=3.0,
                 models_dir="models", profiles_path: str | None = None, on_progress=None): ...
    def identify(self, audio, final) -> int | None          # provisório = None
    def segments(self, audio) -> list[tuple[float, float, int | None]]
    def reset(self) -> None              # só os fixados continuam, mesmos ids; ids novos > maior fixado
    def pin(self, spk) -> bool           # fixa e grava já; False se o id não existe
    def merge(self, src, dst) -> None    # centróides somados; src some; fixação de src passa a dst; grava se fixado
    def forget(self, spk=None) -> None   # tira do arquivo (None = todos; sem fixados o arquivo é apagado)
    def save(self) -> None               # regrava os fixados com o centróide atual
def saved_speakers(path) -> list[int]    # [] se não existe, se está corrompido ou se o "model" é outro
def forget_saved(path, spk=None) -> None
def ensure_model(models_dir="models", on_progress=None) -> str
```
- Dois modelos ONNX em CPU: **TitaNet-small** (embedding de 192 dims, sherpa-onnx) + **pyannote-segmentation-3.0** (quem fala em cada quadro de ~17 ms; só em `segments()`). Agrupamento online por cosseno com centróides (média ponderada pela duração, memória ~60 s).
- **Provisório:** locutor novo sem id ("?") até somar `confirm_s` = 3,0 s de fala; aí recebe o próximo id, na ordem de confirmação, nunca reaproveitado na sessão. O palpite parcial e a fusão de trechos no `segments()` ignoram provisórios. Tabela lotada (`max_speakers`, sem contar os fixados) → descarta o provisório mais antigo; sem provisório → None.
- `identify(final=False)`: palpite sem efeito colateral; só responde se `sim >= threshold − 0,10` e folga sobre o 2º ≥ 0,10. `identify(final=True)` com áudio ≥ `min_audio_s`: atribui e atualiza o centróide ou cria locutor. Áudio < 0,4 s ou silêncio digital → `None`.
- `segments(audio)` (só para finais, 1× por utterance, na thread do worker; não é thread-safe): `[(início_s, fim_s, locutor)]` ladrilhando `[0, duração]`, vizinhos com locutores diferentes; 1 locutor → exatamente 1 trecho. Quedas de energia/pausas ≥ 0,12 s e mudança do locutor local do pyannote cortam; cada trecho ≥ 0,5 s vira embedding; o menor cola no vizinho; vizinhos se fundem se casam com o mesmo locutor. ~47 ms para 7–9 s de áudio.
- **speakers.json:** `{"model": "<onnx>", "profiles": [{"id", "emb" (centróide normalizado), "secs"}]}`, gravação atômica (tmp + `os.replace`). Modelo diferente ou arquivo corrompido = perfis ignorados. Voz é dado biométrico (LGPD art. 5º, II): o arquivo só existe enquanto há voz fixada (opt-in: renomear = fixar), é local e "Esquecer" o apaga. Os perfis dão continuidade (mesmo id entre sessões, rótulo desde a 1ª fala ≥ 1 s), não acurácia. `profiles_path=None` = sem persistência (testes, e2e_report).
- **Calibração:** TitaNet-small, `threshold` 0,40 (platô 0,35–0,45 em fala limpa), MAX_S 6, MIN_PIECE 0,5. `tests/eval_diar.py --sweep` e `tests/eval_split.py` medem. Nada de fusão automática de locutores: juntar é manual (a fusão automática piorou até −57 pts).
- **ERes2Net** (3D-Speaker, 26 MB): avaliado e **não adotado**. Com MAX_S=3, threshold 0,35 e MIN_PIECE=0,7 (sem isso o falso split vai a 3,4 % nas vozes reais): AMI call 91,7 % com 3 pessoas partidas (0 com MAX_S=4), mas conv_4spk 68,6 % por turno (junta as vozes TTS Aria e Natasha em qualquer threshold ≤ 0,40) e `segments()` 93 ms com 2 threads (74 ms com 4). Fica como opção para call ruim (headset Bluetooth); a receita (5 constantes) está no docstring do `diar.py`.
- **Limites:** a pessoa partida em canal de call (6–7 de 16 na AMI) continua com o TitaNet — "Juntar com" resolve na UI; troca sem pausa que o pyannote não enxerga não é cortada; sobreposição vira corte no meio dela; trecho de uma voz < 0,5 s cola no vizinho.

### `app/mt.py`
```python
PAIRS = {"en-pt": (url_tc_big, "62aeb891…", ">>pob<<", "mt-en-pt"),
         "pt-en": (url_por_eng_opus+bt-2021-04-30, "ead25b72…0a5f", None, "mt-pt-en")}
def ensure_model(models_dir="models", pair="en-pt", on_progress=None) -> str   # baixa o zip (SHA-256 fixo), converte, devolve model_dir
class Translator:
    def __init__(self, *, pair="en-pt", model_dir=None, device="auto", on_progress=None): ...
    def translate(self, text: str) -> str
    def warmup(self) -> None
```
- OPUS-MT (Marian) em CTranslate2 (GPU float16; CPU int8, 8 threads); conversão sem torch (só ctranslate2 + pyyaml). EN→PT: tc-big, o token `>>pob<<` força pt-BR; glossário mínimo (`_PRE`/`_POST`) para jargão de call/jogo/dev, medido em `tests/eval_mt.py --terms`. PT→EN: Tatoeba por-eng (download ~280 MB, 148 MB em float16), sem token de idioma; "♪♫" é apagado e o `<unk>` some; sem glossário (gíria brasileira — net, zap, travando — derruba o OPUS: chrF 52 no conjunto coloquial; Gemma 4 E2B fica para a fase 2).
- Entrada pode ser parcial (sem pontuação final, frase cortada): não inventa ponto final. Várias frases → divide e traduz em lote.

### `app/pipeline.py` / `app/main.py`
```python
class Pipeline:
    def __init__(self, out: queue.Queue, *, segmenter=None, transcriber=None, tracker=None, translator=None,
                 capture_factory=None, seg_kw=None, asr_kw=None, diar_kw=None, piece_pad_s: float = 0.0,
                 log_path=LOGS/"latency.csv"): ...   # `out` recebe Update | Status
    def start(self, device: str | None = None, *, app: str | None = None,
              call_lang: str = "auto", sub_lang: str = "pt") -> None   # volta já; reinicia se já rodava
    def stop(self) -> None                                              # idempotente, <= ~1 s
    def speaker(self, cmd: str, *args) -> None   # "pin" spk | "merge" src dst | "forget" spk | "forget_all"
    def saved_speakers(self) -> list[int]        # importa app.diar só aqui (lazy)
PROFILES = DATA_DIR / "speakers.json"; LOCK_S = 1.5; OTHER = {"pt": "en", "en": "pt"}; EXTRA_ID = 1_000_000
# capture_factory(on_audio, device=, on_status=, app=) -> captura
```
- `start()` recusa (AssertionError) `call_lang` fora de {auto, en, pt} ou `sub_lang` fora de {pt, en}. Trocar fonte ou idioma = `start()` de novo: modelos ficam em cache e as vozes fixadas continuam.
- Threads por sessão: captura → fila → thread do segmenter ("pipe-main") → inbox do **worker único** de inferência ("pipe-work", GPU compartilhada). Finais têm prioridade e **nunca** são descartados (FIFO); parciais são coalescidos (só o mais recente por `utt_id`) e descartados se o final da mesma utterance já chegou.
- **Tradução:** só 1 tradutor por sessão, `OTHER[sub_lang] → sub_lang`, carregado sob demanda e em cache por par no Pipeline (o PT→EN só é baixado/carregado quando a legenda é EN). Fala no idioma da legenda: `sub = orig`, sem MT. O translator injetado nos testes vale para qualquer par.
- **Idioma (call "auto"):** o idioma da utterance trava no 1º parcial com ≥ `LOCK_S` de áudio; `prior` = último idioma do locutor (`spk_lang`), senão o da utterance, senão o da sessão (no início, `OTHER[sub_lang]`: quem quer legenda PT provavelmente ouve EN). Cada trecho de um final dividido detecta o próprio idioma, com `prior` = o do locutor do trecho. Com call EN/PT fixa, não há detecção. **Desvio consciente do D3 do briefing:** a trava fica em 1,5 s, e não em 1 s, porque abaixo de `asr.LID_MIN_S` o ASR só devolve o prior e a trava pegaria sempre o prior.
- Parcial: ASR → locutor (`identify(final=False)` só com áudio ≥ `min_audio_s` e ainda sem rótulo; voz desconhecida só tenta de novo com 50 % mais áudio) → MT (pulada se o orig é igual ao do último parcial ou se o idioma já é o da legenda) → `Update(final=False)`.
- Final, pelo mesmo `tracker.segments(audio)` (**no lugar** de `identify(final=True)`):
  - com texto no último parcial: `segments()` vem **antes** do ASR final e, se dividir, o ASR do áudio todo nem roda (poupa ~1 ASR por divisão). Sem texto no parcial: ASR do áudio todo antes; **vazio → `Update(final=True, orig="", sub="")`** (a UI apaga a linha) **sem consultar os locutores**, para ruído não criar locutor fantasma.
  - 1 trecho: ASR (beam 5) do áudio todo; rótulo = o do trecho (sem voz nova, mantém o palpite do parcial); MT pulada se o orig é igual ao do último parcial.
  - > 1 trecho: cada `audio[a:b]` (± `piece_pad_s`, padrão 0) é transcrito com `final=True`, traduzido e vira **1 `Update(final=True)` por trecho, em ordem, no mesmo job**. O 1º trecho com texto reaproveita o `utt_id` do evento; os demais usam ids a partir de `EXTRA_ID`. `t0` = `max(ev.t0, ev.t_end − dur + a)`, `t_end` = `ev.t_end − dur + b`. Se o ASR quebrar num trecho, ou todos vierem vazios, volta ao fluxo de 1 trecho.
  - `segments()` lança exceção → log + `Status` de erro limitado (1 a cada 5 s) + fallback com `identify(final=True)`.
- **`speaker()`** roda sob o lock de inferência (o tracker não é thread-safe). Sem tracker carregado, só `forget`/`forget_all` têm efeito, via `diar.forget_saved(PROFILES)`. Erros viram `Status("Erro em locutores: …", "error")`. O `SpeakerTracker` recebe `profiles_path=PROFILES`; `tracker.save()` roda quando o worker da sessão sai (sob o lock), para o `stop()` não esperar o job em andamento.
- Erros de um job: logar + `Status(level="error")` e seguir (ASR falhou no final → fecha com o último parcial; MT falhou → `sub=""`, a UI mostra o orig; locutor falhou → mantém a legenda). Latências por final em `logs/latency.csv` (`utt_id,dur_s,asr_ms,spk_ms,mt_ms,lag_ms`).
- **Mensagens:** `Carregando modelos…` → (`Baixando … (n/4)`) → `Pronto — escutando <device_name>` (`ready`; com app, o nome do app, e só quando a captura abriu de fato). Erros fatais começam com "Falha" ou "Erro ao iniciar" (a UI encerra a sessão com eles).
- `app/main.py`: `python -m app.main` monta `App(out, get_devices, get_apps=list_audio_apps if APP_CAPTURE else None, get_speakers=pipe.saved_speakers, on_start=pipe.start, on_stop=pipe.stop, on_speaker=pipe.speaker, autostart=AUTOSTART)`. `SEG_KW`, `DIAR_KW`, `ASR_KW` no topo = calibração. O autostart é da UI (só depois do onboarding). Log em `logs/app.log`.

### `app/ui.py` + `app/ui_overlay.py` + `app/ui_i18n.py`
```python
class App:
    def __init__(self, out_q, *, get_devices, get_apps, get_speakers, on_start, on_stop, on_speaker, autostart=False): ...
    def run(self) -> None   # mainloop; ao fechar chama on_stop
```
- tkinter puro, sem dependências. A UI **não importa** audio/pipeline/diar: só `app.events`, `app.paths` e os próprios `ui_*`. `ui.py` = App, telas, transcrição, demo e stress; `ui_overlay.py` = tokens, gerador de PNG, Kit de controles e Overlay; `ui_i18n.py` = strings EN/PT e `system_lang`.
- Chamadas fora da thread do Tk, no executor `ui-ctl` (1 worker, em ordem): `get_apps`, `get_speakers`, `on_start(device=, app=, call_lang=, sub_lang=)`, `on_stop` e `on_speaker(cmd, *args)`. O resultado volta pela própria `out_q` como um callable que o `_drain` executa. `get_devices` roda na thread do Tk.
- Exceção em `on_start`, ou `Status(error)` que começa com "Falha"/"Erro ao iniciar", encerra a sessão (chama `on_stop`): volta à principal com o erro e, se a fonte é um app, oferece "Usar a saída inteira".
- **Telas:** 1ª abertura (sem `ui_lang`): idioma do app → tutorial de 4 passos (pulável; rever em Configurações › Geral). Principal (960×680): cards ÁUDIO (Saída inteira | Um app) e IDIOMAS (call Auto/EN/PT, legenda PT/EN, "Detectado" vindo do `Update.lang` do último final), pílula de status e botões. Ao iniciar, a principal some e fica o overlay. Configurações: Geral, Legenda, Locutores, Áudio e Modelos. Janela de transcrição: .txt, .srt e Limpar (mantém a lógica O(1) com marcas; poda em 2.000 linhas; cabeçalho de locutor só quando ele muda).
- **Overlay** (`ui_overlay.Overlay`): `overrideredirect`, cantos do DWM (33) e borda (34). Vidro acrylic (`SetWindowCompositionAttribute` ACCENT 4, não documentada) ligado por padrão no Win11 (build ≥ 22000): a opacidade vira a tinta (`alpha − 0,25`, 88 % → 63 % de #0D0B1E), texto 100 % opaco; se a API falha, cai para `-alpha`. `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` ligado por padrão (a legenda não aparece no compartilhamento de tela nem em prints). Resize pelas 8 bordas (hit-test de 8 px) e arrasto pelo meio; duplo clique encaixa embaixo, no centro (MonitorFromWindow). Controles só no hover; Ctrl +/− = fonte; Esc = principal. De 1 a 3 falas ancoradas embaixo (o que não cabe na altura sai): parcial em muted → final em text; a fala nova entra de baixo e a anterior sobe e esmaece (6 × 25 ms) até 50 %. Clicar no nome renomeia; o clique direito abre o menu (Juntar com ▸, Esquecer voz, …).
- **Visual:** tokens, cores de locutor presas ao id (`id % 8`; None = #8E8BA8) e tipografia do relatório de UI. Fonte Outfit (OFL, `app/assets/Outfit.ttf` + `OFL.txt`) via `AddFontResourceExW(FR_PRIVATE)`; texto em Segoe UI Variable Text; ícones do Segoe Fluent Icons / MDL2 (glifo de texto fora do Windows; nada de emoji). **Os PNGs anti-aliased (pills, cards com glow, toggles, pontos) são gerados em tempo de execução por `ui_overlay.shape()`**, só com stdlib (zlib/struct), em cache por tamanho — as larguras dependem do texto traduzido e do DPI, então não há `scripts/make_ui_assets.py` nem PNG versionado. Medido: abrir + 1ª tela 378 ms (inclui o Tk), redesenhar a principal 11 ms, Configurações na 1ª vez 216 ms, 44 imagens em cache. `Kit.bind`/`Kit.reset` desfazem os binds de cada redesenho (senão os comandos Tcl acumulam).
- **Status (D13):** os rótulos `info`/`download`/`ready` aparecem no idioma da UI (em PT, o texto do pipeline; em EN, "Loading models…", "Downloading models (n/m)… 42 %", "Ready — listening to <app/dispositivo>"); `warn`/`error` aparecem crus. `# ponytail:` erros só em pt-BR.
- **`settings.json` (dono único: a UI; o main e o pipeline não leem):** `ui_lang`, `call_lang`, `sub_lang`, `source` ("all"|"app"), `device`, `audio_app` (exe / bundle id), `main_geometry`, `overlay{geometry, font 16–56 = 28, alpha .6–1 = .88, glass, topmost = true, show_orig = true, lines 1–3 = 2, hide_capture}` e `speakers{"<id>": "Nome"}`. Chaves antigas são ignoradas. Ao abrir, nomes de ids que não estão em `get_speakers()` são descartados.
- **Locutores:** renomear = `on_speaker("pin", id)` + nome salvo; juntar = `on_speaker("merge", src, dst)` e a UI troca src por dst nas linhas já mostradas e nos updates atrasados (alias; o nome salvo migra para o dst). Alias, a lista do "Juntar com" e o clique no nome valem só na sessão atual: o `diar.reset()` de cada início reaproveita os ids (recomeçam em max(fixados)+1), então numa sessão nova o mesmo id pode ser outra pessoa e o nome das linhas antigas deixa de ser clicável. O campo de renomear é criado no `after_idle`, porque o `_press` do canvas roda depois do bind do item e põe o foco na janela; clicar fora confirma, e confirmar sem mudar o nome não fixa a voz. Esquecer = `"forget"`; "Esquecer todas" = `"forget_all"`.
- **Checks:** `python -m app.ui --stress` (lógica das linhas, locutores, .srt, overlay: vidro com fallback, resize e arrasto via `event_generate`, sem vazamento de binds; rajadas de 2 × 3.000 updates; falha com travada > 50 ms; juntar → sessão nova → o id antigo continua cru; clicar no nome dá o foco ao campo e clicar fora fecha o campo; escolher o idioma no onboarding traduz o status), `python -m app.ui_overlay` (contraste ≥ 4,5:1 de todos os tokens de texto e cores de locutor; PNG válido), `python -m app.ui_i18n` (mesmas chaves nas duas línguas). Demo: `python -m app.ui --demo [--first-run]` (grava numa cópia temporária do `settings.json`, como o `--stress`).

## Medido (04/10/2026, RTX 4080 SUPER, i7-14700K)

### Ponta a ponta (`tests/e2e_report.py`, fixtures em tempo real, pipeline inteiro, máquina livre, rodada da integração)
Metas: locutor ≥ 95 % (2spk) / ≥ 90 % (4spk, misto), WER ≤ 3 % (PT ≤ 4 %), idioma das linhas ≥ 95 %, atraso mediano do final ≤ 500 ms e do parcial ≤ 300 ms. **Todas cumpridas**; o teto de 550 ms do briefing não foi necessário.

| fixture | locutor (por tempo) | WER do orig | idioma das linhas | atraso do final, ms: mediana (p90 / máx) | atraso do parcial, ms: mediana (p90 / máx) |
|---|---|---|---|---|---|
| conv_2spk (sub=pt) | 99,1 % | 0,0 % | 100 % | 456 | 141 |
| conv_4spk (sub=pt) | 94,8 % | 0,3 % | 100 % | 461 (593 / 695) | 141 (203 / 297) |
| conv_2spk_noisy (sub=pt) | 98,9 % | 0,0 % | 100 % | 459 (517 / 617) | 141 (188 / 219) |
| conv_pt_2spk (sub=en) | 97,3 % | 1,5 % | 100 % | 440 (497 / 507) | 141 (187 / 219) |
| misto, auto (conv_2spk + 1 s + conv_pt_2spk, sub=pt) | 98,1 % | 0,7 % | 100 % | 455 (507 / 648) | 125 (184 / 219) |

- Atraso = `t_ready − t_end` da **última** linha do final: ~300 ms de espera do silêncio de fim de frase + ASR + locutores + MT. Por final (mediana): ASR 152–205 ms, locutores 44–61 ms, MT 20–40 ms (0 no misto com legenda PT: a parte PT não traduz). Os finais divididos por locutor pagam 1 ASR + 1 MT a mais por trecho (conv_4spk: 539 ms contra 429 ms dos de 1 locutor).
- Contra a versão anterior (distil-large-v3.5, só EN): +0–20 ms no final (conv_2spk 440 → 456, conv_4spk 457 → 461) e +16 ms no parcial (125 → 141). A rodada do Forja-Idiomas deu 471 / 458 / 450 / 443 ms (final) — a variação entre rodadas é de ~±15 ms.
- Com o provisório, a 1ª fala de cada voz nova (< 3 s somados) sai com `speaker=None`: a acurácia de locutor por tempo cai ~1 a 3 pts nos fixtures (eval_split: conv_4spk 98,7 → 95,9 %, total 99,2 → 97,9 %; e2e conv_4spk 97,8 → 94,8 %).
- **Sensível à CPU:** com a CPU a 88 % (varreduras paralelas de outro agente) o final foi a 586–1146 ms e os locutores a 127–714 ms, por causa do `segments()` na CPU.
- **App real** (`python -m app.main`, settings de teste, wav tocado na saída padrão por um `powershell.exe` com `Media.SoundPlayer`): "Pronto — escutando …" 3,6 s depois de abrir (modelos em cache; 8 s com captura por app esperando o app-alvo abrir); legenda no overlay (`docs/app-live.png`); atraso do final no `latency.csv` com mediana 437–450 ms (máx 507–516) em 4 sessões: saída inteira EN→PT, captura por app PT→EN e EN→PT; `logs/app.log` sem erro. Captura por app: com o app-alvo fechado o overlay mostra "powershell não está aberto — esperando…" e a legenda começa sozinha quando ele toca.
- Limites da medição: TTS neural limpo, sem sobreposição de falas; voz real de call separa menos (calibrar `threshold`, "Juntar com").

### ASR (`tests/bench_asr.py`, 188 turnos EN+PT, detecção como no app)
| | EN | PT |
|---|---|---|
| WER total (turbo, GPU) | 1,3 % | 2,2 % |
| 2spk | 0,0 | 1,5 |
| 4spk / 3spk | 0,5 | 0,6 |
| ruidoso | 0,0 | 1,5 |
| difícil (reverb + burburinho a 8 dB) | 3,8 | 6,1 |

- LID 188/188 (turbo e Parakeet). Latência do turbo (mediana de 2 rodadas): parcial 4 s 125–129 ms; final 4 s 137–138 ms; final 14 s 249–255 ms; 4 s PT 128–136 / 142–148 ms (parcial/final). Contra o distil: +20–25 ms no 4 s e +70 ms no 14 s.
- Parakeet int8, CPU, 8 threads: WER EN 5,3 % (difícil 13,0), PT 5,6 % (difícil 19,4); parcial 4 s de 0,18 s (CPU livre) a 0,58 s (CPU disputada); final 14 s ~0,8 s.
- Alucinações: 52/52 turnos de fala real preservados com os filtros PT novos; os filtros só foram testados em ruído sintético.

### Tradução (`tests/eval_mt.py`)
- EN→PT (padrão): chrF 76,5, sem mudança. PT→EN (`--dir pten`, 55 frases): chrF 78,4 na GPU, 77,9 na CPU; latência 13/33/67 ms (GPU) e 17/39/79 ms (CPU) para 4/12/25 palavras.

### Locutores (`tests/eval_diar.py`, `tests/eval_split.py`, AMI no scratchpad)
| teste | antes | depois (TitaNet + provisório) |
|---|---|---|
| AMI call (4 reuniões, 16 pessoas): acurácia | 84,4 % | 87,5 % |
| AMI call: rótulos por sessão | 8,0 | 6,0 |
| AMI call: fantasmas | 17 | 10 |
| AMI call: pessoas partidas | 6 | 7 (meta ≤ 3 **não atingida**; nenhum threshold 0,30–0,40 nem confirm_s 3–5 desce de 4) |
| AMI clean: acurácia / rótulos | 90,5 % / 8,0 | 93,3 % / 5,0 |
| eval_diar por turno: conv_2spk / conv_4spk / noisy | 100 / 91,4 / 95,2 % | 95,2 / 80,0 / 90,5 % (precisão entre os rotulados 100 %) |
| eval_split: acurácia por tempo / falso split | 99,2 % / 0,1 % | 97,9 % / 0,1 % |
| `segments()` de 7–9 s (mediana) | 47 ms | 46–47 ms (p95 ~76) |

- Meta do conv_4spk no eval_diar: 85 → 80 % (os "?" das primeiras falas curtas são de propósito; o resto é artefato do TTS: vozes Aria/Natasha com cosseno 0,65–0,75).
- Perfis: fixar → reiniciar a sessão → a mesma voz volta com o mesmo id já na 1ª fala de 1,2 s (`check_profiles()` no início do `eval_diar`).

### Captura por app (`tests/test_audio.py`, Windows 11 build 26300, dois processos tocando 440 e 1000 Hz)
- Isolamento de 99,97 % e 99,84 % da energia na banda do alvo, vazamento do outro processo ≤ 0,0003 %, nível exato (RMS 0,1414); blocos de 512 amostras com passo mediano de 32,0 ms (31,7–32,5); a captura volta 0,44–0,50 s depois de o app reabrir; `list_audio_apps()` 6–16 ms; atraso fixo de +32–38 ms em relação à saída inteira.

### Riscos conhecidos
- Windows 10 (19041–20347) sem teste de captura por app; apps cujo som sai de outro exe (Teams novo → `msedgewebview2.exe`) aparecem com o exe que toca; mixers (Voicemeeter) aparecem na lista.
- Mac sem teste: helper `--app` (o áudio do Discord/Chrome sai pelo `.helper`?; se o SCK derrubar o stream quando o app fecha, o helper sai com 2 em vez de 6 e aparece "A captura do sistema parou" antes do aviso), Parakeet arm64, overlay só com `-alpha`, fonte via `ATSApplicationFontsPath`, atalhos de teclado num `overrideredirect`.
- Vidro acrylic usa API não documentada (fallback automático); resize do overlay testado com eventos simulados, não com mouse físico.
- Filtros de alucinação PT sem números de uso real; PT→EN sem glossário de gíria.
