"""ASR multilíngue EN+PT: fala -> (texto, idioma "en"|"pt").

GPU: Whisper large-v3-turbo float16 (faster-whisper/CTranslate2) com detecção de idioma "fused" restrita a {en, pt}:
1 encoder -> detect_language no mesmo encoder_output -> generate. Custa +3 ms; o language=None do faster-whisper custaria
+73 ms e "Oi, gente." viraria espanhol. O distil-large-v3.5 de antes só entende inglês (PT dá WER ~100 %).
CPU/Mac: Parakeet-TDT-0.6B-v3 int8 (NVIDIA, CC-BY-4.0) via sherpa-onnx; transcreve 25 idiomas sozinho e o idioma sai do
texto (palavras funcionais + acentos; empate mantém o anterior).

Pesquisa (relatório ASR de 04/10/2026; 188 turnos EN+PT de TTS; latência = mediana intercalada, GPU compartilhada):
  GPU float16             WER EN total | difícil   WER PT total | difícil   LID       parcial 4 s   final 4 s   final 14 s
  large-v3-turbo fused        1,3 | 3,8              2,1 | 5,6         187/188    ~117 ms      ~130 ms     ~230 ms  <- padrão GPU
  distil-large-v3.5 (antigo)  1,0 | 3,3              ~100 (só EN)        -        ~105 ms      ~113 ms     ~176 ms
  parakeet-v3 fp32 (ORT GPU)  2,1 | 5,5              3,1 | 8,9         187/188     ~25 ms       ~26 ms     ~102 ms  (fase 2)
  CPU int8 (i7-14700K, carga variável): parakeet-v3 sherpa 5,3 | 13,0 EN, 5,6 | 19,4 PT, parcial 4 s 0,15-0,70 s <- padrão CPU;
  small.en (antigo) 2,7 | 9,0 só EN, 1,2-1,8 s.
"difícil" = reverb + burburinho a 8 dB.
Este módulo (tests/bench_asr.py, 04/10/2026, detecção como no app: < 1,5 s ou indeciso = prior do locutor):
  large-v3-turbo GPU: WER EN 1,3 % (difícil 3,8) / PT 2,2 % (difícil 6,1), LID 188/188; parcial 4 s ~129 ms, final 4 s ~138 ms,
  final 14 s ~249 ms (GPU compartilhada com outros processos). parakeet-v3 CPU: EN 5,3 % / PT 5,6 %, LID 188/188,
  parcial 4 s 0,18-0,58 s (conforme a carga da CPU).
Descartados antes: initial_prompt com jargão (WER 1,0 -> 1,1-1,3), int8_float16 (mesmo WER, 5-12 % mais lento) e encoder com
janela < 30 s (WER do small.en 0,5 -> 2-4 %).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import tempfile
import unicodedata
from pathlib import Path

import numpy as np

from app.cuda_dlls import setup_cuda_dlls
from app.events import SR
from app.paths import download

setup_cuda_dlls()  # antes de importar ctranslate2/faster_whisper
import ctranslate2  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
from faster_whisper.audio import pad_or_trim  # noqa: E402
from faster_whisper.tokenizer import Tokenizer  # noqa: E402
from faster_whisper.transcribe import get_compression_ratio, get_suppressed_tokens  # noqa: E402
from faster_whisper.utils import download_model  # noqa: E402

GPU_MODEL, GPU_COMPUTE = "large-v3-turbo", "float16"
PARAKEET = "parakeet-v3"  # padrão da CPU; sempre roda na CPU (int8 no CUDA do onnxruntime cai para a CPU: 30x mais lento)
PARAKEET_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
                "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2")  # 487 MB
PARAKEET_SHA256 = "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf"
_PK_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
MIN_DUR_S = 0.3   # abaixo disso o Whisper só alucina
MIN_RMS = 0.0015  # ~ -56 dBFS: silêncio digital / dither (fala real a -54 dBFS ainda é transcrita)
LP_MIN = -1.0     # avg_logprob abaixo disso = decodificação falha (mesmo critério do Whisper)
LP_WEAK = -0.3    # frases comuns ("Thank you.") só somem se o modelo estiver inseguro (fala real: -0.03..-0.2; ruído: -0.4..-0.9)
CR_MAX = 2.4      # compression_ratio acima disso = loop de repetição
LID_MIN_S = 1.5   # fala mais curta não detecta o idioma: herda o `prior` (o único erro medido é fala curta em ruído)
LID_MARGIN = 0.3  # |p(en) - p(pt)| / (p(en) + p(pt)) abaixo disso = indeciso: herda o `prior`
# frases que o Whisper inventa em silêncio/ruído (comparadas sem acento, pontuação e maiúsculas): sempre descartadas
HALLUCINATIONS = {"you", "thanks for watching", "thank you for watching", "thank you so much for watching",
                  "please subscribe", "subtitles by the amara org community",
                  "legendas pela comunidade amara org", "inscreva se", "obrigado por assistir",
                  "uh", "um", "hmm", "mm", "ah", "eh"}  # hesitação sozinha: o Parakeet ouve "Uh." em ruído; legenda não perde nada
# ...e falas reais comuns que também são alucinação típica: descartadas só com baixa confiança
# ponytail: listas fixas e curtas; os filtros PT não têm números de uso real, ampliar se aparecerem outras alucinações
WEAK_PHRASES = {"thank you", "thanks", "bye", "bye bye", "goodbye", "obrigado", "obrigada", "tchau", "e ai"}  # "E aí" = o "you" do PT
_NONSPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫]+")  # [Music], (laughs), *sighs*, notas musicais

# idioma pelo texto (Parakeet): palavras funcionais (sem acento) + letras acentuadas do PT
_PT = set("de que nao e o os as um uma eu voce ta pra para com do da dos das no na nos nas em se mas isso esse essa ele ela "
          "gente vamos tem sim tambem ja aqui ai muito bom boa obrigado valeu tudo certo agora depois entao mesmo "
          "por pelo pela foi vai vou meu minha seu sua nossa ao mais ou quando como onde porque tipo tchau oi beleza entendi faz "
          "sentido claro perfeito otimo fechado calma verdade serio perai show estou esta".split())
_EN = set("the and you is it to of i that in we this for on are be have what with so yeah okay but not can my your "
          "was it's i'm don't just like there they he she will would do does did got get go going know think right "
          "yes no hey hi thanks thank all good great sure well oh wait".split())


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def text_lid(text: str, prior: str = "en") -> str:
    """'en'/'pt' pelo texto; empate (ou vazio) mantém `prior`."""
    raw = text.lower()
    pt = sum(w in _PT for w in re.findall(r"[a-z]+", _ascii(raw).replace("-", " "))) + len(re.findall(r"[ãõçâêôáéíóú]", raw))
    en = sum(w in _EN for w in re.findall(r"[a-z']+", raw))
    return prior if pt == en else ("pt" if pt > en else "en")


def _pick(model: str, device: str, compute_type: str) -> tuple[str, str, str]:
    """Resolve os 'auto' pelo hardware."""
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    gpu = device == "cuda"
    if model == "auto":
        model = GPU_MODEL if gpu else PARAKEET
    if model == PARAKEET:
        return model, "cpu", "int8"
    if compute_type == "auto":
        compute_type = GPU_COMPUTE if gpu else "int8"
    if compute_type not in ctranslate2.get_supported_compute_types(device):
        compute_type = "default"  # GPU antiga etc.: deixa o CTranslate2 escolher
    return model, device, compute_type


def ensure_model(models_dir: str = "models", model: str = "auto", on_progress=None) -> str:
    """Baixa o modelo (padrão do hardware) para <models_dir>/whisper ou <models_dir>/parakeet-v3-int8; idempotente.
    Devolve o diretório local. on_progress(frac | None) só quando há download."""
    name = _pick(model, "auto", "auto")[0]
    if name == PARAKEET:
        d = Path(models_dir) / "parakeet-v3-int8"
        if all((d / f).exists() for f in _PK_FILES):
            return str(d)
        d.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=d.parent) as tmp:  # queda no meio não deixa modelo pela metade
            tmp = Path(tmp)
            download(PARAKEET_URL, PARAKEET_SHA256, tmp / "m.tar.bz2", on_progress)
            if on_progress:
                on_progress(None)  # descompactando (~30 s)
            with tarfile.open(tmp / "m.tar.bz2") as tf:
                for m in tf.getmembers():
                    if m.isfile() and Path(m.name).name in _PK_FILES:  # o pacote também traz test_wavs/
                        m.name = Path(m.name).name
                        tf.extract(m, tmp / "out", filter="data")
            (tmp / "out" / "info.json").write_text(json.dumps({"name": "parakeet-tdt-0.6b-v3 int8 (sherpa-onnx)", "url": PARAKEET_URL,
                                                               "license": "CC-BY-4.0 (NVIDIA)"}), encoding="utf-8")
            shutil.rmtree(d, ignore_errors=True)
            shutil.move(str(tmp / "out"), str(d))
        return str(d)
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
    """`transcribe(audio, final, language, prior)` -> (texto, idioma). Parcial = beam 1; final = beam `beam_final`."""

    def __init__(self, *, model: str = "auto", device: str = "auto", compute_type: str = "auto",
                 models_dir: str = "models", cpu_threads: int = 0, on_progress=None):
        self.model_name, self.device, self.compute_type = _pick(model, device, compute_type)
        self.beam_final = 5  # beam do final=True no Whisper (mesmo WER e latência que 3; 1 piora no difícil)
        path = ensure_model(models_dir, self.model_name, on_progress)
        # CPU: 8 threads (4 é bem pior; 8-16 dão o mesmo no clipe de 4 s); deixa o resto para o pipeline
        threads = cpu_threads or min(8, os.cpu_count() or 4)
        self._pk = self._m = None
        if self.model_name == PARAKEET:
            import sherpa_onnx
            f = {k: os.path.join(path, k + ".int8.onnx") for k in ("encoder", "decoder", "joiner")}
            self._pk = sherpa_onnx.OfflineRecognizer.from_transducer(
                **f, tokens=os.path.join(path, "tokens.txt"), model_type="nemo_transducer", num_threads=threads)
            self.multi = True
            return
        self._m = WhisperModel(path, device=self.device, compute_type=self.compute_type, cpu_threads=threads)
        self.multi = self._m.model.is_multilingual  # *.en / distil: só inglês
        self._tok: dict[str, tuple] = {}  # idioma -> (Tokenizer, prompt, tokens suprimidos)

    def _whisper(self, audio: np.ndarray, beam: int, language: str | None, prior: str) -> tuple[str, str, float, float]:
        """Caminho fused: 1 encoder, detect_language (só en/pt) na mesma saída e generate. Devolve (texto, idioma,
        avg_logprob, compression_ratio), calculados como o faster-whisper (os filtros continuam valendo)."""
        enc = self._m.encode(pad_or_trim(self._m.feature_extractor(audio)[:, :3000]))  # <= 30 s (o Segmenter corta em 15 s)
        lang = language or prior
        if not self.multi:
            lang = "en"
        elif language is None and len(audio) >= LID_MIN_S * SR:
            p = dict(self._m.model.detect_language(enc)[0])
            en, pt = p.get("<|en|>", 0.0), p.get("<|pt|>", 0.0)
            if abs(en - pt) >= LID_MARGIN * (en + pt):
                lang = "pt" if pt > en else "en"
        if lang not in self._tok:
            tok = Tokenizer(self._m.hf_tokenizer, self.multi, task="transcribe", language=lang)
            self._tok[lang] = tok, [*tok.sot_sequence, tok.no_timestamps], list(get_suppressed_tokens(tok, [-1]))
        tok, prompt, suppress = self._tok[lang]
        r = self._m.model.generate(
            enc, [prompt], beam_size=beam, length_penalty=1, return_scores=True, suppress_blank=True,
            suppress_tokens=suppress, max_length=len(prompt) + min(200, int(len(audio) / SR * 12) + 16))[0]  # trava loops
        ids = r.sequences_ids[0]
        text = tok.decode(ids).strip()
        return text, lang, r.scores[0] * len(ids) / (len(ids) + 1), get_compression_ratio(text) if text else 0.0

    def _parakeet(self, audio: np.ndarray, language: str | None, prior: str) -> tuple[str, str, float, float]:
        s = self._pk.create_stream()
        s.accept_waveform(SR, audio)
        self._pk.decode_stream(s)
        text = s.result.text.strip()
        lang = language or (text_lid(text, prior) if len(audio) >= LID_MIN_S * SR else prior)
        # ponytail: sem logprob no sherpa (ys_log_probs vem vazio): WEAK_PHRASES nunca somem aqui, só HALLUCINATIONS
        return text, lang, 0.0, get_compression_ratio(text) if text else 0.0

    def warmup(self) -> None:
        """Parcial e final dummy em ruído, pela detecção de idioma: inicializa cuDNN/cuBLAS e o alocador."""
        x = (np.random.default_rng(0).standard_normal(4 * SR) * 0.05).astype(np.float32)
        for final in (False, True):
            self.transcribe(x, final)

    def transcribe(self, audio: np.ndarray, final: bool = False, language: str | None = None,
                   prior: str = "en") -> tuple[str, str]:
        """(texto, idioma). language fixo = sem detecção; None = detecta entre en/pt (< LID_MIN_S ou indeciso -> prior).
        Texto "" = vazio ou alucinação."""
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if len(audio) < MIN_DUR_S * SR or float(np.sqrt(np.mean(audio * audio))) < MIN_RMS:
            return "", language or prior
        if self._pk is not None:
            text, lang, lp, cr = self._parakeet(audio, language, prior)
        else:
            text, lang, lp, cr = self._whisper(audio, self.beam_final if final else 1, language, prior)
        text = " ".join(_NONSPEECH.sub(" ", text).split())
        key = " ".join(re.sub(r"[^a-z0-9' ]", " ", _ascii(text)).split())
        # sem conteúdo / decodificação falha / loop de repetição / alucinação conhecida (fixa ou com o modelo inseguro)
        if not key or lp < LP_MIN or cr > CR_MAX or key in HALLUCINATIONS or (key in WEAK_PHRASES and lp < LP_WEAK):
            return "", lang
        return text, lang


if __name__ == "__main__":  # checagem rápida: python -m app.asr [parakeet-v3]
    import sys
    assert text_lid("Oi gente, tá me ouvindo?") == "pt" and text_lid("Hey, can you hear me?", "pt") == "en"
    assert text_lid("", "pt") == "pt" and text_lid("Okay.", "pt") == "en"
    t = Transcriber(model=sys.argv[1] if len(sys.argv) > 1 else "auto")
    t.warmup()
    rng = np.random.default_rng(0)
    for audio in (np.zeros(3 * SR, np.float32), rng.standard_normal(3 * SR).astype(np.float32) * 0.05,
                  rng.standard_normal(SR // 5).astype(np.float32) * 0.05, np.zeros(0, np.float32)):
        for lang in (None, "pt"):
            assert t.transcribe(audio, language=lang)[0] == t.transcribe(audio, True, lang)[0] == "", "filtro de alucinações falhou"
    assert t.transcribe(np.zeros(SR, np.float32), prior="pt") == ("", "pt")
    print(f"ok: {t.model_name} {t.device}/{t.compute_type}")
