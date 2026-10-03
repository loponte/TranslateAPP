# TranslateAPP — arquitetura e contratos entre módulos

App (Windows e macOS) que captura o áudio que sai do computador (Windows: WASAPI loopback; macOS: ScreenCaptureKit; Discord, Meet, Teams, Zoom, YouTube…), transcreve **inglês** com **Whisper**, separa por **locutor**, traduz para **pt-BR** e mostra legendas ao vivo numa janela overlay. 100% local (sem APIs externas, sem Claude), foco em **menor latência possível**.

> Whisper só traduz *para* inglês, nunca para português. Por isso: Whisper = transcrição EN; tradução EN→PT-BR = modelo de MT local (OPUS-MT em CTranslate2, licença CC-BY-4.0, atribuição ao Helsinki-NLP/OPUS-MT em `models\mt-en-pt`).

## Ambiente
- Windows 11 · i7-14700K (28 threads) · 32 GB · **RTX 4080 SUPER 16 GB** · saídas de áudio: Realtek, LG HDR WFHD (HDMI/DP), NVIDIA HDMI.
- Python 3.12 em `.venv` → `.venv\Scripts\python.exe`. Rode sempre a partir da raiz do projeto (`python -m app.x`, `python tests\x.py` com `PYTHONPATH` = raiz). `setup.ps1` cria o `.venv`, instala o `requirements.txt`, cria o atalho `TranslateAPP.lnk` e baixa os modelos; `run.bat` / o atalho abrem o app sem console (`pythonw -m app.main`, log em `logs\app.log`).
- Pacotes (`requirements.txt`, só o que `app\` importa): numpy 2.5, faster-whisper 1.2.1 (ctranslate2 4.8.2 com CUDA), onnxruntime 1.30, sherpa-onnx 1.13.8, PyAV (`av`, reamostragem), pyaudiowpatch, sentencepiece, pyyaml (conversão do OPUS-MT), nvidia-cublas-cu12, nvidia-cudnn-cu12; tkinter (stdlib). `soxr` não é usado.
- CUDA no Windows: `from app.cuda_dlls import setup_cuda_dlls; setup_cuda_dlls()` **antes** de importar ctranslate2/faster_whisper (float16 e int8_float16 funcionam na GPU).
- Pacote extra (sacrebleu, torch/transformers para converter modelo…) só em ambiente descartável: `uv run --no-project --with <pkg> python script.py`; o `.venv` fica só com o `requirements.txt`.
- Modelos em `models\` (raiz do projeto), baixados por `setup.ps1` / `ensure_model()`, nunca versionados:
  `whisper\` (distil-large-v3.5 ~1,5 GB; small.en de reserva), `spk\` (`nemo_en_titanet_small.onnx` 38 MB = embedding; `pyannote-segmentation-3-0.onnx` 6 MB = troca de locutor), `silero_vad.onnx` (copiado do faster-whisper), `mt-en-pt\` (OPUS-MT tc-big, CTranslate2 float16, ~450 MB).
- Fixtures de teste em `tests\data\` (ver `tests\data\README.md`): `conv_2spk.wav`, `conv_4spk.wav`, `conv_2spk_noisy.wav` (16 kHz mono), `conv_2spk_48k_stereo.wav`, e `<nome>.json` com gabarito `{"voices":…, "turns":[{"speaker","text","start","end"}]}`.

## Plataformas, pastas de dados e empacotamento
- `app/paths.py`: `DATA_DIR`, `MODELS`, `LOGS`, `SETTINGS`. Empacotado (PyInstaller, `sys.frozen`): `%LOCALAPPDATA%\TranslateAPP` (Win) ou `~/Library/Application Support/TranslateAPP` (mac); rodando do código: a raiz do repo (os testes não mudam). `main.py` faz `os.chdir(DATA_DIR)`, então `models/` e `logs/` relativos continuam valendo.
- Primeiro uso: modelos ausentes são baixados pelos próprios `ensure_model(..., on_progress)` (asr/mt/diar; o callback só dispara se há download; `None` = sem %). O `Pipeline._load` converte isso em `Status("Baixando <modelo>… (n/4)", "download", progress)`; `ready` limpa. Whisper (HF) é indeterminado. No .exe com NVIDIA, `cuda_dlls.ensure_cuda()` baixa cuBLAS/cuDNN (versões fixas, SHA-256 conferido) do PyPI para `DATA_DIR/cuda.tmp` e renomeia para `cuda` só no fim (`.ok`; `setup_cuda_dlls` só usa a pasta completa); falhou = o pipeline força `device="cpu"` no Whisper e no tradutor; sem GPU = CPU.
- `app/audio.py` escolhe o backend por `sys.platform` (mesma interface; o `_pump` mono/16 kHz/grade de 32 ms é comum). macOS (`app/audio_mac.py`): dispositivo "Áudio do sistema" = helper Swift `native/sck_audio.swift` (SCStream, só áudio, 48 kHz estéreo f32 no stdout; sai com código 3 sem permissão); os outros = entradas via `sounddevice` (ex.: BlackHole). O helper é compilado com `swiftc` no build e vai em `Contents/Frameworks` do .app. `list_loopback_devices()` no mac devolve "Áudio do sistema" primeiro.
- Build: `packaging/TranslateAPP.spec` (onedir, sem console; sem nvidia-*, sem modelos), `scripts/build_mac.sh` (swiftc + pyinstaller + `codesign -s -` ad-hoc + zip) e `scripts/build_win.ps1`; CI em `.github/workflows/build.yml` (matriz windows/macos: testes, build, `--selftest`, artefatos; tag `v*` = Release). `main.py --selftest` importa tudo, cria o VAD, lista dispositivos (sem áudio não falha) e abre/fecha um Tk oculto. `multiprocessing.freeze_support()` em `main.py` é obrigatório (senão o resource_tracker reabre o app em cascata).

## Fluxo
```
LoopbackCapture ─chunks 32 ms (16 kHz mono f32)→ Segmenter ─SegEvent(start|partial|final)→ Pipeline (worker único) ─Update/Status→ queue.Queue → UI (tkinter)

parcial: ASR (beam 1) → identify(final=False) (palpite, rótulo em cache por utterance) → MT
final:   com texto no último parcial (fala confirmada):
           SpeakerTracker.segments() → 1 trecho : ASR (beam 5, áudio todo) → MT → 1 Update (utt_id do evento)
                                     → N trechos: ASR (beam 5) de cada trecho → MT → N Updates (1º com o utt_id do evento, os outros inéditos)
         sem texto no parcial: ASR do áudio todo primeiro; vazio = ruído → descarta sem consultar os locutores; senão, como acima
```

## Convenções
- Áudio interno: `np.float32`, mono, 16 kHz, [-1, 1]. Tempo: `time.monotonic()`. Tipos compartilhados em `app/events.py` (`SR`, `SegEvent`, `Update`, `Status`; `Status.level` inclui `"download"` e `Status.progress: float | None` = 0..1 do download) — não editar sem avisar.
- Código enxuto: stdlib/pacotes instalados primeiro; sem abstrações além do contrato (sem classes base, sem factory, sem config global — cada módulo recebe kwargs com defaults); sem dependência nova sem necessidade.
- Cada módulo deixa UM check executável (`tests\test_<modulo>.py` com `assert`, ou `if __name__ == "__main__"`).
- Comentários em pt-BR, curtos. Identificadores em inglês. Textos de UI/status em pt-BR com acentuação correta. Sem emoji em `print` (console cp1252).
- Simplificação consciente com limite conhecido → comentário `# ponytail: <limite>, <upgrade>`.
- Cada agente edita **só os seus arquivos** (tabela abaixo). `app/events.py`, `app/cuda_dlls.py`, `app/__init__.py` são do orquestrador.

| Agente | Arquivos |
|---|---|
| A áudio+segmentação | `app/audio.py`, `app/audio_mac.py`, `native/`, `app/segmenter.py`, `tests/test_segmenter.py`, `tests/test_audio.py` |
| B ASR | `app/asr.py`, `tests/bench_asr.py` |
| C locutores | `app/diar.py`, `tests/eval_diar.py`, `tests/eval_split.py` |
| D tradução | `app/mt.py`, `scripts/` (conversão de modelo), `tests/eval_mt.py` |
| E UI | `app/ui.py`, `docs/ui-*.png` |
| F/G pipeline + integração | `app/pipeline.py`, `app/main.py`, `app/paths.py`, `packaging/`, `scripts/build_*`, `.github/`, `run.bat`, `setup.ps1`, `README.md`, `ARCHITECTURE.md`, `requirements.txt`, `tests/test_pipeline.py`, `tests/e2e_report.py`, `docs/app-live.png` |

## Contratos

### `app/audio.py`
```python
@dataclass
class LoopbackDevice:
    index: int; name: str; is_default: bool

def list_loopback_devices() -> list[LoopbackDevice]   # saídas WASAPI com loopback; padrão do Windows primeiro

class LoopbackCapture:
    def __init__(self, on_audio: Callable[[np.ndarray, float], None], device: str | None = None,
                 on_status: Callable[[str], None] | None = None, block_ms: int = 32): ...
    def start(self) -> None      # não bloqueia
    def stop(self) -> None       # idempotente; espera as threads
    device_name: str             # dispositivo realmente em uso (preenchido ~0,1 s depois de start)
```
- `on_audio(chunk, t_end)`: `chunk` float32 mono 16 kHz com exatamente `block_ms*16` amostras (512 @ 32 ms, o que o Silero v5/v6 quer); `t_end` = `time.monotonic()` do fim do chunk, numa grade ancorada no relógio. Chamado numa thread de captura; deve voltar rápido.
- WASAPI loopback **não entrega nada quando nada toca** → o módulo gera chunks de zeros no ritmo do relógio (a linha do tempo nunca para; o Segmenter depende disso para fechar utterances). Salto do relógio (suspensão) realinha a grade em vez de despejar zeros.
- `device=None` → saída padrão do Windows; se o padrão mudar com o app rodando, reabre sozinho. `device="texto"` → nome exato, senão substring (sem diferenciar maiúsculas). Dispositivo some/erro → tenta reabrir a cada 1–2 s e avisa por `on_status` (pt-BR; "Capturando áudio de: X" quando (re)abre).
- Qualquer nº de canais/taxa (44,1/48/96 kHz; estéreo/5.1/7.1) → mono (média; 5.1/7.1: FL/FR + 0,7·centro) → 16 kHz com **PyAV/swr** (~1 ms de atraso fixo; o soxr tinha 11–43 ms). O callback do PortAudio só enfileira; resample/entrega em thread própria; nunca perde blocos.

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
- Eventos por utterance (`utt_id` incremental desde 0, **continua entre `reset()`s** para a UI nunca reusar um id; `t0` = início da fala; `t_end` = fim do último áudio incluído):
  - `start` (1×, quando acumulou `min_speech_ms` de fala; `audio=None`; o pipeline ignora);
  - `partial` (a cada `partial_every_ms` de fala, só após `min_partial_ms`; `audio` = utterance inteira até agora, com preroll);
  - `final` (silêncio ≥ `end_silence_ms` — `end_silence_long_ms` quando a utterance já passou de `long_after_s` — ou utterance ≥ `max_utt_s`: corta na última pausa ≥ ~100 ms dos últimos ~4 s, senão corte seco; o resto vira a próxima utterance). `final.audio` = preroll + fala + `tail_ms`.
- Rajadas < `min_speech_ms` = ruído: descartadas sem eventos.

### `app/asr.py`
```python
class Transcriber:
    def __init__(self, *, model: str = "auto", device: str = "auto", compute_type: str = "auto",
                 language: str = "en", models_dir: str = "models", cpu_threads: int = 0, on_progress=None): ...
    def warmup(self) -> None                                   # 2 inferências dummy (beam 1 e final): cuDNN/cuBLAS/alocador
    def transcribe(self, audio: np.ndarray, final: bool = False) -> str   # "" se vazio ou alucinação
    model_name: str; device: str; compute_type: str; beam_final: int = 5
def ensure_model(models_dir: str = "models", model: str = "auto", on_progress=None) -> str   # baixa para models/whisper; idempotente
```
- `final=False` (parcial): beam 1, sem timestamps, sem condition_on_previous_text, temperatura 0. `final=True`: beam 5 (mesmo WER e latência que 3; 1 piora no difícil). Idioma fixo `en`.
- Filtros: áudio < 0,3 s ou RMS < 0,0015, texto vazio, `avg_logprob` < −1, `compression_ratio` > 2,4, alucinações fixas ("thanks for watching"…) e falas comuns ("Thank you.", "bye") só com o modelo inseguro (logprob < −0,3).
- `model="auto"`: GPU → **distil-large-v3.5 float16** (WER 1,0 %; parcial de 4 s ~100 ms, final de 4 s ~110 ms, de 14 s ~185 ms); CPU → small.en int8 (WER 2,7 %, parcial de 4 s ~0,9 s). Detalhes e a tabela dos candidatos no cabeçalho do módulo (`tests/bench_asr.py`).

### `app/diar.py`
```python
class SpeakerTracker:
    def __init__(self, *, threshold: float = 0.40, min_audio_s: float = 1.0, max_speakers: int = 8,
                 models_dir: str = "models", on_progress=None): ...
    def identify(self, audio: np.ndarray, final: bool) -> int | None
    def segments(self, audio: np.ndarray) -> list[tuple[float, float, int | None]]
    def reset(self) -> None
def ensure_model(models_dir: str = "models", on_progress=None) -> str   # baixa os 2 modelos para models/spk
```
- Dois modelos ONNX em CPU: **TitaNet-small** (embedding de 192 dims, sherpa-onnx) + **pyannote-segmentation-3.0** (quem fala em cada quadro de ~17 ms; só em `segments()`). Agrupamento online por similaridade de cosseno com centróides (média ponderada pela duração, memória ~60 s). Locutores recebem índice 0-based na ordem de aparição, estável.
- `identify(final=False)`: palpite sem efeito colateral (não cria locutor nem atualiza centróide); só responde se `sim >= threshold − 0,10` e folga sobre o 2º ≥ 0,10; senão `None` (= "Locutor ?" na UI). `identify(final=True)` com áudio ≥ `min_audio_s`: atribui e atualiza o centróide ou cria locutor (lotado em `max_speakers` → `None`). Áudio < 0,4 s ou silêncio digital → `None`.
- `segments(audio)` (só para utterances **finais**, 1× por utterance, em ordem, na mesma thread que chama `identify`; não é thread-safe): devolve `[(início_s, fim_s, locutor)]` relativos ao começo de `audio`, ladrilhando `[0, duração]`; vizinhos têm locutores diferentes. Utterance de 1 locutor → exatamente 1 trecho, com o rótulo e os centróides do `identify(audio, True)`. Locutor `None` num trecho = voz diferente da vizinha mas sem identidade ainda (curto, tabela cheia): a UI mostra "?" — **não** herdar o rótulo do vizinho. Custo ~47 ms para 7–9 s de áudio (CPU).
- Como acha as trocas: quedas de energia/pausas ≥ 0,12 s e mudança do locutor local do pyannote cortam; cada trecho ≥ 0,5 s vira embedding; o menor cola no vizinho; vizinhos se fundem se casam com o mesmo locutor (só o embedding decide). Medido nos fixtures: acurácia de locutor por tempo 78 % → 99 %, falso split 0,1 %.
- Calibração: `threshold` vale para as duas funções (TitaNet-small: **0,40**, platô 0,35–0,45 em fala limpa; alto demais divide a mesma voz, baixo demais junta vozes parecidas). `tests/eval_diar.py --sweep` e `tests/eval_split.py` medem.
- Limites: troca sem pausa que o pyannote não enxerga (mesmo id local) não é cortada; sobreposição vira corte no meio dela; trecho de uma voz < 0,5 s ("yeah" solto) cola no vizinho; vozes parecidas não se separam; locutor novo de trecho < `min_audio_s` sai `None`.

### `app/mt.py`
```python
class Translator:
    def __init__(self, *, model_dir: str = "models/mt-en-pt", device: str = "auto", on_progress=None): ...
    def translate(self, text: str) -> str          # EN -> PT-BR
    def warmup(self) -> None
def ensure_model(models_dir: str = "models", on_progress=None) -> str  # baixa o zip oficial (~860 MB, SHA-256 fixo), converte e devolve model_dir
```
- **OPUS-MT tc-big** (Helsinki-NLP, Marian) em CTranslate2 (GPU float16; CPU int8, 8 threads). O token `>>pob<<` força português do Brasil (`>>por<<` mistura Portugal). ~60 ms para 25 palavras na GPU. Conversão sem torch (só ctranslate2 + pyyaml).
- Entrada pode ser parcial (sem pontuação final, frase cortada, minúsculas): não inventa ponto final. Várias frases → divide e traduz em lote. Glossário mínimo (pré/pós-processamento) para jargão de call/jogo/dev (deploy, pull request, unmute, lag, ping, bug, nerf…), medido em `tests/eval_mt.py --terms`.

### `app/pipeline.py` / `app/main.py`
```python
class Pipeline:
    def __init__(self, out: queue.Queue, *, segmenter=None, transcriber=None, tracker=None, translator=None,
                 capture_factory=None, seg_kw=None, asr_kw=None, diar_kw=None, piece_pad_s: float = 0.0,
                 log_path=LOGS/"latency.csv"): ...   # `out` recebe Update | Status
    def start(self, device: str | None = None) -> None      # volta já; modelos e captura em thread
    def stop(self) -> None                                   # idempotente, <= ~1 s
EXTRA_ID = 1_000_000
```
- Threads por sessão: captura → fila → thread do segmenter ("pipe-main") → inbox do **worker único** de inferência ("pipe-work", GPU compartilhada). Política do inbox: finais têm prioridade e **nunca** são descartados (FIFO); parciais são coalescidos (só o mais recente por `utt_id`) e descartados se o final da mesma utterance já chegou.
- Parcial: `asr.transcribe(audio, False)` → locutor (`identify(final=False)` só com áudio ≥ `min_audio_s` e ainda sem rótulo na utterance; o rótulo fica em cache; voz desconhecida só tenta de novo com 50 % mais áudio) → `translate` (pulado se o EN é igual ao do último parcial) → `Update(final=False)`.
- Final, pelo mesmo `tracker.segments(audio)` (**no lugar** de `identify(final=True)`; chamado 1× por final, na ordem, na thread do worker):
  - com texto no último parcial a fala já está confirmada: `segments()` vem **antes** do ASR final e, se dividir, o ASR do áudio todo nem roda (poupa ~1 ASR por divisão: ~130 ms). Sem texto no parcial (utterance curta, ou o parcial foi pulado) o ASR do áudio todo vem antes: **vazio → `Update(final=True, en="", pt="")`** (a UI apaga a linha parcial) **sem consultar os locutores**, para ruído que o Whisper não transcreve (risos, notificações) não criar locutor fantasma. Limite: parcial com texto e final vazio (raro) também consulta os locutores.
  - 1 trecho: fluxo de sempre: ASR (beam 5) do áudio todo; rótulo = o do trecho (sem voz nova, mantém o palpite do parcial); MT pulada se o EN é igual ao do último parcial; final vazio → descarte.
  - > 1 trecho: cada `audio[a:b]` (± `piece_pad_s`, padrão 0: folga de 0,05–0,15 s não mudou o WER e ≥ 0,25 s vaza palavras do vizinho) é transcrito com `final=True`, traduzido e vira **1 `Update(final=True)` por trecho, em ordem, no mesmo job** (saem juntos, antes de qualquer parcial da utterance seguinte). Trecho com ASR vazio é pulado. O 1º trecho com texto reaproveita o `utt_id` do evento (troca a linha parcial); os demais usam `utt_id` inéditos (contador por sessão a partir de `EXTRA_ID`; o Segmenter conta de 0). `t0` = `max(ev.t0, ev.t_end − dur + a)`, `t_end` = `ev.t_end − dur + b` (então o "atraso" = `t_ready − t_end` das linhas que não são a última do final inclui o resto da frase). Locutor `None` fica `None`. Se o ASR quebrar num trecho, ou todos vierem vazios, volta ao fluxo de 1 trecho com o EN do áudio todo (se este também vier vazio: descarte).
  - `segments()` lança exceção → log + `Status` de erro limitado (1 a cada 5 s) + fallback: utterance inteira com `identify(final=True)`.
- Erros de um job: logar + `Status(level="error")` e seguir (ASR falhou no final → fecha com o último parcial; MT falhou → mantém o EN; locutor falhou → mantém a legenda). `stop()` sem deadlock. Latências por final (1 linha por utterance, não por trecho) em `logs/latency.csv` (`utt_id,dur_s,asr_ms,spk_ms,mt_ms,lag_ms`).
- `app/main.py`: `python -m app.main` monta UI + Pipeline (`SEG_KW`, `DIAR_KW`, `ASR_KW` no topo = calibração); `AUTOSTART = True` começa a escutar ao abrir. Carrega modelos em background: `Status("Carregando modelos…")` → `Status("Pronto — escutando …", "ready")`. Log em `logs/app.log`.

### `app/ui.py`
```python
class App:
    def __init__(self, out_q: queue.Queue, *, get_devices: Callable[[], list[str]],
                 on_start: Callable[[str | None], None], on_stop: Callable[[], None]): ...
    def run(self) -> None        # mainloop; ao fechar chama on_stop
```
- tkinter puro (+ ttk), sem dependências, DPI-aware. Consome `Update`/`Status` de `out_q` com `after(~30 ms)`; 1 linha por `(época, utt_id)` na ordem de chegada; cabeçalho de locutor colorido só quando o locutor muda. Não importa audio/pipeline (recebe callbacks). `python -m app.ui --demo` roda com dados falsos; preferências em `settings.json`.

## Medido (fixtures em tempo real, `tests/e2e_report.py`, pipeline inteiro, GPU livre)
Antes = 1 rótulo por utterance (`identify(final=True)`); depois = `segments()` + 1 linha por trecho (código atual). Todas as metas cumpridas (locutor ≥ 95 % no 2spk e ≥ 90 % no 4spk, WER ≤ 3 %, atraso do final ≤ 500 ms e do parcial ≤ 300 ms, ambos pela mediana).

| fixture | locutor (por tempo) | WER do EN | linhas finais / turnos | atraso do final, ms: mediana (p90 / máx) | atraso do parcial, ms: mediana (p90 / máx) |
|---|---|---|---|---|---|
| conv_2spk | 83,1 → **99,1 %** | 0,0 → 0,0 % | 15 → 21 / 21 | 314 (492 / 508) → 440 (501 / 633) | 125 (157 / 188) → 125 (157 / 172) |
| conv_4spk | 71,0 → **97,8 %** | 0,5 → 0,3 % | 26 → 42 / 35 | 396 (477 / 508) → 457 (575 / 663) | 125 (188 / 250) → 125 (172 / 266) |
| conv_2spk_noisy | 83,0 → **99,1 %** | 0,0 → 0,0 % | 15 → 22 / 21 | 348 (492 / 507) → 439 (523 / 586) | 125 (157 / 188) → 125 (157 / 187) |

- Atraso = `t_ready − t_end` da **última** linha do final (fim do áudio → legenda pronta): ~300 ms de espera do silêncio de fim de frase em utterances ≤ 6 s (450 ms − 150 ms de cauda; ~100 ms depois de 6 s) + ASR + locutores + MT. Por final (mediana): ASR 128–147 ms, locutores 42–59 ms (`segments()`; o `identify` antigo gastava ~32), MT 24–40 ms.
- Os finais divididos por locutor (6 de 15, 11 de 26, 7 de 15 — fixtures densos, pausa entre turnos < 0,45 s) pagam 1 ASR + 1 MT a mais por trecho: mediana 440 / 539 / 439 ms contra 413 / 346 / 453 ms dos de 1 locutor. A 1ª versão (ASR do áudio todo sempre antes, descartado nas divisões) dava 508 / 468 / 508 ms.
- 4spk: 42 linhas contra 35 turnos porque o Segmenter corta os 3 monólogos de 17–20 s (utterance ≤ 15 s) e duas falas com pausa interna de 0,3 s depois de 6 s (fim de frase de 250 ms).
- Loopback real (Realtek, 48 kHz estéreo → 16 kHz mono; 15 s do conv_2spk tocados na saída padrão, sem UI): 99,5 % de locutor, WER 0,0 %, atraso do final 433 ms, do parcial 137 ms.
- App completo (`run.bat` → pythonw, `docs/app-live.png`): "Pronto — escutando …" ~4 s depois de abrir (modelos em cache), atraso na barra ~0,4 s; fechar a janela encerra todos os processos; `logs/app.log` sem erro.
- Limites da medição: TTS neural limpo, sem sobreposição de falas; voz real de call (Discord/Opus, microfone ruim) separa menos (calibrar `threshold`). PC com a GPU livre; com outros programas na GPU/CPU os atrasos sobem.
