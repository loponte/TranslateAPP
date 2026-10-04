# tests/data — fixtures de áudio

Conversas sintéticas em inglês (edge-tts, vozes neurais Microsoft) + gabarito. Gerado por `make_fixtures.py`;
reprodutível (saída idêntica byte a byte) e só trechos novos precisam de internet (cache em `_cache/`):

    uv run --no-project --with edge-tts --with numpy python tests\data\make_fixtures.py

| Arquivo | Formato | Duração | Conteúdo |
|---|---|---|---|
| `conv_2spk.wav` | 16 kHz mono PCM16 | 99,8 s | Discord casual, 21 turnos. A = en-US-GuyNeural, B = en-US-JennyNeural |
| `conv_4spk.wav` | 16 kHz mono PCM16 | 148,5 s | reunião, 35 turnos. A = en-US-GuyNeural, B = en-US-AriaNeural, C = en-GB-RyanNeural, D = en-AU-NatashaNeural |
| `conv_2spk_noisy.wav` | 16 kHz mono PCM16 | 99,8 s | o 2spk + ruído rosa + "música" tonal sintética (acordes C-Am-F-G), SNR 12 dB |
| `conv_2spk_48k_stereo.wav` | 48 kHz estéreo PCM16 | 99,8 s | o 2spk reamostrado (canais idênticos), para playback/loopback |
| `conv_2spk.json`, `conv_4spk.json`, `conv_2spk_noisy.json` | | | gabarito (o noisy é cópia do 2spk; para o 48k use `conv_2spk.json`) |

## Gabarito

`{"voices": {"A": "en-US-GuyNeural", ...}, "turns": [{"speaker": "A", "text": "...", "start": 0.6, "end": 3.22}, ...]}`
— tempos em segundos desde o início do wav.

- `start`/`end` = fala efetiva, medida por energia (frames de 10 ms com RMS > -50 dBFS, 30 dB abaixo da fala). Trocar o
  limiar entre -55 e -45 dBFS move as bordas < 30 ms em 95% dos trechos (pior caso 80 ms, em caudas de decaimento).
- Sem sobreposição. Gaps entre turnos: 0,25–1,00 s (2spk) e 0,20–0,90 s (4spk). Silêncio interno de um turno ≤ 0,30 s
  (pausas inseridas de 0,10–0,30 s; as pausas de ~0,95 s que o TTS faz a cada ponto final foram encurtadas).
- Gaps do áudio limpo são silêncio digital (como o loopback do Windows sem áudio tocando); no noisy o ruído é contínuo
  (gaps ≈ -32 dBFS, fala ≈ -20 dBFS). SNR = potência da fala dentro dos turnos / potência do ruído (rosa e música com
  potência igual). Todas as vozes normalizadas para -20 dBFS RMS na fala ativa.
- Números vão por extenso no `text` ("forty two"); normalize antes de comparar com o Whisper, que escreve "42".
- 2spk: jargão (API, deploy, bug, lag, ping, patch notes, pull request, staging, cooldown, nerf), nomes próprios,
  interjeições isoladas ("Yeah.", "Right, got it.", "Wait, what?", "Oh, right.").
- 4spk: 3 monólogos (18,4 / 17,5 / 20,3 s; vozes B, C, D; nenhuma pausa interna > 0,3 s) para testar corte de utterance
  longa; 15 turnos < 1,5 s ("Morning, Alex.", "Sounds good.", "All good."...).

Referência: faster-whisper `small.en` (int8) transcrevendo cada turno recortado pelo gabarito dá WER 0,3% (2spk),
2,2% (4spk; só normalização, "back end" / "Oct. 26") e 0,0% (noisy).

## Fixtures em português (`make_pt.py`)
Irmãs dos fixtures EN, com o mesmo formato e gabarito (reaproveita `make_fixtures.py`; cache próprio em `_cache_pt/`):

    uv run --no-project --with edge-tts --with numpy python tests\data\make_pt.py

- `conv_pt_2spk.wav` (102,4 s, 21 turnos; Antonio/Francisca), `conv_pt_3spk.wav` (70,6 s, 17 turnos; + Thalita) e
  `conv_pt_2spk_noisy.wav` (12 dB). Usados por `tests/bench_asr.py` (WER PT e acerto de idioma) e `tests/e2e_report.py`
  (`conv_pt_2spk --sub-lang en`; `misto` = conv_2spk + 1 s + conv_pt_2spk, montado na memória).
