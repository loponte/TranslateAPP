"""Avaliação de SpeakerTracker.segments() (troca de locutor dentro da utterance) nos fixtures de tests/data.

Uso (da raiz do projeto, com o .venv):
  python tests/eval_split.py                 # Segmenter real (chunks de 32 ms) em 2spk, 4spk e 2spk_noisy + turnos sozinhos
  python tests/eval_split.py --hard          # + emendas sintéticas de turnos (troca sem pausa, pausa curta, sobreposta)
  python tests/eval_split.py --real          # + o mesmo com 14 trechos de vozes humanas reais (models/spk/real)
  python tests/eval_split.py --set PAUSE_S=0.15,SHORT_K=0.2 --threshold 0.45   # varre constantes de app/diar.py
Sai com código 1 se faltar meta (falso split <= 3 %, erro de fronteira mediana <= 0,25 s e p90 <= 0,5 s).

Métricas (antes = 1 rótulo por utterance, identify(final=True); depois = segments()):
 acc      acurácia de locutor ponderada por tempo (segundos de fala do gabarito com o rótulo certo; melhor mapeamento
          1-1 rótulo->locutor no arquivo todo; None = erro);
 trocas   trocas do gabarito dentro de uma utterance (turnos vizinhos de locutores diferentes) que ganharam uma fronteira
          a <= 1 s; erro = distância da fronteira ao silêncio entre os turnos (0 = dentro dele) e ao ponto médio dele;
 falso    utterances de 1 locutor (no gabarito) que o segments() dividiu; extras = fronteiras sem troca no gabarito;
 ms       latência de uma chamada (mediana / p95) neste PC, com outros processos rodando.
"""
import argparse
import itertools
import json
import os
import random
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # roda sem PYTHONPATH
from app import diar  # noqa: E402
from app.events import SR  # noqa: E402
from app.segmenter import Segmenter  # noqa: E402
import eval_diar as ed  # noqa: E402  (mesma pasta: read_wav, load_real, DATA, PAD)

TOL = 1.0                                  # s: fronteira a mais que isso de uma troca do gabarito não conta como acerto
GOAL_FALSE, GOAL_MED, GOAL_P90, GOAL_MS = 0.03, 0.25, 0.5, 80
GAPS = (0.3, 0.15, 0.0, -0.3)              # silêncio entre os turnos das emendas (negativo = sobreposição)


def turns_of(name):
    return json.load(open(os.path.join(ed.DATA, name + ".json"), encoding="utf-8"))["turns"]


def utterances(x):
    """Utterances finais do Segmenter real (como o pipeline as recebe): [(início no wav em s, áudio)]."""
    seg, out = Segmenter(), []
    x = np.concatenate([x, np.zeros(2 * SR, np.float32)])  # silêncio final fecha a última
    for k in range(0, len(x) - 511, 512):
        for ev in seg.feed(x[k:k + 512], (k + 512) / SR):
            if ev.kind == "final":
                out.append((ev.t_end - len(ev.audio) / SR, ev.audio.copy()))
    return out


def turn_utts(x, turns):
    """Cada turno sozinho, com o preroll/tail do Segmenter (sem invadir o turno vizinho)."""
    out = []
    for i, t in enumerate(turns):
        lo = max(t["start"] - ed.PAD[0], turns[i - 1]["end"] if i else 0.0)
        hi = min(t["end"] + ed.PAD[1], turns[i + 1]["start"] if i + 1 < len(turns) else 1e9)
        out.append((lo, x[int(lo * SR):int(hi * SR)]))
    return out


def gt_in(turns, a0, d):
    """Turnos do gabarito dentro da janela [a0, a0+d] da utterance, em tempo relativo: [(locutor, início, fim)]."""
    return [(t["speaker"], max(0.0, t["start"] - a0), min(d, t["end"] - a0))
            for t in turns if t["end"] > a0 and t["start"] < a0 + d]


def best_total(cnt):
    """Segundos certos com o melhor mapeamento 1-1 rótulo -> locutor (None nunca casa)."""
    spk = sorted({s for _, s in cnt})
    pool = sorted({p for p, _ in cnt if p is not None}) + [-1 - i for i in range(len(spk))]  # -k: locutor sem rótulo
    return max(sum(cnt[(p, t)] for p, t in zip(perm, spk) if p >= 0) for perm in itertools.permutations(pool, len(spk)))


def check(segs, d):
    """Contrato de segments(): ladrilha [0, d], em ordem, vizinhos com locutores diferentes."""
    assert segs and segs[0][0] == 0.0 and segs[-1][1] == d, segs
    assert all(a < b for a, b, _ in segs) and all(x[1] == y[0] and x[2] != y[2] for x, y in zip(segs, segs[1:])), segs


def score(utts, turns, preds, ms):
    """preds[i] = [(a, b, rótulo)] da utterance i. Devolve as contagens brutas (somáveis entre arquivos)."""
    r = dict(cnt=Counter(), tot=0.0, n_ch=0, e_gap=[], e_mid=[], mono=0, split=0, extra=0, ms=list(ms), n=len(utts))
    for (a0, au), segs in zip(utts, preds):
        gt = gt_in(turns, a0, len(au) / SR)
        for sp, s, e in gt:
            r["tot"] += e - s
            for a, b, lab in segs:
                r["cnt"][(lab, sp)] += max(0.0, min(e, b) - max(s, a))
        left = [segs[k][1] for k in range(len(segs) - 1)]  # fronteiras previstas (rótulos vizinhos sempre diferem)
        if len({sp for sp, _, _ in gt}) == 1:
            r["mono"] += 1
            r["split"] += len(segs) > 1
        else:
            for (s1, _, e1), (s2, b2, _) in zip(gt, gt[1:]):
                if s1 == s2:
                    continue
                r["n_ch"] += 1
                g0, g1 = sorted((e1, b2))
                dist = lambda p: max(g0 - p, p - g1, 0.0)  # noqa: E731
                if left and dist(p := min(left, key=dist)) <= TOL:
                    left.remove(p)
                    r["e_gap"].append(dist(p))
                    r["e_mid"].append(abs(p - (g0 + g1) / 2))
        r["extra"] += len(left)
    return r


def snap(tr):
    return [v.copy() for v in tr._sum], list(tr._w)


def restore(tr, st):
    tr._sum, tr._w = [v.copy() for v in st[0]], list(st[1])


def evaluate(tr, utts, turns):
    """antes (identify) e depois (segments) na mesma sequência de utterances, com o tracker zerado em cada.
    Confere também: sem candidato a corte, segments() dá rótulo e centróides idênticos aos do identify(final=True)."""
    res = {}
    for mode in ("antes", "depois"):
        tr.reset()
        preds, ms = [], []
        for _, au in utts:
            s0 = snap(tr)
            t = time.perf_counter()
            p = [(0.0, len(au) / SR, tr.identify(au, True))] if mode == "antes" else tr.segments(au)
            ms.append((time.perf_counter() - t) * 1000)
            check(p, len(au) / SR)
            if mode == "depois" and len(p) == 1 and len(au) >= 1.2 * SR and len(tr._atoms(au)) < 2:
                s1 = snap(tr)
                restore(tr, s0)
                assert tr.identify(au, True) == p[0][2], "sem corte deve dar o rótulo do identify"
                s2 = snap(tr)
                same = len(s1[0]) == len(s2[0]) and all(np.allclose(x, y, atol=1e-6) for x, y in zip(s1[0], s2[0]))
                assert same and np.allclose(s1[1], s2[1], atol=1e-6), "sem corte deve atualizar os centróides como o identify"
            preds.append(p)
        res[mode] = score(utts, turns, preds, ms)
    return res


def merge(rs):
    """Soma resultados de vários arquivos (acc vira média ponderada pelos segundos de fala)."""
    out = dict(ok=0.0, tot=0.0, n_ch=0, e_gap=[], e_mid=[], mono=0, split=0, extra=0, ms=[], n=0)
    for r in rs:
        out["ok"] += best_total(r["cnt"]) if r["cnt"] else 0.0
        for k in ("tot", "n_ch", "mono", "split", "extra", "n"):
            out[k] += r[k]
        for k in ("e_gap", "e_mid", "ms"):
            out[k] += r[k]
    return out


def pct(v, q):
    return float(np.percentile(v, q)) if len(v) else float("nan")


def err_txt(a):
    if not a["e_gap"]:
        return ""
    return (f" erro mediana {pct(a['e_gap'], 50):.2f} s p90 {pct(a['e_gap'], 90):.2f} s"
            f" (do ponto médio {pct(a['e_mid'], 50):.2f}/{pct(a['e_mid'], 90):.2f})")


def show(name, res):
    b, a = merge([res["antes"]]), merge([res["depois"]])
    print(f"{name:18s} utts {a['n']:3d} (1 loc. {a['mono']:3d}) | acc {b['ok'] / b['tot']:6.1%} -> {a['ok'] / a['tot']:6.1%} | "
          f"trocas {len(a['e_gap'])}/{a['n_ch']}{err_txt(a)} | falso split {a['split']}/{a['mono']}, extras {a['extra']} | "
          f"ms {pct(a['ms'], 50):.0f}/{pct(a['ms'], 95):.0f} (antes {pct(b['ms'], 50):.0f}/{pct(b['ms'], 95):.0f})")


def splice(a, b, gap):
    """[0,25 s | a | gap | b | 0,15 s] (gap < 0: b entra sobre o fim de a, cada um a 70 %). Devolve (áudio, gabarito)."""
    pre, n1, k = int(ed.PAD[0] * SR), len(a), 0.7 if gap < 0 else 1.0
    s2 = n1 + int(gap * SR)
    y = np.zeros(pre + max(n1, s2 + len(b)) + int(ed.PAD[1] * SR), np.float32)
    y[pre:pre + n1] += a * k
    y[pre + s2:pre + s2 + len(b)] += b * k
    return y, [(pre / SR, (pre + n1) / SR), ((pre + s2) / SR, (pre + s2 + len(b)) / SR)]


def run_splices(tr, title, clips, per=100, seed=0):
    """clips = [(locutor, áudio)]. Emenda pares (de locutores diferentes e do mesmo) com cada gap de GAPS: a fronteira
    deve aparecer só na troca. Tracker aquecido com os clipes (como numa call em andamento), estado restaurado a cada caso.
    Devolve os resultados por (mesmo, gap)."""
    tr.reset()
    for _, a in clips:
        tr.identify(a, True)
    st = snap(tr)
    rng, rows = random.Random(seed), {}
    for gap in GAPS:
        for same in (False, True):
            pairs = [(i, j) for i, (si, a) in enumerate(clips) for j, (sj, b) in enumerate(clips)
                     if i != j and (si == sj) == same and len(a) >= 0.8 * SR and len(b) >= 0.5 * SR]
            for i, j in rng.sample(pairs, min(per, len(pairs))):
                y, g = splice(clips[i][1], clips[j][1], gap)
                restore(tr, st)
                t = time.perf_counter()
                segs = tr.segments(y)
                ms = (time.perf_counter() - t) * 1000
                check(segs, len(y) / SR)
                turns = [{"speaker": clips[k][0], "start": s, "end": e} for k, (s, e) in zip((i, j), g)]
                rows.setdefault((same, gap), []).append(score([(0.0, y)], turns, [segs], [ms]))
    print(f"-- emendas sintéticas ({title}): gap = silêncio entre os turnos (negativo = sobreposição)")
    for gap in GAPS:
        d, s = merge(rows[(False, gap)]), merge(rows[(True, gap)])
        print(f"gap {gap:+.2f} s | locutores diferentes: troca achada {len(d['e_gap'])}/{d['n_ch']}{err_txt(d)}, "
              f"extras {d['extra']} | mesmo locutor: falso split {s['split']}/{s['mono']} | ms {pct(d['ms'] + s['ms'], 50):.0f}")
    return rows


def edge_cases(tr):
    """Entradas degeneradas: sempre 1 trecho que cobre tudo e rótulo como no identify (silêncio/ruído/curto -> None)."""
    rng = np.random.default_rng(0)
    noise = lambda s: (rng.standard_normal(int(s * SR)) * 0.05).astype(np.float32)  # noqa: E731
    assert tr.segments(np.zeros(0, np.float32)) == [(0.0, 0.0, None)]
    assert tr.segments(np.zeros(3 * SR, np.float32)) == [(0.0, 3.0, None)]
    for y in (np.zeros(100, np.float32), noise(0.2), noise(1.0), noise(3.0).astype(np.float64),  # float64 também vale
              np.concatenate([noise(1.0), np.zeros(SR, np.float32), noise(1.0)]), noise(25.0)):
        check(tr.segments(y), len(y) / SR)


def real_clips(models_dir):
    """14 trechos de 3 vozes humanas reais (2-8 s) da release do sherpa-onnx (baixados por eval_diar.load_real)."""
    ed.load_real(models_dir)
    return [(n.split("-")[0], ed.read_wav(os.path.join(models_dir, "spk", "real", n + ".wav"))) for n in ed.REAL]


def run_real_alone(tr, clips):
    """Cada voz real sozinha (com preroll/tail): 1 locutor, não pode dividir. Devolve (divididas, total)."""
    tr.reset()
    for _, c in clips:
        tr.identify(c, True)
    z = lambda s: np.zeros(int(s * SR), np.float32)  # noqa: E731
    ys, ms, sp = [np.concatenate([z(ed.PAD[0]), c, z(ed.PAD[1])]) for _, c in clips], [], 0
    for y in ys:
        t0 = time.perf_counter()
        segs = tr.segments(y)
        ms.append((time.perf_counter() - t0) * 1000)
        check(segs, len(y) / SR)
        sp += len(segs) > 1
    print(f"{'vozes reais sozinhas':18s} utts {len(ys)} | falso split {sp}/{len(ys)} | ms {pct(ms, 50):.0f}/{pct(ms, 95):.0f}")
    return sp, len(ys)


def latency_8s(tr):
    """Mediana/p95 de segments() em utterances de 7-9 s dos fixtures (5 repetições, depois do aquecimento)."""
    cand = [au for n in ("conv_2spk", "conv_4spk") for _, au in utterances(ed.read_wav(os.path.join(ed.DATA, n + ".wav")))
            if 7 * SR <= len(au) <= 9 * SR]
    ts = []
    for au in cand:
        tr.segments(au)
        for _ in range(5):
            t0 = time.perf_counter()
            tr.segments(au)
            ts.append((time.perf_counter() - t0) * 1000)
    print(f"latência de segments() p/ utterances de 7-9 s ({len(cand)} utts x 5): mediana {pct(ts, 50):.0f} ms, p95 {pct(ts, 95):.0f} ms")
    return pct(ts, 50)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hard", action="store_true", help="também emendas sintéticas (turnos do conv_4spk)")
    ap.add_argument("--real", action="store_true", help="também vozes humanas reais (sozinhas e emendadas)")
    ap.add_argument("--harsh", action="store_true", help="também 2spk/4spk degradados (banda de telefone + ruído 8 dB)")
    ap.add_argument("--per", type=int, default=100, help="emendas por (gap, mesmo/diferente)")
    ap.add_argument("--set", default="", help="constantes de app/diar.py, ex.: PAUSE_S=0.15,SHORT_K=0.2")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--models-dir", default="models")
    a = ap.parse_args()
    for kv in filter(None, a.set.split(",")):
        k, v = kv.split("=")
        assert hasattr(diar, k), k
        setattr(diar, k, float(v))
    tr = diar.SpeakerTracker(models_dir=a.models_dir, **({} if a.threshold is None else {"threshold": a.threshold}))
    print(f"threshold {tr.threshold}, threads {diar.THREADS}" + (f", {a.set}" if a.set else ""))
    edge_cases(tr)
    sets = [(n, n, False) for n in ("conv_2spk", "conv_4spk", "conv_2spk_noisy")]
    sets += [(n + "_harsh", n, True) for n in ("conv_2spk", "conv_4spk")] if a.harsh else []
    tot_b, tot_a, tot_t = [], [], []
    for tag, name, harsh in sets:
        x = ed.read_wav(os.path.join(ed.DATA, name + ".wav"))
        x, turns = (ed.degrade(x) if harsh else x), turns_of(name)
        res = evaluate(tr, utterances(x), turns)
        tot_b.append(res["antes"])
        tot_a.append(res["depois"])
        show(tag, res)
        r = evaluate(tr, turn_utts(x, turns), turns)
        tot_t.append(r["depois"])
        show(tag + " (turnos)", r)
    b, d, t = merge(tot_b), merge(tot_a), merge(tot_t)
    print(f"{'TOTAL':18s} acc {b['ok'] / b['tot']:6.1%} -> {d['ok'] / d['tot']:6.1%} | trocas {len(d['e_gap'])}/{d['n_ch']}"
          f"{err_txt(d)} | falso split {d['split']}/{d['mono']} (turnos sozinhos {t['split']}/{t['mono']})")
    splits, monos = d["split"] + t["split"], d["mono"] + t["mono"]  # tudo que tem 1 locutor só e não podia dividir
    if a.hard:
        x = ed.read_wav(os.path.join(ed.DATA, "conv_4spk.wav"))
        clips = [(t_["speaker"], x[int(t_["start"] * SR):int(t_["end"] * SR)]) for t_ in turns_of("conv_4spk")]
        s = merge([r for (same, _), v in run_splices(tr, "conv_4spk", clips, a.per).items() if same for r in v])
        splits, monos = splits + s["split"], monos + s["mono"]
    if a.real:
        clips = real_clips(a.models_dir)
        sp, n = run_real_alone(tr, clips)
        s = merge([r for (same, _), v in run_splices(tr, "vozes reais", clips, a.per).items() if same for r in v])
        splits, monos = splits + sp + s["split"], monos + n + s["mono"]
    print(f"falso split em tudo que tem 1 locutor: {splits}/{monos} = {splits / monos:.1%} (meta <= {GOAL_FALSE:.0%})")
    med8, miss = latency_8s(tr), []
    if splits / monos > GOAL_FALSE:
        miss.append(f"falso split {splits}/{monos}")
    if d["e_gap"] and (pct(d["e_gap"], 50) > GOAL_MED or pct(d["e_gap"], 90) > GOAL_P90):
        miss.append("erro de fronteira acima de 0,25 s (mediana) / 0,5 s (p90)")
    if med8 > GOAL_MS:
        print(f"AVISO: latência acima de {GOAL_MS} ms (PC com outros processos?)")
    if miss:
        sys.exit("metas não atingidas: " + "; ".join(miss))


if __name__ == "__main__":
    main()
