"""Relatório ponta a ponta: toca os fixtures em TEMPO REAL (sem som: o áudio entra na captura falsa) pelo Pipeline REAL
(Whisper, locutores, tradução) e compara a saída com o gabarito de tests\\data\\<nome>.json.

Da raiz do projeto:
  $env:PYTHONPATH=(Get-Location).Path; .venv\\Scripts\\python.exe tests\\e2e_report.py [nomes...] [opções]
  nomes: conv_2spk conv_4spk conv_2spk_noisy (padrão: os três, ~6 min em tempo real)
  --secs N   só até o último turno que termina antes de N s (teste curto)
  --fast     não espera o relógio: locutor/WER/linhas valem, atrasos não (varredura rápida de calibração)
  --kw seg|diar|asr|pipe.chave=valor   calibração sobre os padrões de app\\main.py (repetível), ex.: --kw diar.threshold=0.35
                                       (pipe = kwargs do Pipeline, ex.: pipe.piece_pad_s=0.1)
  -v         lista todas as linhas finais
Métricas (por fixture):
  locutor  acurácia ponderada por tempo (segundos de fala do gabarito com o rótulo certo; melhor mapeamento 1-1
           rótulo -> locutor; "?" conta como erro) a partir de t0/t_end das linhas finais
  WER      EN de todas as linhas finais, em ordem, contra o texto de todos os turnos (normalização do bench_asr)
  atraso   final = fim da fala -> legenda pronta (t_ready - t_end da última linha de cada final); parcial idem
Sai com código 1 se faltar alguma meta."""
import argparse
import json
import queue
import sys
import tempfile
import threading
import time
import wave
from collections import Counter
from pathlib import Path

import numpy as np

from app.events import SR, Update
from app.main import ASR_KW, DIAR_KW, SEG_KW
from app.pipeline import EXTRA_ID, Pipeline
from bench_asr import edits, norm   # WER: a mesma normalização do benchmark do ASR
from eval_split import best_total   # melhor mapeamento 1-1 rótulo -> locutor

DATA = Path(__file__).parent / "data"
LOCUTOR_MIN = {"conv_2spk": 95, "conv_4spk": 90, "conv_2spk_noisy": 95}  # % de acurácia de locutor
WER_MAX, FINAL_MAX, PARCIAL_MAX = 3.0, 500, 300                         # % ; ms (mediana)
IDLE_S = 2.5  # sem saída por tanto tempo depois do fim do áudio = terminou


class Replay:
    """No lugar do LoopbackCapture: chunks de 32 ms na velocidade real (t_end = monotonic() na entrega, como a captura real)
    ou, em --fast, sem esperar e com t_end sintético. `origin` = monotonic em t = 0 do áudio."""
    device_name = "replay"

    def __init__(self, pcm, fast):
        self.pcm, self.fast, self.on, self.done, self.origin = pcm, fast, True, threading.Event(), None

    def __call__(self, on_audio, device=None, on_status=None):  # capture_factory do Pipeline
        self.on_audio = on_audio
        return self

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        p0, self.origin = time.perf_counter(), time.monotonic()
        for k in range(len(self.pcm) // 512):
            t = (k + 1) * 512 / SR
            while self.on and not self.fast and (d := p0 + t - time.perf_counter()) > 0:
                time.sleep(min(d, 0.005))
            if not self.on:
                return
            self.on_audio(self.pcm[k * 512:(k + 1) * 512], self.origin + t if self.fast else time.monotonic())
        self.done.set()

    def stop(self):
        self.on = False


def load(name, secs):
    with wave.open(str(DATA / f"{name}.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2), name
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    turns = json.loads((DATA / f"{name}.json").read_text("utf-8"))["turns"]
    if secs:
        turns = [t for t in turns if t["end"] <= secs]
        pcm = pcm[:int((turns[-1]["end"] + 0.5) * SR)]
    return np.concatenate([pcm, np.zeros(3 * SR, np.float32)]), turns  # 3 s de silêncio fecham a última frase


def play(p, out, name, secs, fast):
    """Uma sessão do Pipeline sobre o fixture. Devolve (updates, erros, turnos, origem do relógio, linhas do latency.csv)."""
    pcm, turns = load(name, secs)
    rep = p._capture_factory = Replay(pcm, fast)  # os modelos já carregados ficam no Pipeline entre as sessões
    n0 = len(p._log_path.read_text("utf-8").splitlines()) if p._log_path.exists() else 1
    ups, errors, t_last = [], [], time.monotonic()
    p.start()
    t_max = t_last + len(pcm) / SR * (0.2 if fast else 1) + 180  # carga dos modelos + folga
    while not (rep.done.is_set() and time.monotonic() - t_last > IDLE_S):
        assert time.monotonic() < t_max, f"{name}: não terminou (erros: {errors})"
        try:
            x = out.get(timeout=0.2)
        except queue.Empty:
            continue
        t_last = time.monotonic()
        if isinstance(x, Update):
            ups.append(x)
        elif x.level == "error":
            errors.append(x.text)
            if x.text.startswith(("Falha", "Erro ao iniciar")):
                p.stop()
                sys.exit(x.text)
    p.stop()
    rows = [ln.split(",") for ln in p._log_path.read_text("utf-8").splitlines()[n0:]]
    return ups, errors, turns, rep.origin, rows


def speaker_acc(finals, turns, origin):
    """% do tempo de fala do gabarito com o locutor certo (melhor mapeamento 1-1 rótulo -> locutor)."""
    cnt = Counter()
    for u in finals:
        s, e = u.t0 - origin, u.t_end - origin
        for t in turns:
            cnt[(u.speaker, t["speaker"])] += max(0.0, min(e, t["end"]) - max(s, t["start"]))
    return 100 * best_total(cnt) / sum(t["end"] - t["start"] for t in turns)


def wer(finals, turns):
    ref = [w for t in turns for w in norm(t["text"])]
    return 100 * edits(ref, [w for u in finals for w in norm(u.en)]) / len(ref)


def stat(v):
    return f"mediana {np.median(v):4.0f} | p90 {np.percentile(v, 90):4.0f} | máx {max(v):4.0f} ms" if len(v) else "-"


def report(name, ups, errors, turns, origin, rows, fast, verbose):
    finals = [u for u in ups if u.final and u.en]
    jobs = []  # as linhas de um final dividido saem juntas; só a 1ª leva o utt_id do Segmenter
    for u in finals:
        jobs.append([u]) if u.utt_id < EXTRA_ID or not jobs else jobs[-1].append(u)
    acc, w = speaker_acc(finals, turns, origin), wer(finals, turns)
    lag_f = [(j[-1].t_ready - j[-1].t_end) * 1e3 for j in jobs]
    lag_p = [(u.t_ready - u.t_end) * 1e3 for u in ups if not u.final]
    ok = {"locutor": acc >= LOCUTOR_MIN[name], "WER": w <= WER_MAX, "erros": not errors}
    if not fast:
        ok |= {"atraso final": np.median(lag_f) <= FINAL_MAX, "atraso parcial": np.median(lag_p) <= PARCIAL_MAX}
    flag = lambda k: "ok" if ok.get(k, True) else "FALHOU"  # noqa: E731
    print(f"\n== {name}: {len(turns)} turnos, {len(finals)} linhas finais ({sum(len(j) > 1 for j in jobs)} de {len(jobs)} "
          f"finais divididos por locutor), {sum(u.final and not u.en for u in ups)} descartes, {sum(not u.final for u in ups)} parciais")
    print(f"locutor (por tempo) {acc:5.1f} %   meta >= {LOCUTOR_MIN[name]}   {flag('locutor')}")
    print(f"WER do EN           {w:5.1f} %   meta <= {WER_MAX:g}   {flag('WER')}")
    if not fast:
        print(f"atraso do final     {stat(lag_f)}   meta mediana <= {FINAL_MAX}   {flag('atraso final')}")
        lag_s = [(j[-1].t_ready - j[-1].t_end) * 1e3 for j in jobs if len(j) > 1]
        print(f"  dos divididos     {stat(lag_s)}   (os de 1 locutor: {stat([x for x, j in zip(lag_f, jobs) if len(j) == 1])})")
        print(f"atraso do parcial   {stat(lag_p)}   meta mediana <= {PARCIAL_MAX}   {flag('atraso parcial')}")
        if rows:
            print("por final (mediana, ms): " + ", ".join(f"{n} {np.median([float(r[i]) for r in rows]):.0f}"
                                                         for i, n in ((2, "asr"), (3, "locutor"), (4, "mt"))))
    for e in errors:
        print("ERRO:", e)
    split = [u for j in jobs if len(j) > 1 for u in j]
    show = finals if verbose else []  # exemplos: 3 de finais divididos (+) e o resto espaçado, sem repetir
    for u in ([] if verbose else split[:3] + finals[::max(1, len(finals) // 6)]):
        if u not in show and len(show) < 6:
            show.append(u)
    for u in show:
        who = "?" if u.speaker is None else u.speaker + 1
        print(f"  Locutor {who}{'+' if u in split else ' '}| {u.en} => {u.pt}")
    med = lambda v: np.median(v) if v and not fast else float("nan")  # noqa: E731
    return acc, w, med(lag_f), med(lag_p), all(ok.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", default=list(LOCUTOR_MIN))
    ap.add_argument("--secs", type=float)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--kw", action="append", default=[])
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")
    kw = {"seg": dict(SEG_KW), "diar": dict(DIAR_KW), "asr": dict(ASR_KW), "pipe": {}}
    for s in a.kw:
        k, v = s.split("=", 1)
        kw[k.split(".")[0]][k.split(".", 1)[1]] = json.loads(v)
    print(f"calibração: {kw}" + ("  (--fast: atrasos não medidos)" if a.fast else ""))
    out = queue.Queue()
    p = Pipeline(out, seg_kw=kw["seg"], asr_kw=kw["asr"], diar_kw=kw["diar"], **kw["pipe"],
                 log_path=Path(tempfile.mkdtemp()) / "latency.csv")
    res = {n: report(n, *play(p, out, n, a.secs, a.fast), a.fast, a.v) for n in a.names}
    print("\n== resumo\nfixture            locutor %    WER %   atraso final (ms)   atraso parcial (ms)   metas")
    for n, (acc, w, lf, lp, ok) in res.items():
        print(f"{n:18s} {acc:8.1f} {w:8.1f} {lf:12.0f} {lp:17.0f}   {'ok' if ok else 'FALHOU'}".replace("nan", "  -"))
    sys.exit(0 if all(r[-1] for r in res.values()) else 1)


if __name__ == "__main__":
    main()
