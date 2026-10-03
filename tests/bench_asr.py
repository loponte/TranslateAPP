"""Benchmark do ASR: WER nos fixtures + latência com modelo aquecido + checagem do filtro de alucinações.

Da raiz do projeto:  python tests/bench_asr.py [--only gpu|cpu] [--quick] [--reps 11] [--models a,b] [--beams 1,3,5]
                                                [--no-wer] [--no-lat] [--threads 8] [--prompt "texto"] [-v]
  padrão: candidatos de GPU (se houver CUDA) e de CPU; --models 'modelo[:compute]' (':cpu' = CPU int8);
  --quick = 1 modelo, poucas rodadas; --prompt = initial_prompt em todos; -v = lista os turnos com erro.
WER: cada turno recortado pelo gabarito (± 0,25 s), final=True, normalizado (minúsculas, sem pontuação, números unificados).
Conjuntos: conv_2spk, conv_4spk, conv_2spk_noisy (SNR 12 dB) + extra "dificil" (conv_4spk com reverb e burburinho, SNR 8 dB).
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

from app.asr import Transcriber, ensure_model
from app.events import SR
import ctranslate2  # noqa: E402 (depois de app.asr, que prepara as DLLs da CUDA)

DATA = Path(__file__).parent / "data"
PAD = 0.25                  # folga em volta de cada turno (s)
LENS = (1.5, 4, 8, 14)      # durações dos clipes de latência (s)
# (modelo, dispositivo, compute_type)
GPU_CFGS = [(m, "cuda", c) for m in ("distil-large-v3.5", "distil-large-v3", "large-v3-turbo", "small.en", "distil-small.en")
            for c in ("float16", "int8_float16")]
CPU_CFGS = [(m, "cpu", "int8") for m in ("small.en", "distil-small.en", "base.en", "tiny.en")]

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


def wer(tr, x, turns, final=True, verbose=False):
    """(erros, palavras de referência) transcrevendo cada turno recortado pelo gabarito ± PAD."""
    err = ref = 0
    for t in turns:
        clip = x[max(0, int((t["start"] - PAD) * SR)):int((t["end"] + PAD) * SR)]
        r, h = norm(t["text"]), norm(tr.transcribe(clip, final))
        e = edits(r, h)
        err, ref = err + e, ref + len(r)
        if verbose and e:
            print(f"    [{e}] ref: {' '.join(r)}\n        hyp: {' '.join(h)}")
    return err, ref


# ---------------------------------------------------------------- latência
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


def bench(cfg, fx, speech, reps, threads, prompt, verbose, lat=True, wer_beams=(1, 3, 5)):
    name, dev, comp = cfg
    ensure_model("models", name)
    t0 = time.perf_counter()
    tr = Transcriber(model=name, device=dev, compute_type=comp, cpu_threads=threads)
    load_s = time.perf_counter() - t0
    tr.prompt = prompt
    tr.warmup()
    t0 = time.perf_counter(); tr.transcribe(speech[: 4 * SR], True); first = (time.perf_counter() - t0) * 1000  # 1a chamada pós-warmup
    res = {"cfg": cfg, "load": load_s, "first": first, "wer": {}, "lat": {}}
    print(f"  {name} {dev}/{comp}: carregado em {load_s:.1f} s, 1a chamada {first:.0f} ms", flush=True)
    for beam in wer_beams:                      # WER: beam do final
        tr.beam_final = beam
        errs = [wer(tr, *fx[k], True, verbose and beam == 5) for k in fx]
        res["wer"][beam] = [100 * e / n for e, n in errs] + [100 * sum(e for e, _ in errs) / sum(n for _, n in errs)]
    for sec in LENS if lat else ():             # latência: parcial (beam 1) e finais (beam 3 e 5)
        clip = speech[: int(sec * SR)]
        res["lat"][("p", sec)] = timed(lambda: tr.transcribe(clip, False), reps)
        for beam in (3, 5):
            tr.beam_final = beam
            res["lat"][(f"f{beam}", sec)] = timed(lambda: tr.transcribe(clip, True), reps)
    return res


def hallucination_check(tr, x, turns):
    """Silêncio, ruído branco e áudio curtíssimo devem devolver ''; e nenhuma fala real pode ser descartada."""
    rng = np.random.default_rng(3)
    t = np.arange(3 * SR) / SR
    noise = lambda s, n=3 * SR: (rng.standard_normal(n) * s).astype(np.float32)
    cases = {"silencio 3 s": np.zeros(3 * SR, np.float32), "ruido branco 0,005": noise(0.005), "ruido branco 0,05": noise(0.05),
             "ruido branco 0,2": noise(0.2), "tom 440 Hz": (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
             "ruido 0,2 s": noise(0.05, SR // 5), "ruido 0,5 s": noise(0.05, SR // 2), "vazio": np.zeros(0, np.float32)}
    ok = True
    for name, a in cases.items():
        for final in (False, True):
            out = tr.transcribe(a, final)
            ok &= out == ""
            if out:
                print(f"  FALHA: {name} (final={final}) -> {out!r}")
    kept = sum(bool(tr.transcribe(x[int((t["start"] - PAD) * SR):int((t["end"] + PAD) * SR)], True)) for t in turns)
    ok &= kept == len(turns)
    print(f"fala real preservada: {kept}/{len(turns)} turnos")
    print("filtro de alucinacoes:", "OK" if ok else "FALHOU")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("gpu", "cpu"))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--reps", type=int, default=11)
    ap.add_argument("--models")
    ap.add_argument("--beams", help="beams do final na parte de WER (padrão: 1,3,5 na GPU; 5 na CPU)")
    ap.add_argument("--threads", type=int, default=8, help="cpu_threads dos modelos de CPU")
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--no-lat", action="store_true", help="só WER")
    ap.add_argument("--no-wer", action="store_true", help="só latência")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    if not (DATA / "READY").exists():
        print("aviso: tests/data/READY ausente (fixtures ainda sendo geradas)")
    fx = {k: load(k) for k in ("conv_2spk", "conv_4spk", "conv_2spk_noisy")}
    fx["4spk_dificil"] = (hard(*fx["conv_4spk"]), fx["conv_4spk"][1])
    x, turns = fx["conv_2spk"]
    speech = np.concatenate([x[int(t["start"] * SR):int(t["end"] * SR)] for t in turns])  # só fala, sem pausas
    if a.models:
        cfgs = []
        for m in a.models.split(","):
            n, _, c = m.partition(":")
            cfgs.append((n, "cpu", "int8") if c == "cpu" else (n, "cuda", c or "float16"))
    else:
        gpu = ctranslate2.get_cuda_device_count() > 0
        cfgs = (GPU_CFGS if gpu and a.only != "cpu" else []) + (CPU_CFGS if a.only != "gpu" else [])
        cfgs = cfgs[:1] if a.quick else cfgs
    reps = 3 if a.quick else max(a.reps, 10)
    out = []
    for c in cfgs:
        beams = tuple(map(int, a.beams.split(","))) if a.beams else ((1, 3, 5) if c[1] == "cuda" else (5,))
        out.append(bench(c, fx, speech, reps, a.threads, a.prompt, a.v, not a.no_lat, () if a.no_wer else beams))

    mc = lambda r: f'{r["cfg"][1]}/{r["cfg"][2]}'
    if not a.no_wer:
        print("\nWER (%) do final por beam | 2spk, 4spk, 2spk ruidoso (12 dB), 4spk dificil (reverb + burburinho 8 dB), total (ponderado por palavras)"
              + (f" | initial_prompt={a.prompt!r}" if a.prompt else ""))
        table(["modelo", "compute", "beam", "2spk", "4spk", "r12dB", "dificil", "total", "carga s", "1a chamada ms"],
              [[r["cfg"][0], mc(r), b] + [f"{v:.1f}" for v in r["wer"][b]] + ([f'{r["load"]:.1f}', f'{r["first"]:.0f}'] if b == min(r["wer"]) else ["", ""])
               for r in out for b in r["wer"]])
    for kind, title in (("p", "parcial (beam 1)"), ("f3", "final beam 3"), ("f5", "final beam 5")) if not a.no_lat else ():
        print(f"\nLatencia {title}, ms: mediana de {reps} (minimo), clipe de fala de N s")
        table(["modelo", "compute"] + [f"{s} s" for s in LENS],
              [[r["cfg"][0], mc(r)] + [f'{r["lat"][(kind, s)][0]:.0f} ({r["lat"][(kind, s)][1]:.0f})' for s in LENS] for r in out])
    gpu = [r for r in out if r["cfg"][1] == "cuda" and 5 in r["wer"]]
    if len(gpu) > 1 and not a.no_lat:  # regra do padrao: menor latencia entre os que ficam a <= 1,5 ponto do melhor WER
        best = min(r["wer"][5][4] for r in gpu)
        ok = sorted((r for r in gpu if r["wer"][5][4] <= best + 1.5), key=lambda r: r["lat"][("f5", 4)][1])
        print(f"\nGPU com WER total <= {best + 1.5:.1f}% (melhor {best:.1f}% + 1,5), por latencia minima do final de 4 s: "
              + ", ".join(f'{r["cfg"][0]} {r["cfg"][2]} ({r["lat"][("f5", 4)][1]:.0f} ms, WER {r["wer"][5][4]:.1f})' for r in ok)
              + "\n(mesma arquitetura = mesma latencia: desempata o WER; com a GPU compartilhada o minimo oscila, repita em maquina livre)")
    print("\nFiltro de alucinacoes (Transcriber padrao):")
    sys.exit(0 if hallucination_check(Transcriber(), *fx["conv_4spk"]) else 1)


if __name__ == "__main__":
    main()
