"""Benchmark do ASR (EN + PT): WER, acerto do idioma (LID) e latência com modelo aquecido + checagem do filtro de alucinações.

Da raiz do projeto:  python tests/bench_asr.py [--only gpu|cpu] [--models a,b] [--reps 11] [--fix] [--no-wer] [--no-lat]
                                                [--threads 8] [--beams 5] [-v]
  padrão: o modelo padrão da GPU (large-v3-turbo float16, se houver CUDA) e o da CPU (parakeet-v3);
  --models 'modelo[:compute]' (':cpu' = CPU int8; parakeet-v3 é sempre CPU), ex.: --models large-v3-turbo,distil-large-v3.5,small.en:cpu;
  --fix = idioma do fixture passado ao ASR (oráculo, sem detecção); -v = lista os turnos com erro.
WER: cada turno recortado pelo gabarito (± 0,25 s), final=True, idioma detectado como no app (language=None; prior = último
idioma do mesmo locutor, senão do fixture, senão "en"; < 1,5 s ou indeciso = prior). Normalização EN: minúsculas, sem
pontuação, números unificados; PT: sem acento/pontuação e formas coloquiais iguais ("tá" = "está", "pra" = "para").
Conjuntos (188 turnos): EN conv_2spk, conv_4spk, conv_2spk_noisy (SNR 12 dB), 4spk_dificil (reverb + burburinho 8 dB);
PT conv_pt_2spk, conv_pt_3spk, conv_pt_2spk_noisy, pt_dificil (idem sobre o conv_pt_3spk). Modelo só-inglês pula os PT.
Sai com código 1 se o filtro de alucinações falhar.
"""
import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
import wave
from pathlib import Path

import numpy as np

from app.asr import PARAKEET, Transcriber, ensure_model
from app.events import SR
import ctranslate2  # noqa: E402 (depois de app.asr, que prepara as DLLs da CUDA)

DATA = Path(__file__).parent / "data"
PAD = 0.25                  # folga em volta de cada turno (s)
LENS = (1.5, 4, 8, 14)      # durações dos clipes de latência (s)
# (modelo, dispositivo, compute_type): os padrões do app.asr
GPU_CFGS = [("large-v3-turbo", "cuda", "float16")]
CPU_CFGS = [(PARAKEET, "cpu", "int8")]
FIXTURES = {"conv_2spk": "en", "conv_4spk": "en", "conv_2spk_noisy": "en", "conv_pt_2spk": "pt", "conv_pt_3spk": "pt",
            "conv_pt_2spk_noisy": "pt"}

# ---------------------------------------------------------------- WER
_NUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                   "sixteen seventeen eighteen nineteen".split())}
_NUM |= {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_ORD = {w: i + 1 for i, w in enumerate("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
                                       "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth".split())}
_ORD |= {"twentieth": 20, "thirtieth": 30}


def _numbers(ws):
    """'two hundred and fifty' -> '250'; 'two point four' -> '2.4'; 'twenty sixth' -> '26' (os dois lados passam por aqui)."""
    out, i = [], 0
    while i < len(ws):
        if ws[i] not in _NUM and ws[i] not in _ORD:
            out.append(ws[i]); i += 1
            continue
        tot = cur = 0
        ordinal = False
        while i < len(ws):
            w = ws[i]
            if w in _NUM:
                cur += _NUM[w]
            elif w in _ORD:
                cur += _ORD[w]; ordinal = True; i += 1
                break
            elif w == "hundred":
                cur = max(cur, 1) * 100
            elif w == "thousand":
                tot += max(cur, 1) * 1000; cur = 0
            elif w == "and" and (tot or cur >= 100) and i + 1 < len(ws) and (ws[i + 1] in _NUM or ws[i + 1] in _ORD):
                pass
            else:
                break
            i += 1
        s = str(tot + cur)
        if not ordinal and i + 1 < len(ws) and ws[i] == "point" and ws[i + 1] in _NUM and _NUM[ws[i + 1]] < 10:
            s += "." + str(_NUM[ws[i + 1]]); i += 2
        out.append(s)
    return out


def norm(s):
    """Minúsculas, sem acento/pontuação; $/% e números por extenso unificados."""
    s = unicodedata.normalize("NFKD", s.replace("’", "'")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)        # 2nd -> 2
    s = re.sub(r"(\d)([a-z]+)", r"\1 \2", s)            # 6pm -> 6 pm
    s = re.sub(r"\$\s*(\d[\d,.]*)", r"\1 dollars", s)
    s = re.sub(r"(\d)\s*%", r"\1 percent", s).replace("%", " percent")
    s = re.sub(r"(?<=\d),(?=\d)", "", s)                # 12,000 -> 12000
    s = re.sub(r"(?<=[a-z])\.(?=[a-z]\b)", "", s)       # p.m. -> pm
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)           # ponto só entre dígitos (2.4)
    s = re.sub(r"[^a-z0-9.' ]", " ", s.replace("-", " "))
    for a, b in (("all right", "alright"), ("back end", "backend"), ("cool down", "cooldown"), ("match making", "matchmaking")):
        s = re.sub(rf"\b{a}\b", b, s)                   # grafias equivalentes
    ws = [{"ok": "okay"}.get(w, w) for w in (w.strip("'") for w in s.split()) if w]
    return _numbers(ws)


def edits(r, h):
    """Distância de edição (Levenshtein) entre listas de palavras."""
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[-1]


# formas coloquiais do PT-BR que o ASR escreve de um jeito ou de outro (o Parakeet "formaliza": tá -> está)
PT_EQ = {"ta": "esta", "to": "estou", "pra": "para", "pro": "para o", "pros": "para os", "pras": "para as", "ne": ""}


def norm_pt(s):
    """Minúsculas, sem acento/pontuação; coloquial = formal (PT_EQ)."""
    s = unicodedata.normalize("NFKD", s.replace("’", "'")).encode("ascii", "ignore").decode().lower()
    return [x for w in re.sub(r"[^a-z0-9 ]", " ", s.replace("-", " ")).split() for x in PT_EQ.get(w, w).split()]


def norm_lang(s, lang):
    return norm(s) if lang == "en" else norm_pt(s)


def load(name):
    with wave.open(str(DATA / f"{name}.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2), name
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    turns = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))["turns"]
    return x, turns


def hard(x, turns, snr_db=8, seed=11):
    """Condição difícil: reverb (RT60 ~0,5 s) + 'burburinho' (3 cópias da própria conversa defasadas) com a SNR dada."""
    rng = np.random.default_rng(seed)
    ir = rng.standard_normal(SR // 2) * np.exp(-np.arange(SR // 2) / SR * 13.8)    # decaimento de 60 dB em 0,5 s
    ir[0] = 3.0                                                                      # som direto
    n = 1 << (len(x) + len(ir)).bit_length()
    wet = np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(ir, n), n)[:len(x)]           # convolução por FFT
    babble = sum(np.roll(wet, int(k * SR)) for k in (31.7, 58.3, 83.1))
    speech = np.concatenate([wet[int(u["start"] * SR):int(u["end"] * SR)] for u in turns])
    babble *= np.sqrt((speech ** 2).mean() / 10 ** (snr_db / 10) / (babble ** 2).mean())
    z = wet + babble
    return (z * min(1.0, 0.95 / np.abs(z).max())).astype(np.float32)


# ---------------------------------------------------------------- medição
def timed(fn, reps):
    """(mediana, mínimo) em ms de `reps` rodadas, depois de uma rodada de aquecimento."""
    fn()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter(); fn(); ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts), min(ts)


def table(head, rows):
    w = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    for r in [head, ["-" * n for n in w]] + rows:
        print("  ".join(str(c).ljust(n) if i < 2 else str(c).rjust(n) for i, (c, n) in enumerate(zip(r, w))))


def wer(tr, x, turns, lang, fix=False, verbose=False):
    """(erros, palavras de referência, acertos de idioma) transcrevendo cada turno recortado pelo gabarito ± PAD."""
    err = ref = ok = 0
    last: dict[str, str] = {}  # locutor -> último idioma detectado (como o pipeline)
    prior = "en"
    for t in turns:
        clip = x[max(0, int((t["start"] - PAD) * SR)):int((t["end"] + PAD) * SR)]
        text, got = tr.transcribe(clip, True, lang if fix or not tr.multi else None, last.get(t["speaker"], prior))
        last[t["speaker"]] = prior = got
        r, h = norm_lang(t["text"], lang), norm_lang(text, lang)
        e = edits(r, h)
        err, ref, ok = err + e, ref + len(r), ok + (got == lang)
        if verbose and (e or got != lang):
            print(f"    [{e}{'' if got == lang else ' idioma ' + got}] ref: {' '.join(r)}\n        hyp: {' '.join(h)}")
    return err, ref, ok


def bench(cfg, fx, clips, reps, threads, verbose, lat=True, wer_beams=(5,), fix=False):
    name, dev, comp = cfg
    ensure_model("models", name)
    t0 = time.perf_counter()
    tr = Transcriber(model=name, device=dev, compute_type=comp, cpu_threads=threads)
    load_s = time.perf_counter() - t0
    tr.warmup()
    t0 = time.perf_counter(); tr.transcribe(clips["en"][: 4 * SR], True); first = (time.perf_counter() - t0) * 1000  # 1a chamada pós-warmup
    res = {"cfg": (tr.model_name, tr.device, tr.compute_type), "load": load_s, "first": first, "wer": {}, "lat": {}, "tr": tr}
    print(f"  {tr.model_name} {tr.device}/{tr.compute_type}: carregado em {load_s:.1f} s, 1a chamada {first:.0f} ms", flush=True)
    keys = [k for k in fx if tr.multi or fx[k][2] == "en"]
    for beam in wer_beams if tr._pk is None else wer_beams[-1:]:   # WER: beam do final (o Parakeet não tem beam)
        tr.beam_final = beam
        res["wer"][beam] = {k: wer(tr, *fx[k], fix, verbose and beam == wer_beams[-1]) for k in keys}
    tr.beam_final = 5
    for sec in LENS if lat else ():             # latência: parcial (beam 1) e final (beam 5), com detecção de idioma
        clip = clips["en"][: int(sec * SR)]
        res["lat"][("p", sec)] = timed(lambda: tr.transcribe(clip, False), reps)
        res["lat"][("f", sec)] = timed(lambda: tr.transcribe(clip, True), reps)
    if lat and tr.multi:
        clip = clips["pt"][: 4 * SR]
        res["lat"][("p", "4 PT")] = timed(lambda: tr.transcribe(clip, False), reps)
        res["lat"][("f", "4 PT")] = timed(lambda: tr.transcribe(clip, True), reps)
    return res


def hallucination_check(tr, fx):
    """Silêncio, ruído branco e áudio curtíssimo devem devolver '' (idioma detectado e fixo em PT/EN); e nenhuma fala
    real (conv_4spk EN e conv_pt_3spk PT) pode ser descartada."""
    rng = np.random.default_rng(3)
    t = np.arange(3 * SR) / SR
    noise = lambda s, n=3 * SR: (rng.standard_normal(n) * s).astype(np.float32)
    cases = {"silencio 3 s": np.zeros(3 * SR, np.float32), "ruido branco 0,005": noise(0.005), "ruido branco 0,05": noise(0.05),
             "ruido branco 0,2": noise(0.2), "tom 440 Hz": (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
             "ruido 0,2 s": noise(0.05, SR // 5), "ruido 0,5 s": noise(0.05, SR // 2), "vazio": np.zeros(0, np.float32)}
    ok = True
    for name, a in cases.items():
        for final in (False, True):
            for lang in (None, "en", "pt"):
                out = tr.transcribe(a, final, lang)[0]
                ok &= out == ""
                if out:
                    print(f"  FALHA: {name} (final={final}, idioma={lang}) -> {out!r}")
    kept = total = 0
    for k in ("conv_4spk", "conv_pt_3spk") if tr.multi else ("conv_4spk",):
        x, turns, lang = fx[k]
        kept += sum(bool(tr.transcribe(x[int((t["start"] - PAD) * SR):int((t["end"] + PAD) * SR)], True, lang)[0]) for t in turns)
        total += len(turns)
    ok &= kept == total
    print(f"fala real preservada: {kept}/{total} turnos")
    print("filtro de alucinacoes:", "OK" if ok else "FALHOU")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("gpu", "cpu"))
    ap.add_argument("--reps", type=int, default=11)
    ap.add_argument("--models")
    ap.add_argument("--beams", default="5", help="beams do final na parte de WER (Whisper)")
    ap.add_argument("--threads", type=int, default=8, help="cpu_threads dos modelos de CPU")
    ap.add_argument("--fix", action="store_true", help="passa o idioma do fixture (sem detecção)")
    ap.add_argument("--no-lat", action="store_true", help="só WER")
    ap.add_argument("--no-wer", action="store_true", help="só latência")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")
    fx = {k: (*load(k), lang) for k, lang in FIXTURES.items()}
    fx["4spk_dificil"] = (hard(*fx["conv_4spk"][:2]), fx["conv_4spk"][1], "en")
    fx["pt_dificil"] = (hard(*fx["conv_pt_3spk"][:2]), fx["conv_pt_3spk"][1], "pt")
    fx = dict(sorted(fx.items(), key=lambda kv: kv[1][2]))  # EN primeiro
    clips = {lang: np.concatenate([x[int(t["start"] * SR):int(t["end"] * SR)] for t in turns])  # só fala, sem pausas
             for lang, (x, turns, _) in (("en", fx["conv_2spk"]), ("pt", fx["conv_pt_2spk"]))}
    if a.models:
        cfgs = []
        for m in a.models.split(","):
            n, _, c = m.partition(":")
            cfgs.append((n, "cpu", "int8") if c == "cpu" or n == PARAKEET else (n, "cuda", c or "float16"))
    else:
        gpu = ctranslate2.get_cuda_device_count() > 0
        cfgs = (GPU_CFGS if gpu and a.only != "cpu" else []) + (CPU_CFGS if a.only != "gpu" else [])
    reps = max(a.reps, 5)
    out = []
    for c in cfgs:
        beams = () if a.no_wer else tuple(map(int, a.beams.split(",")))
        out.append(bench(c, fx, clips, reps if c[1] == "cuda" else max(5, reps // 2), a.threads, a.v, not a.no_lat, beams, a.fix))

    mc = lambda r: f'{r["cfg"][1]}/{r["cfg"][2]}'
    if not a.no_wer:
        print("\nWER (%) do final | EN: 2spk, 4spk, 2spk ruidoso (12 dB), 4spk dificil (reverb + burburinho 8 dB) | PT: idem "
              "(3spk no lugar do 4spk) | totais ponderados por palavras | LID = turnos com o idioma certo"
              + (" | --fix: idioma do fixture" if a.fix else ""))
        sets = list(fx)
        tot = lambda vs: f"{100 * sum(v[0] for v in vs) / sum(v[1] for v in vs):.1f}" if vs else "-"
        rows = []
        for r in out:
            for b, w in r["wer"].items():
                en, pt = [w[k] for k in w if fx[k][2] == "en"], [w[k] for k in w if fx[k][2] == "pt"]
                rows.append([r["cfg"][0], mc(r), b] + [f"{100 * w[k][0] / w[k][1]:.1f}" if k in w else "-" for k in sets]
                            + [tot(en), tot(pt), f"{sum(v[2] for v in en + pt)}/{sum(len(fx[k][1]) for k in w)}"]
                            + ([f'{r["load"]:.1f}', f'{r["first"]:.0f}'] if b == min(r["wer"]) else ["", ""]))
        table(["modelo", "compute", "beam"] + [k.replace("conv_", "") for k in sets] + ["EN tot", "PT tot", "LID", "carga s", "1a ms"], rows)
    for kind, title in (("p", "parcial (beam 1)"), ("f", "final (beam 5)")) if not a.no_lat else ():
        cols = [*LENS, "4 PT"]
        print(f"\nLatencia {title}, ms: mediana (minimo), clipe de fala de N s, idioma detectado (language=None)")
        table(["modelo", "compute"] + [f"{s} s" for s in cols],
              [[r["cfg"][0], mc(r)] + [f'{r["lat"][(kind, s)][0]:.0f} ({r["lat"][(kind, s)][1]:.0f})' if (kind, s) in r["lat"] else "-"
                                      for s in cols] for r in out])
    print("\nFiltro de alucinacoes:")
    ok = True
    for r in out:
        print(f"  {r['cfg'][0]} {mc(r)}")
        ok &= hallucination_check(r["tr"], fx)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
