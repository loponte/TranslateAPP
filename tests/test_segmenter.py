"""Teste offline do Segmenter com os fixtures de tests/data (sem áudio ao vivo).

    python tests\\test_segmenter.py        (imprime os números)   ou   pytest tests\\test_segmenter.py

Alimenta o wav em chunks de 32 ms com timestamps sintéticos (t_end = fim do chunk em segundos desde o início do wav),
então os tempos dos eventos comparam direto com o gabarito (turnos em segundos desde o início do wav).
"""
from __future__ import annotations

import functools
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.events import SR  # noqa: E402
from app.segmenter import Segmenter  # noqa: E402

DATA = Path(__file__).parent / "data"
CH = 512                       # chunk de 32 ms
MAX_UTT, END_MS = 15.0, 450    # defaults do Segmenter


def load(name: str) -> np.ndarray:
    with wave.open(str(DATA / f"{name}.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2), name
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def turns(name: str) -> list[dict]:
    return json.loads((DATA / f"{name.replace('_noisy', '')}.json").read_text())["turns"]


def run(x: np.ndarray, chunk: int = CH, **kw):
    """-> (eventos, instante de áudio em que cada um foi emitido, custo de cada feed em ms)."""
    seg, ev, at, cost = Segmenter(**kw), [], [], []
    for i in range(0, len(x) - chunk + 1, chunk):
        t = time.perf_counter()
        out = seg.feed(x[i:i + chunk], (i + chunk) / SR)
        cost.append((time.perf_counter() - t) * 1e3)
        ev += out
        at += [(i + chunk) / SR] * len(out)
    return ev, at, np.array(cost)


@functools.cache
def _run(name: str):
    return run(load(name))


def finals(ev):
    return [e for e in ev if e.kind == "final"]


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def check_contract(ev):
    """start -> partial* -> final por utterance, ids 0..N-1 em ordem, t0 fixo, t_end dos parciais crescente, um aberto
    por vez. (O final de uma utterance cortada em max_utt_s pode ter t_end < o do último parcial: o resto do áudio
    do parcial passou para a utterance seguinte.)"""
    open_id, nxt, last_t = None, 0, 0.0
    for e in ev:
        if e.kind == "start":
            assert open_id is None and e.utt_id == nxt and e.audio is None
            open_id, nxt, last_t, t0 = e.utt_id, nxt + 1, e.t_end, e.t0
        else:
            assert e.utt_id == open_id and e.t0 == t0 and e.t_end > e.t0
            assert e.audio.dtype == np.float32 and e.audio.ndim == 1 and len(e.audio) >= 0.25 * SR
            if e.kind == "partial":
                assert e.t_end >= last_t
                last_t = e.t_end
            else:
                open_id = None
    return nxt


def check_conversation(name: str):
    ev, _, _ = _run(name)
    check_contract(ev)
    fin, tt = finals(ev), turns(name)
    spans = [(e.t_end - len(e.audio) / SR, e.t_end) for e in fin]   # trecho que o ASR vai receber (com preroll e tail)
    for i, t in enumerate(tt):
        d = t["end"] - t["start"]
        if d < 0.6:
            continue
        cov = sum(overlap(a, b, t["start"], t["end"]) for a, b in spans) / d   # as utterances não se sobrepõem
        assert cov >= 0.95, f"{name} turno {i} ({t['start']}-{t['end']}) coberto só {cov:.0%}"
        first = next(f for f in fin if overlap(f.t0, f.t_end, t["start"], t["end"]) > 0.15)
        assert first.t0 <= t["start"] + 0.4, f"{name} turno {i}: utterance começou tarde ({first.t0:.2f} x {t['start']})"
        gap = t["start"] - tt[i - 1]["end"] if i else 9.0
        if gap >= END_MS / 1000 + 0.15:     # com silêncio suficiente antes, tem que abrir utterance nova
            assert abs(first.t0 - t["start"]) <= 0.4, f"{name} turno {i}: início {first.t0:.2f} x {t['start']}"
    assert max(f.t_end - f.t0 for f in fin) <= MAX_UTT + 0.1
    return fin, tt


def test_2spk():
    fin, tt = check_conversation("conv_2spk")
    ev, at, _ = _run("conv_2spk")
    # ritmo dos parciais: 1º com ~min_partial_ms de fala, depois a cada ~partial_every_ms de fala
    iv, first = [], []
    for u in range(len(fin)):
        ps = [e for e in ev if e.kind == "partial" and e.utt_id == u]
        first += [ps[0].t_end - ps[0].t0]
        iv += list(np.diff([p.t_end for p in ps]))
    assert 0.45 <= np.mean(first) <= 0.75 and 0.58 <= np.min(iv) and 0.6 <= np.median(iv) <= 0.75, (np.mean(first), np.min(iv), np.median(iv))
    return np.mean(first), np.median(iv), np.mean(iv), np.max(iv)


def test_4spk_monologos_cortados_em_pausas():
    fin, tt = check_conversation("conv_4spk")
    x = load("conv_4spk")
    for t in (t for t in tt if t["end"] - t["start"] > MAX_UTT):    # monólogos de 17-20 s (sem pausa > 0,3 s)
        parts = [f for f in fin if overlap(f.t0, f.t_end, t["start"], t["end"]) > 0.5]
        assert len(parts) >= 2, f"monólogo {t['start']}-{t['end']} saiu em 1 utterance"
        for f in parts[:-1]:   # o corte (fim do áudio da utterance) tem que cair em silêncio, não no meio de palavra
            tail = x[int((f.t_end - 0.064) * SR):int(f.t_end * SR)]
            assert np.sqrt(np.mean(tail ** 2)) < 0.01, f"corte em {f.t_end:.2f}s fora de pausa"


def test_noisy():
    fin, tt = check_conversation("conv_2spk_noisy")
    spur = [f for f in fin if not any(overlap(f.t0, f.t_end, t["start"], t["end"]) > 0.1 for t in tt)]
    per_min = len(spur) / (len(load("conv_2spk_noisy")) / SR / 60)
    assert per_min <= 2, f"{len(spur)} utterances espúrias ({per_min:.1f}/min)"
    return len(spur), per_min


def _monologo(x: np.ndarray, max_pause: float) -> np.ndarray:
    """Os monólogos longos do 4spk emendados, com cada pausa (RMS < -50 dBFS em frames de 10 ms) limitada a max_pause s."""
    out = []
    for t in (t for t in turns("conv_4spk") if t["end"] - t["start"] > MAX_UTT):
        a = x[int(t["start"] * SR):int(t["end"] * SR)]
        rms = np.sqrt((a[:len(a) // 160 * 160].reshape(-1, 160) ** 2).mean(axis=1))
        i = 0
        while i < len(rms):
            j = i
            while j < len(rms) and (rms[j] > 10 ** (-50 / 20)) == (rms[i] > 10 ** (-50 / 20)):
                j += 1
            seg = a[i * 160:j * 160]
            out.append(seg if rms[i] > 10 ** (-50 / 20) else seg[:int(max_pause * SR)])
            i = j
    return np.concatenate(out)


def test_corte_em_max_utt():
    x = load("conv_4spk")
    # 1) pausas de 0,2 s (< 250 ms: nada fecha por silêncio): só o limite de 15 s corta, e na última pausa
    m = _monologo(x, 0.2)
    ev, at, _ = run(m)
    fin = finals(ev)
    assert len(fin) >= 2 and all(f.t_end - f.t0 <= MAX_UTT + 0.1 for f in fin)
    for f, e in zip(fin, [a for a, k in zip(at, ev) if k.kind == "final"]):
        if e - f.t0 >= MAX_UTT - 0.1:   # fechada pelo limite: o áudio acaba dentro de uma pausa
            tail = m[int((f.t_end - 0.064) * SR):int(f.t_end * SR)]
            assert np.sqrt(np.mean(tail ** 2)) < 0.01
    check_contract(ev)
    # 2) sem pausa nenhuma: corte seco a cada 15 s, a próxima utterance começa no corte
    m = _monologo(x, 0.0)
    ev, _, _ = run(m)
    fin = finals(ev)
    assert len(fin) >= 3 and all(abs((f.t_end - f.t0) - MAX_UTT) <= 0.1 for f in fin[:3])
    assert all(abs(b.t0 - a.t_end) <= 0.1 for a, b in zip(fin, [e for e in ev if e.kind == "start"][1:]))
    check_contract(ev)


def test_chunks_de_qualquer_tamanho():
    x = load("conv_2spk")[:40 * SR]
    ref = [(e.kind, e.utt_id, len(e.audio) if e.audio is not None else -1, e.t0, e.t_end) for e in run(x)[0]]
    assert len(ref) > 10
    for c in (160, 333, 1000, 4096):
        got = [(e.kind, e.utt_id, len(e.audio) if e.audio is not None else -1, e.t0, e.t_end) for e in run(x, chunk=c)[0]]
        assert len(got) == len(ref) and all(g[:3] == r[:3] and abs(g[3] - r[3]) < 1e-6 and abs(g[4] - r[4]) < 1e-6
                                            for g, r in zip(got, ref)), f"chunk={c}"


def test_silencio_ruido_e_reset():
    rng = np.random.default_rng(0)
    assert run(np.zeros(10 * SR, np.float32))[0] == []
    assert run((rng.standard_normal(10 * SR) * 0.02).astype(np.float32))[0] == []
    x = load("conv_2spk")[:8 * SR]
    seg, n = Segmenter(), 0
    ev = []
    for i in range(0, 3 * SR, CH):     # 3 s: utterance em curso (fala desde 0,6 s)
        ev += seg.feed(x[i:i + CH], (i + CH) / SR)
    assert [e.kind for e in ev][:1] == ["start"] and not finals(ev)
    seg.reset()
    ev2 = []
    for i in range(0, len(x) - CH + 1, CH):
        ev2 += seg.feed(x[i:i + CH], 100 + (i + CH) / SR)
    assert ev2[0].kind == "start" and ev2[0].utt_id == 1 and finals(ev2)   # ids não voltam a 0; t0 segue o t_end novo
    assert abs(ev2[0].t0 - (100 + 0.64)) < 0.1


def test_custo_do_feed():
    cost = _run("conv_2spk")[2]
    mean, p99 = cost.mean(), np.percentile(cost, 99)
    assert mean < 1.0 and p99 < 5.0, (mean, p99)
    return mean, p99, cost.max()


if __name__ == "__main__":
    t0 = time.time()
    r = test_2spk()
    print(f"2spk: ok | 1o parcial com {r[0]:.2f}s de fala; intervalo entre parciais: mediana {r[1]:.2f}s média {r[2]:.2f}s máx {r[3]:.2f}s")
    for f in (test_4spk_monologos_cortados_em_pausas, test_corte_em_max_utt, test_chunks_de_qualquer_tamanho, test_silencio_ruido_e_reset):
        f()
        print(f"{f.__name__}: ok")
    n, per_min = test_noisy()
    print(f"noisy: ok | utterances espúrias: {n} ({per_min:.1f}/min)")
    m, p99, mx = test_custo_do_feed()
    print(f"feed() de 32 ms: média {m:.3f} ms, p99 {p99:.3f} ms, máx {mx:.3f} ms")
    print(f"TODOS OK em {time.time() - t0:.1f}s")
