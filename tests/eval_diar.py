"""Avaliação do app/diar.py: simula o uso real (turno a turno, em ordem) nos fixtures de tests/data.

Uso (da raiz do projeto, com o .venv):
  python tests/eval_diar.py                    # acurácia no threshold atual; sai com código 1 se faltar meta
  python tests/eval_diar.py --sweep            # varre o threshold (curva + melhor valor)
  python tests/eval_diar.py --bench [--threads N]   # tempo de embedding p/ 1 s / 3 s / 8 s
  python tests/eval_diar.py --real             # inclui vozes humanas reais (14 wavs da release do sherpa-onnx)
  python tests/eval_diar.py --pad              # turnos com preroll 250 ms + tail 150 ms (como o Segmenter entrega)
  python tests/eval_diar.py --harsh            # inclui 2spk/4spk degradados (banda de telefone + ruído branco 8 dB)
  python tests/eval_diar.py --e2e              # idem com o Segmenter real (utterances como o pipeline as vê)
  python tests/eval_diar.py --models a.onnx,b.onnx --sweep --bench   # compara modelos (cada um no seu melhor threshold)
  python tests/eval_diar.py --set MAX_S=4,SLACK=0.1                  # varre constantes de app/diar.py

Troca de voz DENTRO da utterance (SpeakerTracker.segments): tests/eval_split.py.

Métricas: acurácia por turno com o melhor mapeamento 1-1 rótulo->locutor (permutações; None conta como erro).
 online   = rótulo devolvido no momento da chamada (identify(final=True), sem revisão posterior); entre parênteses,
            % de turnos rotulados e a precisão só entre eles (None é melhor que rótulo errado);
 revisada = cada turno re-rotulado (argmax) contra os centróides finais: quanto a convergência ainda ajudaria;
 parcial  = identify(final=False) nos primeiros 1,0/1,5/2,0 s de cada turno, antes do final (cob = % com palpite,
            prec = % de acerto entre os palpites). Também confere que final=False não altera o estado.
"""
import argparse
import itertools
import json
import os
import random
import sys
import tempfile
import time
import urllib.request
import wave
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # roda sem PYTHONPATH
from app import diar  # noqa: E402
from app.events import SR  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# conv_4spk 80 (era 85): com o locutor provisório as 1as falas curtas (1,1 s) de B/C/D saem "?" de propósito
GOALS = {"conv_2spk": 95, "conv_4spk": 80, "conv_2spk_noisy": 90}
CROPS = (1.0, 1.5, 2.0)
PAD = (0.25, 0.15)  # preroll/tail do Segmenter (s)
REAL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
REAL = ["fangjun-sr-1", "fangjun-sr-2", "fangjun-sr-3", "fangjun-test-sr-1", "fangjun-test-sr-2", "leijun-sr-1",
        "leijun-sr-2", "leijun-test-sr-1", "leijun-test-sr-2", "leijun-test-sr-3", "liudehua-sr-1", "liudehua-sr-2",
        "liudehua-test-sr-1", "liudehua-test-sr-2"]


def read_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def degrade(x, snr_db=8.0):
    """Canal ruim: banda de telefone (300-3400 Hz) + ruído branco com SNR dado (fala em -20 dBFS)."""
    f = np.fft.rfft(x)
    hz = np.fft.rfftfreq(len(x), 1 / SR)
    y = np.fft.irfft(f * ((hz > 300) & (hz < 3400)), len(x)).astype(np.float32)
    n = np.random.default_rng(1).standard_normal(len(x)).astype(np.float32)
    return y + n * 10 ** (-20 / 20 - snr_db / 20)


def load_fixtures(pad=False, harsh=False):
    out = {}
    for name in list(GOALS) + (["conv_2spk", "conv_4spk"] if harsh else []):
        x = read_wav(os.path.join(DATA, name + ".wav"))
        tag = name
        if harsh and tag in out:  # segunda passada: versões degradadas
            x, tag = degrade(x), name + "_harsh"
        turns = json.load(open(os.path.join(DATA, name + ".json"), encoding="utf-8"))["turns"]
        a, b = PAD if pad else (0, 0)
        out[tag] = [(t["speaker"], x[max(0, int((t["start"] - a) * SR)):int((t["end"] + b) * SR)]) for t in turns]
    return out


def load_real(models_dir="models"):
    """Vozes humanas reais (3 locutores, 14 trechos de 2-8 s) em 12 ordens embaralhadas (a nota é a média)."""
    d = os.path.join(models_dir, "spk", "real")
    os.makedirs(d, exist_ok=True)
    clips = []
    for n in REAL:
        p = os.path.join(d, n + ".wav")
        if not os.path.isfile(p):
            urllib.request.urlretrieve(REAL_URL + n + ".wav", p)
        clips.append((n.split("-")[0], read_wav(p)))
    return [random.Random(s).sample(clips, len(clips)) for s in range(12)]


def best_map(true, pred):
    """(acurácia, {rótulo: locutor}) com o melhor mapeamento 1-1 por força bruta."""
    spk = sorted(set(true))
    labs = sorted({p for p in pred if p is not None})
    cnt = Counter(zip(pred, true))
    pool = labs + list(range(-1, -len(spk) - 1, -1))  # marcadores p/ locutores sem rótulo
    best, bm = -1, {}
    for perm in itertools.permutations(pool, len(spk)):
        s = sum(cnt[(p, t)] for p, t in zip(perm, spk) if p >= 0)
        if s > best:
            best, bm = s, {p: t for p, t in zip(perm, spk) if p >= 0}
    return best / len(true), bm


def memo_embed(tr):
    """Cacheia _embed por conteúdo (a varredura de threshold repete os mesmos trechos)."""
    raw, memo = tr._embed, {}

    def f(a):
        k = (len(a), hash(a.tobytes()))
        if k not in memo:
            memo[k] = raw(a)
        return memo[k]
    tr._embed = f


def run(tr, turns):
    """Simula o uso real. Devolve (acc online, acc revisada, {crop: (cobertura, precisão)}, nº de locutores)."""
    tr.reset()
    true = [s for s, _ in turns]
    pred, part = [], {c: [] for c in CROPS}
    for _, a in turns:
        snap = ([v.copy() for v in tr._sum], list(tr._w))
        for c in CROPS:
            part[c].append(tr.identify(a[:int(c * SR)], False) if len(a) > c * SR else "-")
        assert len(snap[0]) == len(tr._sum) and snap[1] == tr._w and all(
            np.array_equal(x, y) for x, y in zip(snap[0], tr._sum)), "identify(final=False) alterou o estado"
        pred.append(tr.identify(a, True))
    acc, m = best_map(true, pred)
    lab = [(t, p) for t, p in zip(true, pred) if p is not None]  # só os turnos que receberam rótulo
    onl = (len(lab) / len(true), sum(m.get(p) == t for t, p in lab) / max(1, len(lab)))
    C = np.stack(tr._sum)  # revisão: cada turno vs centróides finais (argmax, sem limiares)
    C = C / np.linalg.norm(C, axis=1, keepdims=True)
    rev, _ = best_map(true, [int((C @ tr._embed(a)).argmax()) for _, a in turns])
    ps = {}
    for c in CROPS:
        g = [(t, p) for t, p in zip(true, part[c]) if p != "-"]
        ans = [(t, p) for t, p in g if p is not None]
        ps[c] = (len(ans) / max(1, len(g)), sum(m.get(p) == t for t, p in ans) / max(1, len(ans)))
    return acc, rev, ps, sum(k is not None for k in tr._lab), onl


def run_set(tr, data):
    """data: {nome: turnos} ou {nome: [ordens]} (média). Devolve {nome: (acc, rev, ps, nº locutores, (cob, prec))}."""
    res = {}
    for name, v in data.items():
        rs = [run(tr, t) for t in (v if isinstance(v[0], list) else [v])]
        res[name] = (np.mean([r[0] for r in rs]), np.mean([r[1] for r in rs]),
                     {c: tuple(np.mean([r[2][c][i] for r in rs]) for i in (0, 1)) for c in CROPS},
                     np.mean([r[3] for r in rs]), tuple(np.mean([r[4][i] for r in rs]) for i in (0, 1)))
    return res


def show(res):
    miss = []
    for name, (acc, rev, ps, n, onl) in res.items():
        goal = GOALS.get(name)
        ok = ""
        if goal is not None:
            ok = f"  (meta {goal}%: {'OK' if acc * 100 >= goal - 1e-9 else 'ABAIXO'})"
            if acc * 100 < goal - 1e-9:
                miss.append(name)
        par = " | ".join(f"{c:.1f}s cob {v[0]:3.0%} prec {v[1]:4.0%}" for c, v in ps.items())
        print(f"{name:18s} online {acc:6.1%}{ok}  (rotulados {onl[0]:.0%}, prec {onl[1]:.1%})  revisada {rev:6.1%}  "
              f"locutores {n:.1f}\n{'':18s} parcial: {par}")
    return miss


def sweep(tr, data, grid):
    """Varre o threshold; devolve {t: {nome: acc online}} e imprime a curva."""
    memo_embed(tr)
    out = {}
    print(f"{'thr':>5s} " + " ".join(f"{n[:15]:>15s}" for n in data) + "   mín   média")
    for t in grid:
        tr.threshold = t
        r = run_set(tr, data)
        out[t] = {n: v[0] for n, v in r.items()}
        a = list(out[t].values())
        print(f"{t:5.2f} " + " ".join(f"{x:15.1%}" for x in a) + f"  {min(a):5.1%} {np.mean(a):6.1%}")
    return out


def best_of(curve):
    """Melhor threshold = maior média das acurácias; devolve (t, faixa a <= 1 pt do melhor, média)."""
    mean = {t: np.mean(list(v.values())) for t, v in curve.items()}
    bt = max(mean, key=lambda t: (round(mean[t], 4), -abs(t - 0.4)))
    ok = [t for t in sorted(mean) if mean[t] >= mean[bt] - 0.01]
    return bt, (min(ok), max(ok)), mean[bt]


def bench(tr, n=25):
    """Tempo de embedding (mediana / p90, ms) com a janela máxima liberada, e o de um identify() de 15 s (com cap)."""
    x = read_wav(os.path.join(DATA, "conv_2spk.wav"))[int(15 * SR):]
    old, rows = diar.MAX_S, []
    diar.MAX_S = 99
    for secs in (1, 3, 8):
        a = x[:secs * SR]
        for _ in range(3):
            tr._embed(a)
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            tr._embed(a)
            ts.append((time.perf_counter() - t0) * 1000)
        rows.append(f"{secs} s: {np.median(ts):5.1f} ms (p90 {np.percentile(ts, 90):5.1f})")
    diar.MAX_S = old
    a, ts = x[:15 * SR], []
    for _ in range(n):
        t0 = time.perf_counter()
        tr.identify(a, False)
        ts.append((time.perf_counter() - t0) * 1000)
    print(f"  embedding {' | '.join(rows)} | identify(15 s, cap {old:.0f} s): {np.median(ts):5.1f} ms")


def overlap(turns, a, b):
    """{locutor: segundos do gabarito dentro de [a, b]}."""
    by = {}
    for t in turns:
        o = max(0, min(b, t["end"]) - max(a, t["start"]))
        if o > 0:
            by[t["speaker"]] = by.get(t["speaker"], 0) + o
    return by


def e2e(tr):
    """Segmenter real (chunks de 32 ms) + política do pipeline (palpite parcial reaproveitado, final consolida).
    Turnos separados por gap < end_silence do Segmenter viram UMA utterance com 2+ locutores ("misturada"): um rótulo
    só não separa. Mede as utterances puras (acurácia do tracker) e as misturadas (rótulo é um dos presentes?)."""
    from app.segmenter import Segmenter
    for name in GOALS:
        x = read_wav(os.path.join(DATA, name + ".wav"))
        turns = json.load(open(os.path.join(DATA, name + ".json"), encoding="utf-8"))["turns"]
        x = np.concatenate([x, np.zeros(2 * SR, np.float32)])  # silêncio final fecha a última utterance
        seg, st, fin, lat = Segmenter(), {}, [], []
        tr.reset()
        for k in range(0, len(x) - 511, 512):
            for ev in seg.feed(x[k:k + 512], (k + 512) / SR):
                if ev.kind == "start":
                    continue
                s = st.setdefault(ev.utt_id, {"spk": None, "tried": 0, "first": None})
                final, n = ev.kind == "final", len(ev.audio)
                if final or (s["spk"] is None and n >= max(int(tr.min_audio_s * SR), s["tried"] * 3 // 2)):
                    s["tried"] = n
                    t = time.perf_counter()
                    got = tr.identify(ev.audio, final)
                    lat.append((time.perf_counter() - t) * 1000)
                    if not final and s["first"] is None and got is not None:
                        s["first"] = (got, ev.t0, ev.t_end)  # rótulo exibido no parcial e o trecho que cobria
                    s["spk"] = s["spk"] if got is None else got
                if final:
                    fin.append((ev.t0, ev.t_end, s["spk"], s["first"]))
                    st.pop(ev.utt_id)
        rows = []
        for t0, t1, spk, first in fin:
            by = overlap(turns, t0, t1)
            if by:
                v = sorted(by.values())
                rows.append((max(by, key=by.get), by, len(v) < 2 or v[-2] / sum(v) < 0.15, spk, first))
        pure, mixed = [r for r in rows if r[2]], [r for r in rows if not r[2]]
        acc, m = best_map([r[0] for r in pure], [r[3] for r in pure])
        lab = [r for r in pure if r[3] is not None]
        par = [(m.get(r[4][0]), overlap(turns, *r[4][1:])) for r in rows if r[4]]
        ok = sum(g == max(b, key=b.get) for g, b in par if b)
        print(f"{name:16s} utterances {len(rows)} p/ {len(turns)} turnos | puras {len(pure)}: acc {acc:.1%} "
              f"prec {sum(m.get(r[3]) == r[0] for r in lab) / max(1, len(lab)):.1%} | misturadas {len(mixed)}: rótulo é um dos "
              f"presentes em {sum(m.get(r[3]) in r[1] for r in mixed) / max(1, len(mixed)):.0%} | parcial: {len(par)}/"
              f"{len(rows)} utts com rótulo, prec {ok / max(1, len(par)):.1%} | identify {np.median(lat):.0f} ms "
              f"(p95 {np.percentile(lat, 95):.0f})")


def check_profiles(models_dir="models"):
    """Check executável do provisório e dos perfis (pin/merge/forget/save/reset/speakers.json), com vozes dos fixtures."""
    def clips(name, idx):
        x = read_wav(os.path.join(DATA, name + ".wav"))
        t = json.load(open(os.path.join(DATA, name + ".json"), encoding="utf-8"))["turns"]
        return [x[int(t[i]["start"] * SR):int(t[i]["end"] * SR)] for i in idx]

    A, B = clips("conv_2spk", (0, 2, 4, 6)), clips("conv_2spk", (1, 7, 9))  # A: 2,6 5,1 10,9 5,5 s; B: 3,7 5,9 5,9 s
    s12 = int(1.2 * SR)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "speakers.json")
        tr = diar.SpeakerTracker(models_dir=models_dir, profiles_path=p)
        # provisório: 2,6 s de voz nova = sem id (nem no palpite); ao somar confirm_s (3 s) ganha o id
        assert tr.identify(A[0], True) is None and tr.identify(A[0], False) is None
        a, b = tr.identify(A[1], True), tr.identify(B[0], True)
        assert (a, b) == (0, 1), (a, b)
        assert not tr.pin(7) and not os.path.exists(p)  # id que não existe
        assert tr.pin(a) and diar.saved_speakers(p) == [a]
        # reset: o fixado continua com o mesmo id; ids novos começam depois do maior fixado
        tr.reset()
        assert tr.identify(A[2][:s12], True) == a and tr.identify(B[1], True) == a + 1
        # app reaberto (tracker novo lendo o arquivo): a mesma voz volta com o mesmo id já na 1ª fala >= 1 s
        tr = diar.SpeakerTracker(models_dir=models_dir, profiles_path=p)
        assert tr.identify(A[3][:s12], False) == a and tr.identify(A[3][:s12], True) == a
        b = tr.identify(B[1], True)
        assert b == a + 1, b
        # save: o centróide gravado acompanha a fala nova
        e0 = json.load(open(p))["profiles"][0]["emb"]
        tr.identify(A[2], True)
        tr.save()
        assert json.load(open(p))["profiles"][0]["emb"] != e0
        # merge: src some (peso somado; id nunca reaproveitado na sessão); src fixado passa a fixação para dst
        assert tr.pin(b) and diar.saved_speakers(p) == [a, b]
        w = tr._w[tr._lab.index(a)] + tr._w[tr._lab.index(b)]
        tr.merge(a, b)
        assert tr._lab == [b] and abs(tr._w[0] - w) < 1e-6 and tr._next > b and diar.saved_speakers(p) == [b]
        # forget: tira do arquivo (sem nenhum fixado o arquivo some); no reset a voz esquecida não volta
        tr.forget(b)
        assert not os.path.exists(p) and tr._lab == [b]
        tr.reset()
        assert tr._lab == [] and tr.identify(B[2], True) == 0
        assert tr.pin(0) and os.path.exists(p)
        tr.forget(None)
        assert not os.path.exists(p) and not tr._pin
        # forget_saved / saved_speakers direto no arquivo (pipeline sem tracker carregado)
        tr.identify(A[1], True)
        assert tr.pin(0) and tr.pin(1) and diar.saved_speakers(p) == [0, 1]
        diar.forget_saved(p, 0)
        assert diar.saved_speakers(p) == [1]
        diar.forget_saved(p)
        assert not os.path.exists(p)
        # outro modelo no arquivo invalida os perfis
        tr.pin(1)
        j = json.load(open(p))
        with open(p, "w") as f:
            json.dump({**j, "model": "outro.onnx"}, f)
        assert diar.saved_speakers(p) == [] and diar.SpeakerTracker(models_dir=models_dir, profiles_path=p)._lab == []
    # tabela cheia: descarta o provisório mais antigo; sem provisório, voz nova = None
    A, B, C, C2, D = clips("conv_4spk", (4, 1, 2, 10, 18))  # 4,9 1,1 1,1 17,5 4,6 s
    tr = diar.SpeakerTracker(models_dir=models_dir, max_speakers=2)
    assert tr.identify(A, True) == 0 and tr.identify(B, True) is None and tr.identify(C, True) is None
    assert tr._lab == [0, None]
    assert tr.identify(C2, True) == 1 and tr.identify(D, True) is None and tr._lab == [0, 1]
    print("perfis e provisório: ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--pad", action="store_true")
    ap.add_argument("--harsh", action="store_true")
    ap.add_argument("--e2e", action="store_true", help="com o Segmenter real (utterances como o pipeline as vê)")
    ap.add_argument("--models", default=None, help="arquivos .onnx separados por vírgula")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--set", default="", help="constantes de app/diar.py, ex.: MAX_S=3,SLACK=0.1")
    ap.add_argument("--threads", type=int, default=None, help="threads do onnxruntime (padrão: diar.THREADS)")
    ap.add_argument("--models-dir", default="models")
    a = ap.parse_args()
    if a.threads:
        diar.THREADS = a.threads
    for kv in filter(None, a.set.split(",")):
        k, v = kv.split("=")
        assert hasattr(diar, k), k
        setattr(diar, k, float(v))
    check_profiles(a.models_dir)
    data = load_fixtures(a.pad, a.harsh)
    if a.real:
        data["real_3spk"] = load_real(a.models_dir)
    grid = [round(x, 2) for x in np.arange(0.20, 0.901, 0.05)]
    summary, miss = [], []
    for mdl in (a.models.split(",") if a.models else [diar.MODEL]):
        diar.MODEL = mdl
        tr = diar.SpeakerTracker(models_dir=a.models_dir, **({} if a.threshold is None else {"threshold": a.threshold}))
        p = diar.ensure_model(a.models_dir)
        assert os.path.isfile(p) and diar.ensure_model(a.models_dir) == p  # idempotente (não rebaixa)
        print(f"\n=== {mdl} (threshold {tr.threshold}, threads {diar.THREADS}{', pad' if a.pad else ''})")
        if a.bench:
            bench(tr)
        if a.sweep:
            curve = sweep(tr, data, grid)
            bt, (lo, hi), m = best_of(curve)
            print(f"melhor threshold {bt:.2f} (média {m:.1%}); faixa a <= 1 pt do melhor: {lo:.2f}-{hi:.2f}")
            tr.threshold = bt
            summary.append((mdl, bt, lo, hi, curve[bt]))
        miss = show(run_set(tr, data))
        if a.e2e:
            print("-- ponta a ponta (Segmenter real)")
            e2e(tr)
    if len(summary) > 1:
        print("\n=== resumo (acurácia online no melhor threshold de cada modelo)")
        for mdl, bt, lo, hi, accs in summary:
            print(f"{mdl:52s} thr {bt:.2f} [{lo:.2f}-{hi:.2f}] " + " ".join(f"{n[5:14]} {v:5.1%}" for n, v in accs.items()))
    if miss:
        sys.exit(f"metas não atingidas: {', '.join(miss)}")


if __name__ == "__main__":
    main()
