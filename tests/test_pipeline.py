"""Testes do Pipeline só com stubs (sem modelos, sem áudio). Da raiz do projeto:
  $env:PYTHONPATH=(Get-Location).Path; .venv\\Scripts\\python.exe tests\\test_pipeline.py
Com os modelos reais, em tempo real e contra o gabarito: tests\\e2e_report.py (aqui, `--real [arquivo.wav] [segundos]` é um atalho)."""
import contextlib
import logging
import queue
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np

from app.events import SR, SegEvent, Status, Update
from app.pipeline import EXTRA_ID, Pipeline, _Inbox

CHUNK = np.zeros(512, np.float32)
logging.disable(logging.CRITICAL)  # os erros provocados de propósito não precisam sujar a saída


# ---- stubs ----
class Seg:
    """Segmenter falso: cada feed() devolve o próximo item do roteiro (reset rebobina, como um recomeço)."""
    def __init__(self, script):
        self.script, self.i = script, 0

    def feed(self, chunk, t_end):
        self.i += 1
        return self.script[self.i - 1] if self.i <= len(self.script) else []

    def reset(self):
        self.i = 0


class Asr:
    """ASR falso: dorme `partial_s`/`final_s`; texto = fn(uid, final) ou "u<uid>p|F". O uid vem do áudio. Idioma: o
    fixado, senão `lid(audio)` (padrão: o prior). langs = (language, prior) de cada chamada."""
    def __init__(self, partial_s=0.0, final_s=0.0, fn=None, lid=None):
        self.partial_s, self.final_s, self.fn, self.lid, self.calls, self.langs = partial_s, final_s, fn, lid, [], []

    def _lang(self, audio, language, prior):
        self.langs.append((language, prior))
        return language or (self.lid(audio) if self.lid else prior)

    def transcribe(self, audio, final=False, language=None, prior="en"):
        uid = int(audio[0]) - 1
        self.calls.append((uid, final))
        time.sleep(self.final_s if final else self.partial_s)
        return (self.fn(uid, final) if self.fn else f"u{uid}{'F' if final else 'p'}"), self._lang(audio, language, prior)


class AsrAudio(Asr):
    """ASR falso que decide pelo áudio: texto = fn(audio, final) (pode levantar exceção). calls = (nº de amostras, final)."""
    def transcribe(self, audio, final=False, language=None, prior="en"):
        self.calls.append((len(audio), final))
        time.sleep(self.final_s if final else self.partial_s)
        return self.fn(audio, final), self._lang(audio, language, prior)


class Spk:
    """Locutores falsos. identify: rótulo = 1ª amostra do áudio % 3. segments: o que `split[uid]` mandar
    ([(início_s, fim_s, locutor)]) ou, sem isso, 1 trecho com o rótulo do identify(final=True) (como o contrato)."""
    min_audio_s = 1.0

    def __init__(self, fail_uid=None, unknown=False, split=None, fail_segments=False):
        self.calls, self.seg_calls = [], []  # flags `final` de cada identify; nº de amostras de cada segments
        self.fail_uid, self.unknown, self.split, self.fail_segments = fail_uid, unknown, split or {}, fail_segments

    def identify(self, audio, final):
        self.calls.append(final)
        if int(audio[0]) - 1 == self.fail_uid:
            raise RuntimeError("boom-spk")
        return None if self.unknown else int(audio[0]) % 3

    def segments(self, audio):
        self.seg_calls.append(len(audio))
        if self.fail_segments:
            raise RuntimeError("boom-seg")
        return self.split.get(int(audio[0]) - 1) or [(0.0, len(audio) / SR, self.identify(audio, True))]

    def reset(self):
        pass

    def save(self):
        self.saved = getattr(self, "saved", 0) + 1


class Mt:
    def __init__(self, fail_text=None):
        self.calls, self.fail_text = [], fail_text

    def translate(self, text):
        self.calls.append(text)
        if text == self.fail_text:
            raise RuntimeError("boom-mt")
        return "PT " + text


class Cap:
    def __init__(self, on_audio, name="Stub", on_status=None, app=None):
        self.on_audio, self.device_name, self.on_status, self.app, self.stopped = on_audio, name, on_status, app, False

    def start(self):
        pass

    def stop(self):
        self.stopped = True


# ---- helpers ----
def ev(kind, uid, secs=1.0):
    t = time.monotonic()
    return SegEvent(kind, uid, None if kind == "start" else np.full(int(SR * secs), uid + 1.0, np.float32), t, t)


def utt(uid, partials=3):
    """Roteiro de uma utterance, um feed por evento: start, N parciais (áudio crescente), final."""
    return ([[ev("start", uid)]] + [[ev("partial", uid, 0.5 * (k + 1))] for k in range(partials)]
            + [[ev("final", uid, 0.5 * (partials + 1))]])


def split_ev(uid, secs=(1.5, 2.5)):
    """Final com um trecho (região de valor constante) por duração de `secs`: a região r vale uid+1+0,25r (o ASR e o Spk
    falsos acham uid e região pela 1ª amostra). A fala começa 0,25 s depois do início do áudio (preroll)."""
    audio = np.concatenate([np.full(int(SR * d), uid + 1 + 0.25 * r, np.float32) for r, d in enumerate(secs)])
    t = time.monotonic()
    return SegEvent("final", uid, audio, t - len(audio) / SR + 0.25, t)


def region(audio):
    return round(float(audio[0] % 1) * 4)


def words(audio, final):
    """Texto do AsrAudio: áudio com mais de uma região (o final inteiro) = 'u<uid> todo'; um trecho = 'u<uid> A/B/C'."""
    uid = int(audio[0]) - 1
    return f"u{uid} todo" if audio.min() != audio.max() else f"u{uid} {'ABC'[region(audio)]}"


def make(script, asr, spk=None, mt=None, name="Stub", pad=0.0):
    out, caps = queue.Queue(), []

    def factory(on_audio, device=None, on_status=None, app=None):
        caps.append(Cap(on_audio, app or name, on_status, app))
        return caps[-1]

    p = Pipeline(out, segmenter=Seg(script), transcriber=asr, tracker=spk or Spk(), translator=mt or Mt(),
                 capture_factory=factory, piece_pad_s=pad, log_path=Path(tempfile.mkdtemp()) / "latency.csv")
    return p, caps, out


READY = [Status("Carregando modelos…"), Status("Pronto — escutando Stub", "ready")]


@contextlib.contextmanager
def running(script, asr, spk=None, mt=None, pad=0.0, **start):
    p, caps, out = make(script, asr, spk, mt, pad=pad)
    p.start(**start)
    got = [out.get(timeout=3), out.get(timeout=3)]
    assert got == READY or start.get("app"), got
    try:
        yield p, caps[0], out
    finally:
        p.stop()


def feed(cap, n=1, gap=0.0):
    for _ in range(n):
        cap.on_audio(CHUNK, time.monotonic())
        if gap:
            time.sleep(gap)


def collect(out, done, timeout=5.0):
    """Lê `out` até done(itens) ser verdadeiro."""
    items, end = [], time.monotonic() + timeout
    while not done(items):
        assert time.monotonic() < end, f"timeout; recebido: {items}"
        try:
            items.append(out.get(timeout=0.05))
        except queue.Empty:
            pass
    return items


def n_finals(items):
    return sum(isinstance(u, Update) and u.final for u in items)


def csv_rows(p, n):
    """Espera o worker escrever n linhas de dados no CSV (a escrita vem logo depois do Update)."""
    end = time.monotonic() + 2
    while time.monotonic() < end:
        lines = p._log_path.read_text(encoding="utf-8").splitlines()
        if len(lines) > n:
            return lines
        time.sleep(0.01)
    raise AssertionError(f"CSV incompleto: {lines}")


# ---- testes ----
def test_inbox_policy():
    ib = _Inbox()
    put = lambda kind, uid, n=1: ib.put(SegEvent(kind, uid, np.zeros(n, np.float32), 0.0, 0.0))
    put("partial", 1, 1)
    put("partial", 1, 2)                       # coalesce: fica o mais recente
    assert len(ib) == 1 and ib._partials[1].audio.size == 2
    put("final", 1)                            # final mata o parcial pendente...
    put("partial", 1, 3)                       # ...e barra os que chegarem depois
    assert len(ib) == 1
    put("final", 2)
    put("partial", 3)
    assert [(e.kind, e.utt_id) for e in (ib.get(), ib.get(), ib.get())] == [("final", 1), ("final", 2), ("partial", 3)]
    res = []                                   # get() bloqueia sem polling e acorda com close()
    t = threading.Thread(target=lambda: res.append(ib.get()))
    t.start()
    time.sleep(0.05)
    assert t.is_alive() and not res
    ib.close()
    t.join(1)
    assert res == [None]


def test_burst_all_finals_in_order():
    script = [e for uid in range(20) for e in utt(uid)]
    with running(script, Asr(0.005, 0.015)) as (p, cap, out):
        feed(cap, len(script), gap=0.006)
        items = collect(out, lambda it: n_finals(it) == 20)
        ups = [u for u in items if isinstance(u, Update)]
        finals = [u for u in ups if u.final]
        assert [u.utt_id for u in finals] == list(range(20)), "finais fora de ordem ou faltando"
        assert all(u.orig == f"u{u.utt_id}F" and u.sub == f"PT u{u.utt_id}F" and u.speaker == (u.utt_id + 1) % 3
                   for u in finals)
        done = set()
        for u in ups:                          # nada da utterance depois do seu final
            assert u.utt_id not in done, f"update depois do final {u.utt_id}"
            if u.final:
                done.add(u.utt_id)
        assert any(not u.final for u in ups), "nenhum parcial passou: teste sem graça"
        lines = csv_rows(p, 20)
        assert lines[0] == "utt_id,dur_s,asr_ms,spk_ms,mt_ms,lag_ms"
        rows = [[float(x) for x in ln.split(",")] for ln in lines[1:]]
        assert [int(r[0]) for r in rows] == list(range(20)), lines
        assert all(r[5] >= r[2] >= 5 for r in rows), lines   # lag >= asr >= ~15 ms (sleep do stub)


def test_slow_worker_coalesces_partials():
    n = 24                                     # parcial a cada 100 ms contra ASR de 400 ms
    script = [[ev("partial", 0, 1.0)] for _ in range(n)] + [[ev("final", 0, 3.0)]]
    asr, mt = Asr(0.4, 0.4), Mt()
    with running(script, asr, mt=mt) as (p, cap, out):
        depth, t0 = 0, time.monotonic()
        for _ in range(n):
            feed(cap)
            depth = max(depth, len(p._sess.inbox))
            time.sleep(0.1)
        t_final = time.monotonic()
        feed_s = t_final - t0                  # o sleep(0.1) estica em runner lento (mac do CI): o teto de parciais escala com isso
        feed(cap)
        items = collect(out, lambda it: n_finals(it) == 1)
    ups = [u for u in items if isinstance(u, Update)]
    n_partial = sum(not f for _, f in asr.calls)
    # sem coalescer seriam n; coalescendo, 1 parcial por ~0,4 s de ASR durante a alimentação (+ o em andamento e a folga de 1)
    assert 2 <= n_partial <= min(feed_s / 0.4 + 2, 0.6 * n), f"{n_partial} parciais processados de {n} em {feed_s:.2f}s: sem coalescer?"
    assert depth <= 2, f"fila cresceu: {depth}"
    assert ups[-1].final and ups[-1].t_ready - t_final < 1.5, "final atrasou além do parcial em andamento + 1 job (~0,8 s)"
    assert len(mt.calls) == 2, f"MT deveria pular parciais com EN repetido: {mt.calls}"


def test_empty_final_discards_line():
    # parcial com texto e final vazio: a linha parcial é apagada e nada é traduzido. (Com texto no parcial os locutores
    # são consultados antes do ASR final: ver test_empty_final_without_partial_text_skips_speakers para o ruído puro.)
    script = [[ev("partial", 0, 1.5)], [ev("final", 0, 2.0)]]
    spk, mt = Spk(), Mt()
    with running(script, Asr(fn=lambda uid, final: "" if final else "hello"), spk, mt) as (p, cap, out):
        feed(cap)
        part = out.get(timeout=2)
        feed(cap)
        fin = out.get(timeout=2)
        lines = csv_rows(p, 1)
    assert (part.final, part.orig) == (False, "hello")
    assert (fin.utt_id, fin.final, fin.orig, fin.sub, fin.speaker) == (0, True, "", "", None)
    assert mt.calls == ["hello"], "final vazio não deve traduzir"
    assert len(lines) == 2


def test_empty_final_without_partial_text_skips_speakers():
    # ruído que o Whisper não transcreve (sem texto nem no parcial): descarta sem consultar os locutores (nada de locutor fantasma)
    spk, mt = Spk(), Mt()
    with running([[ev("final", 0, 2.0)]], Asr(fn=lambda uid, final: ""), spk, mt) as (p, cap, out):
        feed(cap)
        fin = out.get(timeout=2)
        time.sleep(0.1)
        assert out.empty()
    assert (fin.utt_id, fin.final, fin.orig, fin.sub, fin.speaker) == (0, True, "", "", None)
    assert not spk.calls and not spk.seg_calls and not mt.calls, "final vazio não deve criar locutor nem traduzir"


def test_label_cache_and_mt_skip():
    # parcial de 0,5 s: sem locutor; 1,5 s: identifica; 2,0 s: reaproveita; final: reidentifica (consolida)
    script = [[ev("partial", 0, 0.5)], [ev("partial", 0, 1.5)], [ev("partial", 0, 2.0)], [ev("final", 0, 2.5)]]
    spk, mt = Spk(), Mt()
    with running(script, Asr(fn=lambda uid, final: "hello"), spk, mt) as (p, cap, out):
        ups = []
        for _ in script:
            feed(cap)
            ups.append(out.get(timeout=2))
    assert [u.speaker for u in ups] == [None, 1, 1, 1], [u.speaker for u in ups]
    assert spk.calls == [False, True], spk.calls
    assert mt.calls == ["hello"] and ups[-1].sub == "PT hello", "EN igual ao parcial: MT deve ser pulada"


def test_unknown_voice_backs_off():
    # voz sem centróide: o parcial devolve None; não pode gastar uma identify por parcial
    secs = [1.0, 1.1, 1.2, 1.6, 2.5]
    spk = Spk(unknown=True)
    with running([[ev("partial", 0, x)] for x in secs], Asr(), spk) as (p, cap, out):
        for _ in secs:
            feed(cap)
            assert out.get(timeout=2).speaker is None
    assert spk.calls == [False] * 3, spk.calls  # tenta em 1,0 s, 1,6 s e 2,5 s (cada uma >= 1,5x a anterior)


def test_errors_do_not_kill_pipeline():
    def fn(uid, final):
        if (uid, final) in [(0, False), (1, True)]:  # parcial da utt 0 e final da utt 1
            raise RuntimeError("boom-asr")
        return f"u{uid}{'F' if final else 'p'}"
    script = [[ev("partial", 0, 1.5)]] + [[ev("final", uid, 2.0)] for uid in range(5)]
    spk, mt = Spk(fail_uid=3), Mt(fail_text="u2F")
    with running(script, Asr(fn=fn), spk, mt) as (p, cap, out):
        feed(cap)                                      # parcial da utt 0: ASR levanta
        errs = collect(out, lambda it: any(getattr(x, "level", "") == "error" for x in it))
        feed(cap, 5)                                   # finais 0..4 em rajada
        items = errs + collect(out, lambda it: n_finals(it) == 5)
        ups = {u.utt_id: u for u in items if isinstance(u, Update) and u.final}
        assert (ups[0].orig, ups[0].sub) == ("u0F", "PT u0F")
        assert (ups[1].orig, ups[1].sub, ups[1].speaker) == ("", "", None), "ASR falhou no final sem parcial: descarta a linha"
        assert (ups[2].orig, ups[2].sub) == ("u2F", ""), "MT falhou: mantém o EN"
        assert (ups[3].orig, ups[3].speaker) == ("u3F", None), "locutor falhou: mantém a legenda"
        assert (ups[4].orig, ups[4].sub) == ("u4F", "PT u4F"), "pipeline vivo depois dos erros"
        texts = [x.text for x in items if isinstance(x, Status) and x.level == "error"]
        assert sum("ASR" in t and "boom-asr" in t for t in texts) == 1, texts   # 2 falhas de ASR, 1 aviso (anti-inundação)
        assert any("tradução" in t for t in texts) and any("locutor" in t for t in texts), texts
        assert any(t.name == "pipe-work" and t.is_alive() for t in threading.enumerate())


def test_split_final_two_speakers():
    # final com 2 vozes: a linha parcial (cinza) vira a 1ª linha final; a 2ª é uma linha nova com utt_id inédito
    spk, mt, fin, asr = Spk(split={0: [(0.0, 1.5, 0), (1.5, 4.0, 2)]}), Mt(), split_ev(0), AsrAudio(fn=words)
    with running([[ev("partial", 0, 2.0)], [fin]], asr, spk, mt) as (p, cap, out):
        feed(cap)
        part = out.get(timeout=2)
        feed(cap)
        a, b = out.get(timeout=2), out.get(timeout=2)
        lines = csv_rows(p, 1)
    assert (part.utt_id, part.final, part.orig) == (0, False, "u0 A")
    assert (a.utt_id, a.final, a.speaker, a.orig, a.sub) == (0, True, 0, "u0 A", "PT u0 A"), "1ª linha troca o parcial"
    assert (b.utt_id, b.final, b.speaker, b.orig, b.sub) == (EXTRA_ID, True, 2, "u0 B", "PT u0 B")
    cut = fin.t_end - len(fin.audio) / SR + 1.5  # t0/t_end de cada trecho: início do áudio + a / + b
    assert abs(a.t0 - fin.t0) < 1e-6 and abs(a.t_end - cut) < 1e-6 and abs(b.t0 - cut) < 1e-6 and abs(b.t_end - fin.t_end) < 1e-6
    assert a.t_ready <= b.t_ready
    assert len(spk.seg_calls) == 1 and spk.calls == [False], "segments() no lugar de identify(final=True)"
    assert mt.calls == ["u0 A", "u0 B"], "EN do 1º trecho = EN do parcial: PT reaproveitado"
    assert len(lines) == 2, "1 linha de CSV por final, não por trecho"
    assert [n / SR for n, final in asr.calls if final] == [1.5, 2.5], "com texto no parcial o ASR do áudio todo nem roda"


def test_single_piece_final_is_the_old_flow():
    # segments() com 1 trecho: rótulo dele (não o do parcial), ASR e MT como antes (MT pulada com EN igual ao parcial)
    spk, mt, asr = Spk(split={0: [(0.0, 2.5, 2)]}), Mt(), Asr(fn=lambda uid, final: "hello")
    with running([[ev("partial", 0, 1.5)], [ev("final", 0, 2.5)]], asr, spk, mt) as (p, cap, out):
        feed(cap)
        part = out.get(timeout=2)
        feed(cap)
        fin = out.get(timeout=2)
        time.sleep(0.1)
        assert out.empty(), "1 trecho = 1 linha"
    assert (part.speaker, part.final) == (1, False)
    assert (fin.utt_id, fin.speaker, fin.orig, fin.sub, fin.final) == (0, 2, "hello", "PT hello", True)
    assert spk.calls == [False] and len(spk.seg_calls) == 1 and mt.calls == ["hello"]
    assert asr.calls == [(0, False), (0, True)]


def test_split_skips_empty_parts_and_keeps_unknown_speaker():
    # trechos A, B, C com rótulos 0, None, 2; o parcial já chutou o locutor 1 (não vale para a linha dividida)
    split = {0: [(0.0, 1.5, 0), (1.5, 2.5, None), (2.5, 4.0, 2)]}
    for empty in (1, 0):                              # trecho vazio no meio / no começo
        fn = lambda a, final, empty=empty: "" if final and a.min() == a.max() and region(a) == empty else words(a, final)
        with running([[ev("partial", 0, 1.5)], [split_ev(0, (1.5, 1.0, 1.5))]], AsrAudio(fn=fn), Spk(split=split)) as (p, cap, out):
            feed(cap)
            assert out.get(timeout=2).speaker == 1
            feed(cap)
            a, b = out.get(timeout=2), out.get(timeout=2)
            time.sleep(0.1)
            assert out.empty()
        want = [(0, 0, "u0 A"), (EXTRA_ID, 2, "u0 C")] if empty == 1 else [(0, None, "u0 B"), (EXTRA_ID, 2, "u0 C")]
        assert [(u.utt_id, u.speaker, u.orig) for u in (a, b)] == want, (empty, a, b)


def test_split_all_parts_empty_discards():
    # todos os trechos vazios (e o áudio todo também, última chance): descarta a linha parcial, no utt_id original
    asr = AsrAudio(fn=lambda a, final: "" if final else "x")
    split = {0: [(0.0, 1.5, 0), (1.5, 4.0, 1)]}
    with running([[ev("partial", 0, 1.5)], [split_ev(0)]], asr, Spk(split=split)) as (p, cap, out):
        feed(cap)
        assert out.get(timeout=2).orig == "x"
        feed(cap)
        d = out.get(timeout=2)
        time.sleep(0.1)
        assert out.empty()
    assert (d.utt_id, d.speaker, d.orig, d.sub, d.final) == (0, None, "", "", True)
    assert [n / SR for n, final in asr.calls if final] == [1.5, 2.5, 4.0], asr.calls


def test_split_falls_back_to_whole_line():
    # todos os trechos vazios, ou ASR que quebra num trecho: o EN do áudio todo vira 1 linha (1 ASR dele), com o rótulo do trecho mais longo
    split = {0: [(0.0, 1.5, 0), (1.5, 4.0, 1)]}
    def boom(a, final):
        raise RuntimeError("boom-asr")
    for partial in (False, True):                      # sem texto no parcial o ASR do áudio todo vem antes dos locutores
        for name, piece in (("vazios", lambda a, final: ""), ("ASR falhou", boom)):
            fn = lambda a, final, piece=piece: words(a, final) if not final or a.min() != a.max() else piece(a, final)
            asr = AsrAudio(fn=fn)
            with running(([[ev("partial", 0, 1.5)]] if partial else []) + [[split_ev(0)]], asr, Spk(split=split)) as (p, cap, out):
                feed(cap, 1 + partial)
                items = collect(out, lambda it: n_finals(it) == 1)
                time.sleep(0.1)
                assert out.empty(), name
            ups = [u for u in items if isinstance(u, Update) and u.final]
            assert [(u.utt_id, u.speaker, u.orig, u.sub) for u in ups] == [(0, 1, "u0 todo", "PT u0 todo")], (name, partial, ups)
            assert sum(n == 4 * SR and final for n, final in asr.calls) == 1, asr.calls
            errs = [x.text for x in items if isinstance(x, Status) and x.level == "error"]
            assert bool(errs) == (name == "ASR falhou") and all("boom-asr" in t for t in errs), (name, errs)


def test_segments_failure_falls_back_to_identify():
    spk = Spk(fail_segments=True)
    with running([[ev("final", 0, 2.0)], [ev("final", 1, 2.0)]], Asr(), spk) as (p, cap, out):
        feed(cap, 2)
        items = collect(out, lambda it: n_finals(it) == 2)
    ups = [u for u in items if isinstance(u, Update)]
    assert [(u.utt_id, u.speaker, u.orig) for u in ups] == [(0, 1, "u0F"), (1, 2, "u1F")], ups  # identify(final=True)
    assert spk.calls == [True, True]
    errs = [x.text for x in items if isinstance(x, Status) and x.level == "error"]
    assert len(errs) == 1 and "locutor" in errs[0] and "boom-seg" in errs[0], errs          # 1 aviso só (anti-inundação)


def test_split_burst_keeps_lines_together():
    n = 6                                              # finais divididos em rajada, com parciais da seguinte no meio
    split = {u: [(0.0, 1.5, 0), (1.5, 4.0, 1)] for u in range(n)}
    script = [e for u in range(n) for e in ([ev("partial", u, 1.5)], [split_ev(u)])]
    with running(script, AsrAudio(0.005, 0.03, fn=words), Spk(split=split)) as (p, cap, out):
        feed(cap, len(script), gap=0.006)
        items = collect(out, lambda it: n_finals(it) == 2 * n)
    ups = [u for u in items if isinstance(u, Update)]
    assert [u.utt_id for u in ups if u.final] == [i for u in range(n) for i in (u, EXTRA_ID + u)], "ids ou ordem"
    pos = [i for i, u in enumerate(ups) if u.final]
    assert all(j == i + 1 for i, j in zip(pos[::2], pos[1::2])), "linhas de um final separadas por outro update"
    assert [u.speaker for u in ups if u.final] == [0, 1] * n


def test_ui_extra_id_matches():
    from app import ui                                 # a UI não importa o pipeline: a constante é duplicada (atraso do final dividido)
    assert ui.EXTRA_ID == EXTRA_ID


def test_piece_pad():
    # folga em volta de cada trecho, cortada nas pontas do áudio: 4 s = [0, 1,5] + [1,5, 4] com 0,5 s de folga
    asr = AsrAudio(fn=words)
    with running([[split_ev(0)]], asr, Spk(split={0: [(0.0, 1.5, 0), (1.5, 4.0, 1)]}), pad=0.5) as (p, cap, out):
        feed(cap)
        collect(out, lambda it: n_finals(it) == 2)
    assert [n / SR for n, final in asr.calls] == [4.0, 2.0, 3.0], asr.calls


def test_stop_with_job_in_flight():
    started, release = threading.Event(), threading.Event()

    class Hang(Asr):
        def transcribe(self, audio, final=False, language=None, prior="en"):
            started.set()
            release.wait(10)
            return "tarde", "en"

    with running([[ev("final", 0, 2.0)]], Hang()) as (p, cap, out):
        feed(cap)
        assert started.wait(2)
        t = time.monotonic()
        p.stop()
        dt = time.monotonic() - t
        assert dt < 2.0 and cap.stopped, f"stop() demorou {dt:.2f}s"
        p.stop()                                       # idempotente
        release.set()                                  # o job termina depois do stop: resultado descartado
        time.sleep(0.3)
        assert out.empty(), "saída depois do stop"
        assert not [t for t in threading.enumerate() if t.name.startswith("pipe-") and t.is_alive()], "thread viva"


def test_same_language_skips_mt():
    # fala no idioma da legenda: sub = orig, sem MT; no outro idioma traduz (o Mt falso serve aos dois pares)
    lid = lambda a: "pt" if int(a[0]) % 2 else "en"                   # uid par fala PT (1ª amostra = uid + 1)
    script = [[ev("partial", 0, 2.0)], [ev("final", 0, 2.0)], [ev("partial", 1, 2.0)], [ev("final", 1, 2.0)]]
    mt = Mt()
    with running(script, Asr(lid=lid), mt=mt, sub_lang="pt") as (p, cap, out):
        ups = []
        for _ in script:
            feed(cap)
            ups.append(out.get(timeout=2))
    assert [(u.utt_id, u.final, u.lang, u.sub) for u in ups] == [
        (0, False, "pt", "u0p"), (0, True, "pt", "u0F"), (1, False, "en", "PT u1p"), (1, True, "en", "PT u1F")], ups
    assert mt.calls == ["u1p", "u1F"], mt.calls
    with running([[ev("final", 0, 2.0)]], Asr(lid=lambda a: "en"), mt=(mt := Mt()), sub_lang="en") as (p, cap, out):
        feed(cap)
        u = out.get(timeout=2)
    assert (u.orig, u.sub, u.lang) == ("u0F", "u0F", "en") and not mt.calls


def test_language_lock_and_prior():
    # call "auto": parcial curto usa o prior da sessão (o outro idioma da legenda); o 1º parcial >= LOCK_S trava o idioma
    # e o resto da utterance (parciais e final) passa language fixo; a utterance seguinte herda o idioma do locutor
    from app.pipeline import LOCK_S
    script = [[ev("partial", 0, 1.0)], [ev("partial", 0, LOCK_S)], [ev("partial", 0, 2.0)], [ev("final", 0, 2.5)],
              [ev("partial", 1, 1.0)]]
    asr = Asr(lid=lambda a: "pt" if len(a) >= LOCK_S * SR else "en")
    with running(script, asr, sub_lang="pt") as (p, cap, out):
        for _ in script:
            feed(cap)
            out.get(timeout=2)
    assert asr.langs == [(None, "en"), (None, "en"), ("pt", "pt"), ("pt", "pt"), (None, "pt")], asr.langs
    asr = Asr(lid=lambda a: "pt")                                     # call fixa: nunca detecta
    with running(script[:2], asr, call_lang="en") as (p, cap, out):
        for _ in range(2):
            feed(cap)
            assert out.get(timeout=2).lang == "en"
    assert all(lang == "en" for lang, _ in asr.langs), asr.langs


def test_speaker_commands_serialized():
    # speaker() espera o job de inferência em andamento (o tracker não é thread-safe); sem tracker, forget vai direto ao arquivo
    started, release, order = threading.Event(), threading.Event(), []

    class Hang(Asr):
        def transcribe(self, audio, final=False, language=None, prior="en"):
            started.set()
            release.wait(5)
            order.append("job")
            return "x", "en"

    class Pins(Spk):
        def pin(self, spk):
            order.append(("pin", spk))

        def merge(self, src, dst):
            order.append(("merge", src, dst))

        def forget(self, spk=None):
            order.append(("forget", spk))

    spk = Pins()
    with running([[ev("final", 0, 2.0)]], Hang(), spk) as (p, cap, out):
        feed(cap)
        assert started.wait(2)
        t = threading.Thread(target=lambda: [p.speaker("pin", 3), p.speaker("merge", 1, 0), p.speaker("forget_all")])
        t.start()
        time.sleep(0.2)
        assert order == [], "speaker() rodou no meio do job"
        release.set()
        t.join(2)
        assert order == ["job", ("pin", 3), ("merge", 1, 0), ("forget", None)], order
    end = time.monotonic() + 2                                        # o worker grava ao sair (sem atrasar o stop())
    while not getattr(spk, "saved", 0) and time.monotonic() < end:
        time.sleep(0.01)
    assert getattr(spk, "saved", 0) == 1, "fim de sessão grava as vozes fixadas"
    prev, fake, got = sys.modules.get("app.diar"), types.ModuleType("app.diar"), []
    fake.forget_saved = lambda path, spk=None: got.append((Path(path).name, spk))
    fake.saved_speakers = lambda path: [4, 9]
    sys.modules["app.diar"] = fake
    try:
        p = Pipeline(queue.Queue())                                   # nada carregado: tracker None
        p.speaker("forget", 4)
        p.speaker("forget_all")
        p.speaker("pin", 1)                                           # sem tracker: ignorado
        assert got == [("speakers.json", 4), ("speakers.json", None)] and p.saved_speakers() == [4, 9], got
    finally:
        sys.modules.pop("app.diar", None)
        if prev is not None:
            sys.modules["app.diar"] = prev


def test_app_capture_and_args():
    with running([], Asr(), app="Discord.exe") as (p, cap, out):
        assert cap.app == "Discord.exe"
    p, caps, out = make([], Asr())
    try:
        for bad in ({"call_lang": "es"}, {"sub_lang": "auto"}):
            try:
                p.start(**bad)
            except AssertionError:
                continue
            raise AssertionError(f"aceitou {bad}")
    finally:
        p.stop()


def test_restart():
    script = utt(0, partials=1)
    p, caps, out = make(script, Asr())
    try:
        for session in range(2):                       # Iniciar -> Parar -> Iniciar: cada sessão com captura nova
            p.start()
            collect(out, lambda it: it[-1:] == [READY[1]])
            feed(caps[session], len(script))
            collect(out, lambda it: n_finals(it) == 1)
            p.stop()
            p.stop()                                   # idempotente
            assert caps[session].stopped
        assert len(caps) == 2
    finally:
        p.stop()


def test_device_name_arrives_late():
    # como a captura real: device_name vazio logo após start() e preenchido ~0,1 s depois
    p, caps, out = make([], Asr(), name="")
    try:
        p.start()
        assert collect(out, lambda it: len(it) == 1) == [READY[0]]
        time.sleep(0.1)
        assert out.empty(), "'Pronto' sem saber o dispositivo"
        cap = caps[0]
        cap.on_status("Dispositivo 'X' não encontrado — tentando de novo…")
        assert out.get(timeout=2) == Status("Dispositivo 'X' não encontrado — tentando de novo…", "warn")
        cap.device_name = "Fone"
        cap.on_audio(CHUNK, time.monotonic())          # o próximo chunk percebe o nome
        assert out.get(timeout=2) == Status("Pronto — escutando Fone", "ready")
        cap.on_status("Capturando áudio de: Fone")     # (re)abriu: volta a "Pronto"
        assert out.get(timeout=2) == Status("Pronto — escutando Fone", "ready")
    finally:
        p.stop()


def test_capture_open_failure():
    p, caps, out = make([], Asr())

    def boom(*a, **k):
        raise OSError("sem saída de áudio")
    p._capture_factory = boom
    p.start()
    items = collect(out, lambda it: len(it) == 2)
    assert items[1].level == "error" and "captura" in items[1].text and "sem saída de áudio" in items[1].text, items
    time.sleep(0.2)
    assert not [t for t in threading.enumerate() if t.name.startswith("pipe-") and t.is_alive()], "threads sobraram"


def test_load_failure_and_stop_during_load():
    prev, fake = sys.modules.get("app.asr"), types.ModuleType("app.asr")
    sys.modules["app.asr"] = fake                      # carga real do Whisper trocada por um módulo falso
    loads = []
    try:
        def boom(**kw):
            raise FileNotFoundError("modelo X")
        fake.Transcriber = boom
        p, caps, out = make([], None)
        p.start()
        items = collect(out, lambda it: len(it) == 2)
        assert items[0] == Status("Carregando modelos…") and items[1].level == "error", items
        assert "fala" in items[1].text and "modelo X" in items[1].text and "setup.ps1" in items[1].text
        assert not caps, "não abre captura se a carga falhou"

        class Slow:
            def __init__(self, **kw):
                loads.append(1)
                time.sleep(0.8)

            def warmup(self):
                pass
        fake.Transcriber = Slow
        p, caps, out = make([], None)
        p.start()
        time.sleep(0.1)
        t = time.monotonic()
        p.stop()                                       # no meio da carga: volta já, sem esperar
        assert time.monotonic() - t < 0.5
        p.start()                                      # ...e iniciar de novo não recarrega: espera a carga em curso
        items = collect(out, lambda it: it[-1:] == [READY[1]], timeout=5)
        assert len(loads) == 1 and len(caps) == 1, (loads, caps)
        assert [x for x in items if isinstance(x, Status)].count(READY[1]) == 1
        p.stop()
    finally:
        sys.modules.pop("app.asr", None)
        if prev is not None:
            sys.modules["app.asr"] = prev


if __name__ == "__main__":
    if "--real" in sys.argv:  # modelos reais em tempo real: o relatório completo está em tests\e2e_report.py
        args = sys.argv[sys.argv.index("--real") + 1:][:2]  # [arquivo.wav] [segundos], como antes
        sys.argv = ["e2e_report.py", *[a.removesuffix(".wav") for a in args[:1]], *(["--secs", args[1]] if args[1:] else [])]
        logging.disable(logging.NOTSET)
        import e2e_report
        e2e_report.main()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            t0 = time.monotonic()
            fn()
            print(f"ok   {name} ({time.monotonic() - t0:.2f}s)")
    print("todos os testes passaram")
