"""Gera as fixtures de áudio do TranslateAPP (conversas sintéticas via edge-tts) e os gabaritos JSON.

Uso (da raiz do projeto):
    uv run --no-project --with edge-tts --with numpy python tests/data/make_fixtures.py

- Trechos sintetizados ficam em tests/data/_cache/ (mp3); só trechos novos pedem internet.
- Precisa do ffmpeg no PATH (decodifica o mp3 e gera a versão 48 kHz estéreo).
- Tempos do gabarito = fala efetiva: o silêncio das pontas de cada trecho é aparado por energia.
"""
import asyncio
import hashlib
import json
import subprocess
import sys
import wave
from pathlib import Path

import edge_tts
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"
SR = 16000                              # taxa dos wavs "16k"
FR = SR // 100                          # frame de 10 ms (tudo na linha do tempo é múltiplo dele)
THR = 10 ** (-50 / 20)                  # frame com RMS acima disto conta como fala (-50 dBFS)
LEVEL = 10 ** (-20 / 20)                # RMS da fala ativa de cada trecho (-20 dBFS)
PAUSAS = [0.15, 0.25, 0.10, 0.30, 0.20]  # pausas internas dos turnos (s), em ciclo
MAX_PAUSA = 0.30                        # teto de silêncio interno num turno (o TTS faz ~0,95 s a cada ponto final)
USADOS = set()                          # arquivos do cache usados nesta execução (o resto é podado)

# ------------------------------------------------------------------ roteiros
# (locutor, texto). "|" = pausa interna curta (0,1-0,3 s) dentro do turno; não entra no texto do gabarito.
V2 = {"A": "en-US-GuyNeural", "B": "en-US-JennyNeural"}
S2 = [
    ("A", "Hey Dana, you there? | Can you hear me okay?"),
    ("B", "Yeah, loud and clear. Sorry I'm late, my build took forever."),
    ("A", "No worries. | So, about Project Nebula, | we need to lock the plan before Friday."),
    ("B", "Right, got it."),
    ("A", "Okay, three things are left. | First, the matchmaking API still times out after about thirty seconds. "
          "| Second, there's a nasty bug in the inventory screen. | And third, Marcus is out until Monday."),
    ("B", "Wait, what?"),
    ("A", "Yeah, his flight got moved. His pull request for the inventory fix is still waiting for review."),
    ("B", "Okay, I'll review it right after this call. Honestly, I'm more worried about the lag on the Frankfurt servers."),
    ("A", "Yeah."),
    ("B", "Players in Brazil are seeing a ping of around two hundred and fifty milliseconds, "
          "which is way too high for a shooter."),
    ("A", "That's rough. Can we spin up another region on AWS, maybe one in Sao Paulo?"),
    ("B", "It would cost around four hundred dollars a month, but I think it's worth it. | We can test it on staging first."),
    ("A", "Perfect. And the new boss? Players say his fire attack is way too strong."),
    ("B", "Yeah, I'll nerf it by fifteen percent | and bump the cooldown from eight seconds to twelve."),
    ("A", "Nice. | Okay, | the deploy is scheduled for Friday at six p.m. Pacific time. "
          "And we still need to write the patch notes for version two point four."),
    ("B", "Hold on, Friday at six? That's three in the morning for Tom in Berlin."),
    ("A", "Oh, right."),
    ("B", "Let's do Thursday at noon instead, and keep Friday as a backup."),
    ("A", "Works for me. I'll update the calendar and post it in the channel."),
    ("B", "Awesome. | Talk later?"),
    ("A", "Yep, see you tomorrow. | And don't forget to push your branch before you log off."),
]
G2 = [0.40, 0.65, 0.30, 0.90, 0.50, 1.00, 0.35, 0.75, 0.25, 0.55,
      0.85, 0.45, 0.30, 0.70, 0.60, 0.95, 0.35, 0.50, 0.80, 0.25]   # pausas entre turnos (s)

V4 = {"A": "en-US-GuyNeural", "B": "en-US-AriaNeural", "C": "en-GB-RyanNeural", "D": "en-AU-NatashaNeural"}
S4 = [
    ("A", "Alright everyone, let's get started. Thanks for joining."),
    ("B", "Morning, Alex."),
    ("C", "Morning, all."),
    ("D", "Hi, everyone."),
    ("A", "So, first on the agenda, the mobile release. Sarah, can you give us a quick update?"),
    ("B", "Sure. We closed forty two tickets this sprint, which is our best number so far, and the crash rate "
          "dropped from two point one percent to zero point six after we fixed the memory leak in the image loader. "
          "The only blocker left is the payment screen on older Android phones, and Jake thinks he can have a fix "
          "ready by Wednesday."),                                                      # monólogo 1
    ("A", "Great. Is Wednesday realistic?"),
    ("B", "I think so."),
    ("C", "Sounds good."),
    ("A", "Okay. James, what's the status on the backend?"),
    ("C", "The new search API is finally in staging, and the response time went down from nine hundred "
          "milliseconds to about two hundred. We still need to run a load test with ten thousand concurrent users "
          "before we deploy to production, and I'd like to do it on Thursday night when traffic is lowest, "
          "so nobody gets paged at three in the morning."),                            # monólogo 2
    ("D", "Makes sense."),
    ("A", "Any risk we'd miss the October fourteenth date?"),
    ("C", "Only if the test fails."),
    ("A", "Fair enough. Olivia, you're up."),
    ("D", "Thanks. We surveyed twelve hundred users last week, and the main complaint is still the onboarding flow, "
          "which takes about four minutes. People love the new dark mode, though, and our score went up from "
          "thirty eight to forty seven. I suggest we cut onboarding to three screens and translate the whole app "
          "into Portuguese and Spanish before launch."),                               # monólogo 3
    ("B", "Love that."),
    ("C", "How much would the translation cost?"),
    ("D", "About twelve thousand dollars for both languages, and the vendor needs two weeks."),
    ("A", "Two weeks is tight. Can we start with Portuguese only?"),
    ("D", "Yes, that works."),
    ("B", "Perfect."),
    ("A", "Good. Next, a quick one on hiring. We have two open positions on the QA team, and the budget was "
          "approved yesterday."),
    ("C", "Finally. We really need them."),
    ("B", "Should I post the job ads today?"),
    ("A", "Yes, please, and send me the link so I can share it with the team leads."),
    ("B", "Will do."),
    ("D", "Also, the launch event in Sydney is confirmed for November second, so we need the final build "
          "a week before that."),
    ("A", "Noted. That puts the code freeze on October twenty sixth."),
    ("C", "Works for me."),
    ("A", "Okay, so to recap. Sarah owns the payment fix, James runs the load test on Thursday, and Olivia sends "
          "the Portuguese strings to the vendor by Monday. Anything else?"),
    ("C", "Nothing from me."),
    ("B", "Same here."),
    ("D", "All good."),
    ("A", "Thanks, everybody. Talk next week."),
]
G4 = [0.35, 0.25, 0.20, 0.45, 0.60, 0.30, 0.80, 0.20, 0.50, 0.35, 0.70, 0.30, 0.25, 0.55,
      0.40, 0.90, 0.30, 0.45, 0.20, 0.65, 0.35, 0.50, 0.30, 0.75, 0.25, 0.40]


# ------------------------------------------------------------ síntese e áudio
async def _tts(voz, texto, sem):
    f = CACHE / (hashlib.sha1(f"{voz}|{texto}".encode()).hexdigest()[:16] + ".mp3")
    USADOS.add(f)
    if f.exists():
        return f
    async with sem:
        for k in range(4):  # a API às vezes falha (NoAudioReceived/403): tenta de novo
            try:
                await edge_tts.Communicate(texto, voz).save(f"{f}.part")
                Path(f"{f}.part").replace(f)
                return f
            except Exception as e:
                erro = e
                await asyncio.sleep(2 * (k + 1))
    raise RuntimeError(f"edge-tts falhou ({voz}: {texto!r}): {erro}")


def decode(f):
    """mp3 do TTS -> float64 mono 16 kHz."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(f), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32).astype(np.float64)


def synth(pares):
    """Sintetiza (ou lê do cache) cada (voz, texto); devolve {(voz, texto): áudio}."""
    CACHE.mkdir(exist_ok=True)
    pares = sorted(pares)

    async def todos():
        sem = asyncio.Semaphore(4)
        return await asyncio.gather(*(_tts(v, t, sem) for v, t in pares))

    return {p: decode(f) for p, f in zip(pares, asyncio.run(todos()))}


def frms(x):
    """RMS por frame de 10 ms."""
    n = len(x) // FR
    return np.sqrt((x[: n * FR].reshape(n, FR) ** 2).mean(1))


def sil(s):
    return np.zeros(round(s * 100) * FR)  # silêncio digital, alinhado a frames


def prep(x):
    """Normaliza a fala ativa para -20 dBFS e apara o silêncio das pontas (o TTS põe ~100 ms de cada lado)."""
    r = frms(x)
    x = x * (LEVEL / np.sqrt((r[r > THR] ** 2).mean()))
    x = x * min(1.0, 0.9 / np.abs(x).max())
    a = np.flatnonzero(frms(x) > THR)
    return x[a[0] * FR:(a[-1] + 1) * FR]


def squeeze(x, max_s):
    """Encurta silêncios internos maiores que max_s cortando o miolo (garante monólogo sem pausa longa)."""
    q = frms(x) <= THR
    keep = np.ones(len(q), bool)
    i = 0
    while i < len(q):
        j = i
        while j < len(q) and q[j] == q[i]:
            j += 1
        ok = round(max_s * 100)
        if q[i] and j - i > ok:
            keep[i + ok // 2:j - (ok - ok // 2)] = False
        i = j
    return x[: len(q) * FR].reshape(-1, FR)[keep].ravel()


def build(voices, script, gaps):
    """Monta a conversa: devolve (áudio, gabarito). Posições em amostras -> segundos (sem acumular erro)."""
    chunks = [[c.strip() for c in texto.split("|")] for _, texto in script]
    wav = synth({(voices[s], c) for (s, _), cs in zip(script, chunks) for c in cs})
    partes, turns, pos = [sil(0.6)], [], len(sil(0.6))
    for i, ((spk, _), cs) in enumerate(zip(script, chunks)):
        if i:
            partes.append(sil(gaps[(i - 1) % len(gaps)]))
            pos += len(partes[-1])
        t = []
        for j, c in enumerate(cs):
            if j:
                t.append(sil(PAUSAS[(i + j) % len(PAUSAS)]))
            t.append(squeeze(prep(wav[(voices[spk], c)]), MAX_PAUSA))
        t = np.concatenate(t)
        turns.append({"speaker": spk, "text": " ".join(cs), "start": round(pos / SR, 3), "end": round((pos + len(t)) / SR, 3)})
        partes.append(t)
        pos += len(t)
    partes.append(sil(0.6))
    return np.concatenate(partes), {"voices": voices, "turns": turns}


def save(path, y, sr=SR):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.rint(np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())


# ------------------------------------------------------------ versão ruidosa
def pink(n, rng):
    """Ruído rosa (densidade 1/f) com variância 1."""
    X = np.fft.rfft(rng.standard_normal(n))
    X[0] = 0
    X[1:] /= np.sqrt(np.arange(1, len(X)))
    y = np.fft.irfft(X, n)
    return y / y.std()


def music(n):
    """'Música' tonal sintética (pad): C-Am-F-G, um acorde a cada 2 s, janelas sen² de 4 s com 50% de sobreposição."""
    t = np.arange(n) / SR
    y = np.zeros(n)
    acordes = [(261.6, 329.6, 392.0), (220.0, 261.6, 329.6), (174.6, 220.0, 261.6), (196.0, 246.9, 293.7)]
    for k in range(-1, -(-n // (2 * SR))):
        a, b = max(0, 2 * k * SR), min(n, (2 * k + 4) * SR)
        env = np.sin(np.pi * (t[a:b] - 2 * k) / 4) ** 2
        for f in acordes[k % 4]:
            y[a:b] += env * sum(np.sin(2 * np.pi * h * f * t[a:b]) / h for h in (1, 2, 3))
    return y / y.std()


def noisy(y, turns, snr=12.0, seed=7):
    """Soma ruído rosa + música (mesma potência). SNR = potência da fala dentro dos turnos / potência do ruído."""
    n = pink(len(y), np.random.default_rng(seed)) + music(len(y))
    fala = np.concatenate([y[round(t["start"] * SR):round(t["end"] * SR)] for t in turns])
    n *= np.sqrt((fala ** 2).mean() / 10 ** (snr / 10) / (n ** 2).mean())
    z = y + n
    return z * min(1.0, 0.95 / np.abs(z).max())  # sem clipping (escala igual não muda a SNR)


# ------------------------------------------------------------------ validação
def read(path):
    with wave.open(str(path)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32768
    return sr, ch, sw, x.reshape(-1, ch)


def db(p):
    return 10 * np.log10(p + 1e-12)


def maxrun(q):
    """Maior sequência de True."""
    m = c = 0
    for v in q:
        c = c + 1 if v else 0
        m = max(m, c)
    return m


def verify(name, clean=True):
    """Abre o wav (stdlib wave), compara com o gabarito e imprime o resumo. Devolve (duração, turnos)."""
    sr, ch, sw, x = read(HERE / f"{name}.wav")
    assert (sr, ch, sw) == (SR, 1, 2), (name, sr, ch, sw)
    turns = json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))["turns"]
    x = x[:, 0]
    r = frms(x)
    inside, near = np.zeros(len(r), bool), np.zeros(len(r), bool)
    for t in turns:
        s, e = round(t["start"] * 100), round(t["end"] * 100)
        inside[s:e] = True
        near[max(0, s - 5):e + 5] = True  # tolerância de ±50 ms
    rin, rgap = db((r[inside] ** 2).mean()), db((r[~near] ** 2).mean())
    assert rin > -30, f"{name}: RMS dentro dos turnos baixo ({rin:.1f} dBFS)"
    extra = ""
    if clean:
        act = r > THR
        assert rgap < -60 and not (act & ~near).any(), f"{name}: há fala fora dos turnos (gap {rgap:.1f} dBFS)"
        dev = 0
        for t in turns:  # primeira/última fala ativa perto de cada borda do gabarito
            s, e = round(t["start"] * 100), round(t["end"] * 100)
            w = np.flatnonzero(act[s - 6:e + 6]) + s - 6
            dev = max(dev, abs(w[0] - s), abs(w[-1] + 1 - e))
        assert dev <= 5, f"{name}: borda do gabarito desviada {dev * 10} ms"
        extra = f" | desvio de borda máx {dev * 10} ms, fala ativa {act[inside].mean():.0%} dos frames dos turnos"
    else:
        assert 10 < rin - rgap < 14, f"{name}: contraste turno/gap {rin - rgap:.1f} dB"
        c = read(HERE / "conv_2spk.wav")[3][:, 0]  # ruído = noisy - g*clean (g por mínimos quadrados)
        g = (c @ x) / (c @ c)
        m = inside.repeat(FR)  # máscara por amostra (a linha do tempo é múltipla de FR)
        snr = db(((g * c[m]) ** 2).mean()) - db(((x - g * c) ** 2).mean())
        assert abs(snr - 12) < 0.5, f"{name}: SNR {snr:.2f} dB"
        extra = f" | SNR medida {snr:.2f} dB"
    spk = {s: sum(t["speaker"] == s for t in turns) for s in sorted({t["speaker"] for t in turns})}
    gaps = [b["start"] - a["end"] for a, b in zip(turns, turns[1:])]
    print(f"{name}.wav: {sr} Hz, {ch} ch, PCM16, {len(x) / sr:.2f} s | {len(turns)} turnos {spk} | "
          f"RMS turnos {rin:.1f} / gaps {rgap:.1f} dBFS | gaps {min(gaps):.2f}-{max(gaps):.2f} s{extra}")
    return len(x) / sr, turns, r


def main():
    y2, g2 = build(V2, S2, G2)
    y4, g4 = build(V4, S4, G4)
    for name, y, g in [("conv_2spk", y2, g2), ("conv_4spk", y4, g4), ("conv_2spk_noisy", noisy(y2, g2["turns"]), g2)]:
        save(HERE / f"{name}.wav", y)
        (HERE / f"{name}.json").write_text(json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(HERE / "conv_2spk.wav"), "-ar", "48000", "-ac", "2",
                    "-c:a", "pcm_s16le", str(HERE / "conv_2spk_48k_stereo.wav")], check=True)
    for f in CACHE.glob("*.mp3"):  # poda trechos de versões antigas do roteiro
        if f not in USADOS:
            f.unlink()

    # --- validação (qualquer assert aborta antes de criar o READY)
    d2, t2, _ = verify("conv_2spk")
    d4, t4, r4 = verify("conv_4spk")
    verify("conv_2spk_noisy", clean=False)
    sr, ch, sw, x = read(HERE / "conv_2spk_48k_stereo.wav")
    assert (sr, ch, sw) == (48000, 2, 2) and abs(len(x) / sr - d2) < 0.01 and (x[:, 0] == x[:, 1]).all()
    print(f"conv_2spk_48k_stereo.wav: {sr} Hz, {ch} ch, PCM16, {len(x) / sr:.2f} s")
    assert 16 <= len(t2) <= 22 and 95 <= d2 <= 106, (len(t2), d2)
    gaps = [b["start"] - a["end"] for a, b in zip(t2, t2[1:])]
    assert min(gaps) >= 0.249 and max(gaps) <= 1.001, (min(gaps), max(gaps))
    assert 140 <= d4 <= 160 and len(set(g4["voices"].values())) == 4, d4
    longos = [t for t in t4 if t["end"] - t["start"] >= 12]
    for t in longos:
        q = r4[round(t["start"] * 100):round(t["end"] * 100)] <= THR
        print(f"  monólogo {t['speaker']}: {t['end'] - t['start']:.1f} s, maior pausa interna {maxrun(q) * 10} ms")
        assert 15 <= t["end"] - t["start"] <= 25 and maxrun(q) <= 40
    curtos = [t for t in t4 if t["end"] - t["start"] < 1.5]
    print(f"  conv_4spk: {len(longos)} monólogos, {len(curtos)} turnos < 1,5 s")
    assert len(longos) == 3 and len(curtos) >= 8
    print("OK")


if __name__ == "__main__":
    main()
