"""ASR: fala em inglês -> texto (faster-whisper = pesos Whisper em CTranslate2).

Padrão `auto`, por benchmark (tests/bench_asr.py; RTX 4080 SUPER + i7-14700K; latência = mediana de 21 em 2 rodadas, GPU livre):
  GPU float16         WER total | "difícil"   parcial 4 s   final 4 s (beam 5)   (final 14 s)
  distil-large-v3.5    1,0%  |  3,3%          ~100 ms        ~110 ms             ~185 ms   <- padrão GPU
  large-v3-turbo       1,2%  |  3,5%          ~120 ms        ~135 ms             ~245 ms
  distil-large-v3      1,8%  |  5,0%          ~110 ms        ~115 ms             ~195 ms
  small.en             2,5%  |  8,3%          ~105 ms        ~140 ms             ~365 ms
  distil-small.en      4,4%  | 13,0%           ~55 ms         ~65 ms             ~160 ms
  CPU int8, 8 threads  small.en 2,7% | 9,0% (parcial 4 s ~0,93 s) <- padrão CPU; base.en 6,4% | 20,3% (0,33 s); tiny.en 8,5% | 27,1% (0,16 s)
"difícil" = conv_4spk com reverb + burburinho (8 dB). Com a GPU compartilhada a latência dobra (parcial 4 s ~190 ms).
Regra: menor latência com WER <= melhor + 1,5. distil-large-v3.5 empata em latência com o distil-large-v3 (mesma arquitetura) e erra menos.
int8_float16 não ganha (mesmo WER, 5-12% mais lento). Descartados: initial_prompt com jargão (WER 1,0 -> 1,1-1,3) e encoder com
janela < 30 s (6x mais rápido na CPU, mas WER 0,5 -> 2-4% no small.en e 22-80% no distil-large-v3).
"""
from __future__ import annotations

import os
import re

import numpy as np

from app.cuda_dlls import setup_cuda_dlls
from app.events import SR

setup_cuda_dlls()  # antes de importar ctranslate2/faster_whisper
import ctranslate2  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
from faster_whisper.utils import download_model  # noqa: E402

GPU_MODEL, GPU_COMPUTE = "distil-large-v3.5", "float16"
CPU_MODEL, CPU_COMPUTE = "small.en", "int8"  # base.en é 3x mais rápido (Transcriber(device="cpu", model="base.en")), mas pior no ruído
MIN_DUR_S = 0.3   # abaixo disso o Whisper só alucina
MIN_RMS = 0.0015  # ~ -56 dBFS: silêncio digital / dither (fala real a -54 dBFS ainda é transcrita)
LP_MIN = -1.0     # avg_logprob abaixo disso = decodificação falha (mesmo critério do Whisper)
LP_WEAK = -0.3    # frases comuns ("Thank you.") só somem se o modelo estiver inseguro (fala real: -0.03..-0.2; ruído: -0.4..-0.9)
CR_MAX = 2.4      # compression_ratio acima disso = loop de repetição
# frases que o Whisper inventa em silêncio/ruído (comparadas sem pontuação, minúsculas): sempre descartadas
HALLUCINATIONS = {"you", "thanks for watching", "thank you for watching", "thank you so much for watching",
                  "please subscribe", "subtitles by the amara org community"}
# ...e falas reais comuns que também são alucinação típica: descartadas só com baixa confiança
# ponytail: listas fixas e curtas; "Thank you." confiante passa, ampliar se aparecerem outras alucinações em uso real
WEAK_PHRASES = {"thank you", "thanks", "bye", "bye bye", "goodbye"}
_NONSPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫]+")  # [Music], (laughs), *sighs*, notas musicais


def _pick(model: str, device: str, compute_type: str) -> tuple[str, str, str]:
    """Resolve os 'auto' pelo hardware."""
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    gpu = device == "cuda"
    if model == "auto":
        model = GPU_MODEL if gpu else CPU_MODEL
    if compute_type == "auto":
        compute_type = GPU_COMPUTE if gpu else CPU_COMPUTE
    if compute_type not in ctranslate2.get_supported_compute_types(device):
        compute_type = "default"  # GPU antiga etc.: deixa o CTranslate2 escolher
    return model, device, compute_type


def ensure_model(models_dir: str = "models", model: str = "auto", on_progress=None) -> str:
    """Baixa o modelo (padrão do hardware) para <models_dir>/whisper; idempotente. Devolve o diretório local."""
    name = _pick(model, "auto", "auto")[0]
    root = os.path.join(models_dir, "whisper")
    try:  # já no disco (e completo): sem rede
        path = download_model(name, local_files_only=True, cache_dir=root)
        if os.path.exists(os.path.join(path, "model.bin")):
            return path
    except Exception:
        pass
    if on_progress:
        on_progress(None)  # ponytail: download do HF sem % (indeterminado), tqdm por arquivo se incomodar
    return download_model(name, cache_dir=root)


class Transcriber:
    """Whisper EN -> texto. `transcribe(audio, final)`: parcial = beam 1; final = beam `beam_final`."""

    def __init__(self, *, model: str = "auto", device: str = "auto", compute_type: str = "auto",
                 language: str = "en", models_dir: str = "models", cpu_threads: int = 0, on_progress=None):
        self.model_name, self.device, self.compute_type = _pick(model, device, compute_type)
        self.language = language
        self.beam_final = 5          # beam do final=True (mesmo WER e latência que 3; 1 piora no difícil)
        self.prompt: str | None = None  # initial_prompt: testado com jargão (Discord, deploy, ping...), não melhorou o WER
        path = ensure_model(models_dir, self.model_name, on_progress)
        # CPU: 8 threads (4 é bem pior; 8-16 dão o mesmo ~0,9 s no clipe de 4 s do small.en); deixa o resto para o pipeline
        threads = cpu_threads or min(8, os.cpu_count() or 4)
        self._m = WhisperModel(path, device=self.device, compute_type=self.compute_type, cpu_threads=threads)

    def _run(self, audio: np.ndarray, beam: int) -> tuple[str, float, float]:
        """Decodifica e devolve (texto, avg_logprob, compression_ratio). Sem filtros."""
        segs, _ = self._m.transcribe(
            audio, language=self.language, beam_size=beam, best_of=1, temperature=0,
            condition_on_previous_text=False, without_timestamps=True, vad_filter=False, suppress_blank=True,
            no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
            max_new_tokens=min(200, int(len(audio) / SR * 12) + 16),  # trava loops: 12 tokens/s é o dobro da fala rápida
            initial_prompt=self.prompt)
        segs = list(segs)
        text = " ".join(s.text.strip() for s in segs)
        return text, min((s.avg_logprob for s in segs), default=0.0), max((s.compression_ratio for s in segs), default=0.0)

    def warmup(self) -> None:
        """Duas inferências dummy (beam 1 e final) em ruído: inicializa cuDNN/cuBLAS e o alocador."""
        x = (np.random.default_rng(0).standard_normal(4 * SR) * 0.05).astype(np.float32)
        for beam in (1, self.beam_final):
            self._run(x, beam)

    def transcribe(self, audio: np.ndarray, final: bool = False) -> str:
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if len(audio) < MIN_DUR_S * SR or float(np.sqrt(np.mean(audio * audio))) < MIN_RMS:
            return ""
        text, lp, cr = self._run(audio, self.beam_final if final else 1)
        text = " ".join(_NONSPEECH.sub(" ", text).split())
        key = " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())
        # sem conteúdo / decodificação falha / loop de repetição / alucinação conhecida (fixa ou com o modelo inseguro)
        if not key or lp < LP_MIN or cr > CR_MAX or key in HALLUCINATIONS or (key in WEAK_PHRASES and lp < LP_WEAK):
            return ""
        return text


if __name__ == "__main__":  # checagem rápida: python -m app.asr
    t = Transcriber()
    t.warmup()
    rng = np.random.default_rng(0)
    for audio in (np.zeros(3 * SR, np.float32), rng.standard_normal(3 * SR).astype(np.float32) * 0.05,
                  rng.standard_normal(SR // 5).astype(np.float32) * 0.05, np.zeros(0, np.float32)):
        assert t.transcribe(audio) == t.transcribe(audio, True) == "", "filtro de alucinações falhou"
    print(f"ok: {t.model_name} {t.device}/{t.compute_type}")
