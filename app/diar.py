"""Locutores online: embedding de voz (sherpa-onnx, CPU) + centróides por similaridade de cosseno.

Como funciona
- Cada trecho vira um embedding L2-normalizado (TitaNet-small, 192 dims); compara-se com os centróides (média dos
  embeddings ponderada pela duração, memória ~CAP_S s). Sem fusão automática (testada: piora até -57 pts).
- final=True com áudio >= min_audio_s: sim >= threshold -> atribui e atualiza o centróide; senão cria locutor
  PROVISÓRIO: sem id (None = "?") até somar confirm_s de fala; aí ganha o próximo id (0, 1, ... na ordem de
  confirmação, nunca reaproveitado na sessão). Corta os fantasmas (fala solta que não casa): na AMI com canal de
  call, rótulos por sessão 8,0 -> 6,0, fantasmas 17 -> 10. Lotado (max_speakers sem contar os fixados): descarta
  o provisório mais antigo; sem provisório -> None.
- Palpite (final=False) e áudio curto (< min_audio_s): NUNCA cria nem atualiza. Só responde se
  sim >= threshold - SLACK e sim - (2º colocado) >= MARGIN (em trecho curto é a folga sobre o 2º que separa bem,
  não o valor absoluto); senão None = "?" na UI. Abaixo de MIN_S de áudio (ou silêncio): None direto.
- Janela do embedding: no máx. MAX_S s do miolo do trecho (limita a latência; mais áudio quase não ajuda).

Calibrar `threshold` (a escala de cosseno depende do modelo; TitaNet-small: 0,40 = centro do melhor platô,
0,35-0,45 em fala limpa; fora disso cai rápido):
  1. python tests/eval_diar.py --sweep --real [--pad] [--harsh]  -> acurácia x threshold (TTS + vozes humanas).
  2. Voz real em call (Discord/Opus, microfone ruim, vozes parecidas) separa MENOS que TTS e o canal pesa:
     a mesma pessoa em canais diferentes dá sim < threshold. Se aparecem locutores fantasmas (a mesma pessoa com
     2 rótulos) -> BAIXE o threshold; se duas pessoas viraram uma só (vozes parecidas, áudio ruim) -> SUBA.
     Passos de 0,05. Trocou de mic/canal no meio da call? reset().

Perfis (opt-in, só locais: voz é dado biométrico, LGPD art. 5º, II) — comandos da UI, na thread do identify:
- pin(id) fixa a voz e grava já em profiles_path (speakers.json: {"model", "profiles": [{"id", "emb", "secs"}]});
  reset() mantém os fixados com os mesmos ids (ids novos começam depois do maior fixado) e um tracker novo os lê:
  a mesma voz volta com o mesmo id já na 1ª fala >= min_audio_s. save() regrava com o centróide atual (o pipeline
  chama no fim da sessão). merge(src, dst) junta os centróides (src some; a fixação de src passa para dst).
  forget(id|None) tira do arquivo (sem fixados o arquivo é apagado). Outro MODEL no arquivo = perfis ignorados.
- Perfis dão continuidade (nome/id entre sessões), não acurácia; trocar de headset entre dias atrapalha.

ERes2Net (3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx, 26 MB, mesmo release): avaliado e NÃO adotado.
  Receita testada: MAX_S=3, MIN_PIECE=0.7 (sem isso, falso split 3,4 % nas vozes reais), threshold 0,35. Resolve
  a pessoa partida em call (AMI call: acc 87,5 -> 91,7 %, partidas 7 -> 3; 0 com MAX_S=4), mas junta as vozes
  B/D do conv_4spk (68,6 % por turno, 78,8 % por tempo, contra 80,0 % / 95,9 % do TitaNet) e segments() de 7-9 s
  leva 93 ms com THREADS=2 (74 ms com 4) contra 48 ms. Para trocar: MODEL, MAX_S, MIN_PIECE, THREADS e threshold.

Troca de locutor dentro da utterance: segments(audio), só para utterances FINAIS
- Devolve [(início_s, fim_s, locutor)] relativos ao começo de `audio`, em ordem, ladrilhando [0, duração]; trechos
  vizinhos têm locutores diferentes. Utterance de 1 locutor -> exatamente 1 trecho, rotulado pelo mesmo critério e
  centróides do identify(audio, True) (idêntico a ele quando não há candidato a corte). Locutor None = voz diferente
  da vizinha mas sem identidade ainda (trecho curto, provisório, tabela cheia): mostrar "?" e não herdar o rótulo do
  vizinho.
- Uso no pipeline, no lugar de identify(ev.audio, True) no final (o parcial segue com identify(..., False)):
      segs = tracker.segments(ev.audio)
      len(segs) == 1: spk = segs[0][2] e o fluxo de hoje (transcreve o áudio inteiro);
      senão, para cada (a, b, spk): transcribe(ev.audio[int(a*SR):int(b*SR)], final=True) e 1 Update por trecho, na
      ordem, com t0/t_end = (ev.t_end - len(ev.audio)/SR) + a / + b. O 1º trecho reaproveita o utt_id (troca a linha
      parcial); os demais precisam de utt_id inédito (a UI abre 1 linha por utt_id, na ordem de chegada).
  Custo (CPU, PC com outros programas rodando): ~50 ms em utterance de 8 s, até ~75 ms quando divide, ~70 ms em
  15 s. Roda em threads curtas (POOL) e dá para sobrepor ao ASR final (a GPU é do Whisper), re-transcrevendo só se
  vier > 1 trecho. O estado muda como em identify(final=True): 1 chamada por utterance, na ordem, na mesma thread
  que chama identify (não é thread-safe).
- Como acha as trocas: (1) pyannote-segmentation-3.0 (ONNX, CPU) dá, por quadro de ~17 ms, silêncio / locutor local /
  sobreposição; corta onde o locutor local muda e em pausas; (2) quedas de energia >= PAUSE_S também cortam (o modelo
  liga pausas curtas entre vozes diferentes: perde ~20-30% das trocas); (3) cada trecho >= MIN_PIECE vira embedding;
  o menor cola no vizinho (mesmo locutor local, senão o mais próximo); (4) vizinhos se fundem se casam com o mesmo
  locutor já conhecido (palpite do identify) ou, sem isso, se sim >= _need(duração); só o embedding decide (o id local
  do modelo erra: ~25% de trocas falsas em voz real); (5) cada trecho recebe o rótulo pela regra do identify.
- Calibração: o `threshold` vale também aqui. Alto demais (>= 0,45) divide a mesma voz em trechos curtos; baixo demais
  (<= 0,35) junta vozes parecidas. Canal ruim/Opus baixo separa menos: subir SHORT_K funde mais. python
  tests/eval_split.py --hard --real [--harsh] [--set CONST=v] mede (falso split, fronteira, latência).
- Limites: troca sem pausa que o modelo não enxerga (mesmo id local) não é cortada; sobreposição vira corte no meio
  dela; trecho de uma voz < MIN_PIECE (um "yeah" solto) cola no vizinho; vozes parecidas (sim alta) não se separam
  nem na identidade; locutor novo de trecho < min_audio_s sai None; não testado em áudio > ~35 s (o Segmenter dá <= 15 s).
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import onnxruntime as ort
import sherpa_onnx

from app.events import SR

MODEL = "nemo_en_titanet_small.onnx"
URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
SEG_MODEL = "pyannote-segmentation-3-0.onnx"  # model.onnx (6 MB) do tar.bz2 abaixo
SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
           "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
SEG_MEMBER = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
THREADS = 2
MIN_S = 0.4          # abaixo disso não há embedding utilizável -> None (a 0,4 s a regra ainda acerta ~99% dos palpites)
MAX_S = 6.0          # janela máxima enviada ao modelo (latência)
SLACK = 0.10         # palpite aceita sim >= threshold - SLACK ...
MARGIN = 0.10        # ... se a folga sobre o 2º colocado for >= MARGIN
CAP_S = 60.0         # memória do centróide (s de fala acumulada); evita congelar em call longa
MIN_RMS = 1e-3       # abaixo disso é silêncio digital -> None
# segments(): troca de locutor dentro da utterance
FR, OFF = 270 / SR, 991 / 2 / SR  # passo e centro do 1º campo receptivo dos quadros do modelo de segmentação (s)
TICK = SR // 100     # grade de 10 ms
PAUSE_S = 0.12       # pausa (silêncio do modelo ou queda de energia) mínima que vira corte
DIP_DB = 30.0        # queda de energia (dB abaixo do nível de fala, p90) que conta como pausa
MIN_PIECE = 0.5      # trecho de fala mais curto que isso não tem embedding nem fica sozinho
SHORT_K = 0.2        # trecho curto (embedding ruidoso) se funde ao vizinho com sim >= threshold - SHORT_K*(1 - dur_s)
POOL = 4             # embeddings dos trechos em paralelo (o sherpa-onnx libera o GIL e é thread-safe)


def _fetch(url: str, path: str, member: str | None = None, on_progress=None) -> None:
    """Baixa url para path (atômico). member: arquivo dentro de um tar.bz2 (só ele é extraído)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"  # só vira o arquivo final se vier completo
    with urllib.request.urlopen(url, timeout=30) as r, open(tmp, "wb") as f:
        size = int(r.headers.get("Content-Length") or -1)
        while chunk := r.read(1 << 18):
            f.write(chunk)
            if on_progress and size > 0:
                on_progress(min(1.0, f.tell() / size))
        if on_progress and size <= 0:
            on_progress(None)
    if size >= 0 and os.path.getsize(tmp) != size:
        raise OSError(f"download incompleto: {url}")
    if member:  # só o modelo sai do tar.bz2
        with tarfile.open(tmp, "r:bz2") as t, t.extractfile(member) as src, open(path + ".tmp", "wb") as f:
            shutil.copyfileobj(src, f)
        os.remove(tmp)
        tmp = path + ".tmp"
    os.replace(tmp, path)


def ensure_model(models_dir: str = "models", on_progress=None) -> str:
    """Baixa os modelos de embedding e de segmentação para <models_dir>/spk (idempotente); devolve o do embedding.
    Levanta se a rede falhar."""
    path = os.path.join(models_dir, "spk", MODEL)
    if not os.path.isfile(path):
        _fetch(URL + MODEL, path, on_progress=on_progress)
    seg = os.path.join(models_dir, "spk", SEG_MODEL)
    if not os.path.isfile(seg):
        _fetch(SEG_URL, seg, SEG_MEMBER, on_progress)
    return path


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


@dataclass
class _Piece:
    """Trechos de fala vizinhos já fundidos: [s, e] em s; v = soma dos embeddings (peso = duração; None = sem);
    w = fala em s; il/ir = locutor local do modelo nas pontas; n = nº de trechos originais."""
    s: float
    e: float
    v: np.ndarray | None
    w: float
    il: int
    ir: int
    n: int = 1

    def join(self, o: _Piece) -> None:
        self.e, self.w, self.ir, self.n = o.e, self.w + o.w, o.ir, self.n + o.n
        self.v = o.v if self.v is None else self.v if o.v is None else self.v + o.v


def _load(path) -> list[dict]:
    """Perfis do speakers.json; [] se não existe, se está corrompido ou se foi gravado com outro modelo."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return [p for p in d["profiles"] if {"id", "emb", "secs"} <= p.keys()] if d.get("model") == MODEL else []
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def _write(path, profiles: list[dict]) -> None:
    """Grava os perfis (atômico); lista vazia apaga o arquivo (voz é dado biométrico: nada fica para trás)."""
    if not profiles:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(f"{path}.tmp", "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "profiles": profiles}, f)
    os.replace(f"{path}.tmp", path)


def saved_speakers(path) -> list[int]:
    """Ids das vozes salvas (só lê o json)."""
    return [int(p["id"]) for p in _load(path)]


def forget_saved(path, spk: int | None = None) -> None:
    """Tira a voz `spk` do arquivo (None = todas: apaga o arquivo)."""
    _write(path, [] if spk is None else [p for p in _load(path) if p["id"] != spk])


class SpeakerTracker:
    def __init__(self, *, threshold: float = 0.40, min_audio_s: float = 1.0, max_speakers: int = 8,
                 confirm_s: float = 3.0, models_dir: str = "models", profiles_path: str | None = None, on_progress=None):
        self.threshold, self.min_audio_s, self.max_speakers = threshold, min_audio_s, max_speakers
        self.confirm_s, self.profiles_path = confirm_s, profiles_path
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=ensure_model(models_dir, on_progress), num_threads=THREADS)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self._embed((np.random.default_rng(0).standard_normal(SR) * 0.05).astype(np.float32))  # aquece
        o = ort.SessionOptions()
        o.intra_op_num_threads, o.inter_op_num_threads, o.log_severity_level = THREADS, 1, 4
        self._seg = ort.InferenceSession(os.path.join(models_dir, "spk", SEG_MODEL), o, providers=["CPUExecutionProvider"])
        self._seg.run(None, {"x": np.zeros((1, 1, SR), np.float32)})  # aquece
        self._pool = ThreadPoolExecutor(POOL)
        # tabela de locutores, listas paralelas (índice interno != id exibido):
        self._sum: list[np.ndarray] = []  # soma dos embeddings ponderada pela duração
        self._w: list[float] = []         # peso dessa soma (s; esquece o excedente de CAP_S)
        self._tot: list[float] = []       # fala total (s), para confirmar o provisório
        self._lab: list[int | None] = []  # id exibido; None = provisório (sem id até somar confirm_s)
        self._pin: set[int] = set()       # ids fixados (vão para o arquivo; sobrevivem ao reset)
        for p in _load(profiles_path) if profiles_path else []:
            if len(p["emb"]) != self._ex.dim:  # arquivo mexido à mão: ignora o perfil em vez de quebrar a sessão
                continue
            s = max(float(p["secs"]), self.min_audio_s)
            self._sum.append(_unit(np.asarray(p["emb"], np.float32)) * s)
            self._w.append(s)
            self._tot.append(s)
            self._lab.append(int(p["id"]))
            self._pin.add(int(p["id"]))
        self.reset()

    def reset(self) -> None:
        """Sessão nova: só os fixados continuam (mesmos ids); ids novos começam depois do maior fixado."""
        keep = [j for j, k in enumerate(self._lab) if k in self._pin]
        for L in (self._sum, self._w, self._tot, self._lab):
            L[:] = [L[j] for j in keep]
        self._next = max(self._pin, default=-1) + 1

    # ---- perfis (comandos da UI; mesma thread do identify) ----
    def _drop(self, j: int) -> None:
        for L in (self._sum, self._w, self._tot, self._lab):
            L.pop(j)

    def pin(self, spk: int) -> bool:
        """Fixa a voz `spk` (lembrar entre sessões) e grava já. False se o id não existe."""
        if spk is None or spk not in self._lab:
            return False
        self._pin.add(spk)
        self.save()
        return True

    def merge(self, src: int, dst: int) -> None:
        """Junta src em dst (centróide ponderado pela duração); src some para sempre (id não é reaproveitado)."""
        if src == dst or src not in self._lab or dst not in self._lab:
            return
        i, j = self._lab.index(src), self._lab.index(dst)
        self._sum[j] = self._sum[j] + self._sum[i]
        self._w[j] += self._w[i]
        self._tot[j] += self._tot[i]
        if src in self._pin:  # quem fixou src quer lembrar essa voz: passa para dst
            self._pin.discard(src)
            self._pin.add(dst)
        self._drop(i)
        if dst in self._pin:
            self.save()

    def forget(self, spk: int | None = None) -> None:
        """Esquece a voz fixada `spk` (None = todas): sai do arquivo; na sessão vira um locutor comum (some no reset)."""
        if spk is None:
            self._pin.clear()
        else:
            self._pin.discard(spk)
        if self.profiles_path:
            forget_saved(self.profiles_path, spk)

    def save(self) -> None:
        """Grava os fixados com o centróide atual (sem profiles_path: nada)."""
        if self.profiles_path:
            _write(self.profiles_path, [{"id": k, "emb": [round(float(x), 5) for x in _unit(self._sum[j])],
                                         "secs": round(self._w[j], 2)} for j, k in enumerate(self._lab) if k in self._pin])

    def _embed(self, audio: np.ndarray) -> np.ndarray | None:
        """Embedding L2-normalizado do miolo (<= MAX_S) do áudio; None se inválido."""
        n, m = len(audio), int(MAX_S * SR)
        if n > m:
            audio = audio[(n - m) // 2:(n - m) // 2 + m]
        s = self._ex.create_stream()
        s.accept_waveform(SR, audio)
        s.input_finished()
        if not self._ex.is_ready(s):
            return None
        e = np.asarray(self._ex.compute(s), np.float32)
        nrm = float(np.linalg.norm(e))
        return e / nrm if np.isfinite(nrm) and nrm > 1e-6 else None

    def identify(self, audio: np.ndarray, final: bool) -> int | None:
        d = len(audio) / SR
        if d < MIN_S or float(np.sqrt(np.mean(np.square(audio)))) < MIN_RMS:
            return None
        e = self._embed(audio)
        return None if e is None else self._assign(e, d, final and d >= self.min_audio_s)

    def _match(self, e: np.ndarray) -> tuple[int | None, float, float]:
        """(índice, similaridade, folga sobre o 2º) do centróide mais próximo; (None, 0, 0) sem centróides."""
        if not self._sum:
            return None, 0.0, 0.0
        C = np.stack(self._sum)
        sims = (C / np.linalg.norm(C, axis=1, keepdims=True)) @ e
        j = int(sims.argmax())
        return j, float(sims[j]), float(sims[j] - (np.partition(sims, -2)[-2] if len(sims) > 1 else 0.0))

    def _guess(self, e: np.ndarray) -> int | None:
        """Palpite sem efeito colateral: o locutor confirmado (índice), se casar com folga (regra do identify parcial)."""
        j, s, gap = self._match(e)
        return j if j is not None and s >= self.threshold - SLACK and gap >= MARGIN and self._lab[j] is not None else None

    def _assign(self, e: np.ndarray, d: float, full: bool) -> int | None:
        """Id exibido do embedding e (d s de áudio). full: pode criar locutor / atualizar centróide; senão só palpita.
        Locutor novo nasce provisório (None) e ganha id ao somar confirm_s de fala."""
        dw = min(d, MAX_S)                      # peso = áudio realmente embutido
        j, s, gap = self._match(e)
        if j is not None and (s >= self.threshold if full else (s >= self.threshold - SLACK and gap >= MARGIN)):
            if full:
                k = min(1.0, CAP_S / self._w[j])  # esquece o excedente antigo (memória limitada)
                self._sum[j] = self._sum[j] * k + e * dw
                self._w[j] = self._w[j] * k + dw
                self._tot[j] += d
        elif not full:
            return None
        else:
            # ponytail: sem fusão automática (testada: piora); lotado => descarta o provisório mais antigo, senão None
            if len(self._lab) - len(self._pin) >= self.max_speakers:
                if None not in self._lab:
                    return None
                self._drop(self._lab.index(None))
            self._sum.append(e * dw)
            self._w.append(dw)
            self._tot.append(d)
            self._lab.append(None)
            j = len(self._lab) - 1
        if full and self._lab[j] is None and self._tot[j] >= self.confirm_s:
            self._lab[j], self._next = self._next, self._next + 1
        return self._lab[j]

    # ---- troca de locutor dentro da utterance ----
    def _atoms(self, audio: np.ndarray) -> list[tuple[float, float, int]]:
        """[(início_s, fim_s, locutor local)] dos trechos de fala, cortados onde o modelo troca de locutor local (mesmo sem
        pausa) e em pausas >= PAUSE_S (silêncio/sobreposição do modelo ou queda de energia). Pontas silenciosas ficam fora."""
        # ponytail: troca sem pausa que o modelo não enxerga (mesmo id local) não vira corte; upgrade: janelas de embedding
        # (~6 ms por s de áudio) nos trechos longos
        n = len(audio) // TICK
        x = audio[:n * TICK].reshape(n, TICK)
        db = 10 * np.log10(np.mean(x * x, axis=1) + 1e-10)  # dBFS a cada 10 ms
        y, = self._seg.run(None, {"x": audio[None, None]})
        c = y[0].argmax(-1)                                  # 0 = silêncio, 1-3 = locutor local, 4-6 = pares
        i = np.clip(((np.arange(n) + 0.5) / 100 - OFF) / FR + 0.5, 0, len(c) - 1).astype(int)
        own = np.where((c[i] >= 1) & (c[i] <= 3), c[i], 0)   # dono do tick: 0 = ninguém (silêncio ou sobreposição)
        own[db < np.percentile(db, 90) - DIP_DB] = 0
        b = (np.flatnonzero(np.diff(own)) + 1).tolist()
        out: list[list] = []                                 # [início, fim, locutor local] em ticks
        for a, e in zip([0, *b], [*b, n]):
            if own[a] == 0:
                continue
            if out and a - out[-1][1] < PAUSE_S * 100 and own[a] == out[-1][2]:
                out[-1][1] = e                               # mesma voz, pausa curta: continua o trecho
            else:
                out.append([a, e, int(own[a])])
        return [(a / 100, e / 100, k) for a, e, k in out]

    def _need(self, dur: float) -> float:
        """Similaridade mínima para dois trechos vizinhos serem o mesmo locutor (curto: embedding ruidoso, exige menos)."""
        return self.threshold - SHORT_K * max(0.0, 1.0 - dur)

    def _same(self, a: _Piece, b: _Piece) -> float:
        """> 0: a e b (vizinhos) são o mesmo locutor; o valor é a folga."""
        ea, eb = _unit(a.v), _unit(b.v)
        ga, gb = self._guess(ea), self._guess(eb)
        if ga is not None and gb is not None:  # os dois casam com um locutor conhecido: vale a identidade
            return 1.0 if ga == gb else -1.0
        return float(ea @ eb) - self._need(min(a.w, b.w))

    def segments(self, audio: np.ndarray) -> list[tuple[float, float, int | None]]:
        """Divide uma utterance FINAL em trechos de um locutor só: [(início_s, fim_s, locutor_global)], ladrilhando
        [0, duração]. Pipeline: 1 trecho -> segue como hoje (rótulo = o do identify); > 1 -> transcribe(audio[a:b],
        final=True) e 1 Update por trecho. Chamar 1x por utterance final, na ordem (muda os centróides). Cabeçalho do módulo."""
        audio = np.ascontiguousarray(audio, np.float32).reshape(-1)
        d = len(audio) / SR
        if d < 2 * MIN_PIECE + PAUSE_S or float(np.sqrt(np.mean(np.square(audio)))) < MIN_RMS:
            return [(0.0, d, self.identify(audio, True))]
        whole = self._pool.submit(self._embed, audio)  # especulativo: o caso comum é 1 locutor (= identify)
        at = self._atoms(audio)
        fut = [self._pool.submit(self._embed, audio[int(s * SR):int(e * SR)]) if e - s >= MIN_PIECE else None for s, e, _ in at]
        g = []
        for (s, e, k), f in zip(at, fut):
            em = f and f.result()
            g.append(_Piece(s, e, None if em is None else em * (e - s), e - s, k, k))
        gap = lambda i, j: g[j].s - g[i].e if j > i else g[i].s - g[j].e  # noqa: E731

        def glue(bad) -> None:  # cola o menor trecho `bad` no vizinho de mesmo locutor local, senão no mais perto; repete
            while len(g) > 1:
                ruins = [k for k in range(len(g)) if bad(g[k])]
                if not ruins:
                    return
                i = min(ruins, key=lambda k: g[k].w)
                nb = [j for j in (i - 1, i + 1) if 0 <= j < len(g)]
                same = [j for j in nb if (g[j].ir if j < i else g[j].il) == (g[i].il if j < i else g[i].ir)]
                j = same[0] if len(same) == 1 else min(nb, key=lambda j: (gap(i, j), j > i))
                g[min(i, j)].join(g.pop(max(i, j)))

        glue(lambda p: p.w < MIN_PIECE)  # trecho curto não fica sozinho
        for p, f in [(p, self._pool.submit(self._embed, audio[int(p.s * SR):int(p.e * SR)])) for p in g if p.v is None]:
            em = f.result()  # >= MIN_PIECE feito só de micro-trechos: embeda o vão (são curtos)
            p.v = None if em is None else em * p.w
        glue(lambda p: p.v is None)
        while len(g) > 1:  # funde vizinhos do mesmo locutor (a folga maior primeiro)
            sc = [self._same(g[i], g[i + 1]) for i in range(len(g) - 1)]
            i = int(np.argmax(sc))
            if sc[i] < 0:
                break
            g[i].join(g.pop(i + 1))
        if len(g) < 2:  # 1 locutor (ou nada de fala): é o identify(audio, True)
            e = whole.result()
            return [(0.0, d, None if e is None else self._assign(e, d, d >= self.min_audio_s))]
        cut = [0.0] + [(a.e + b.s) / 2 for a, b in zip(g, g[1:])] + [d]  # fronteira = meio de cada pausa
        # rótulo de cada trecho: embedding do áudio dele (a média dos embeddings das partes borra e separa pior)
        span = [self._pool.submit(self._embed, audio[int(p.s * SR):int(p.e * SR)]) if p.n > 1 else None for p in g]
        emb = [_unit(p.v) if f is None else f.result() for p, f in zip(g, span)]
        lab: list[int | None] = [None] * len(g)
        for full in (True, False):  # longos (criam/atualizam) primeiro; curtos só palpitam
            for k in range(len(g)):
                dur = cut[k + 1] - cut[k]  # como no identify: inclui as pausas/pontas do trecho
                if emb[k] is not None and (dur >= self.min_audio_s) == full:
                    lab[k] = self._assign(emb[k], dur, full)
        out: list[tuple[float, float, int | None]] = []
        for k in range(len(g)):
            if out and out[-1][2] == lab[k]:
                out[-1] = (out[-1][0], cut[k + 1], lab[k])
            else:
                out.append((cut[k], cut[k + 1], lab[k]))
        return out
