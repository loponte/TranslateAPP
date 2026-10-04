"""Avaliação da tradução EN->PT-BR (e PT->EN com --dir pten): chrF (corpus, igual ao sacreBLEU padrão) + latência.

Uso, da raiz do projeto (PowerShell):  $env:PYTHONPATH=(Get-Location).Path
  .venv\\Scripts\\python.exe tests\\eval_mt.py                        # app.mt.Translator (models\\mt-en-pt): chrF + latência, GPU e CPU
  .venv\\Scripts\\python.exe tests\\eval_mt.py -v --device cuda       # + imprime cada tradução (EN/REF/HYP)
  .venv\\Scripts\\python.exe tests\\eval_mt.py --terms                # glossário: termos de call/jogo sem e com as regras
  .venv\\Scripts\\python.exe tests\\eval_mt.py --no-glossary          # chrF do modelo cru
  .venv\\Scripts\\python.exe tests\\eval_mt.py --dir pten             # PT->EN: as 55 frases ao contrário (app.mt, par pt-en)
  .venv\\Scripts\\python.exe tests\\eval_mt.py --cand opus madlad3b --root <pasta> --device cuda --ct bfloat16 --lat
Candidatos (--cand) = subpastas CT2 de --root, ver CANDS. Só o app/mt.py entra no produto.
Não precisa de pacote extra (sacreBLEU só serviu para validar o chrF próprio: idêntico até a 4a casa)."""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (categoria, inglês, referência pt-BR). "cut" e frases sem pontuação imitam os parciais do Whisper.
DATA = [
    ("chat", "Hey guys, can you hear me okay?", "Oi, pessoal, vocês estão me ouvindo bem?"),
    ("chat", "Yeah, I'm gonna hop on in like five minutes, I just need to grab some water.",
     "É, eu vou entrar daqui a tipo cinco minutos, só preciso pegar um pouco de água."),
    ("chat", "Dude, you're not gonna believe what just happened.", "Cara, você não vai acreditar no que acabou de acontecer."),
    ("chat", "No way, that's actually insane!", "Não é possível, isso é uma loucura!"),
    ("chat", "I think my mic is muted, can you guys hear me now",
     "Acho que meu microfone está no mudo, vocês conseguem me ouvir agora"),
    ("chat", "Wait, are you still there or did you get disconnected?", "Espera, você ainda está aí ou a conexão caiu?"),
    ("chat", "I've been waiting for like an hour and nobody showed up, so I just left.",
     "Eu estava esperando fazia tipo uma hora e ninguém apareceu, então eu simplesmente fui embora."),
    ("chat", "Let me share my screen real quick so you can see what I mean",
     "Deixa eu compartilhar minha tela rapidinho pra vocês verem o que eu quero dizer"),
    ("chat", "I don't know, man, it's kind of hard to say.", "Não sei, cara, é meio difícil dizer."),
    ("chat", "Oh my god, I forgot to turn off my camera!", "Meu Deus, esqueci de desligar minha câmera!"),
    ("chat", "Please send me the file on my cell phone, I'm on the bus right now.",
     "Por favor, me envie o arquivo no meu celular, estou no ônibus agora."),
    ("chat", "What are you up to this weekend? Anything fun?", "O que você vai fazer neste fim de semana? Alguma coisa divertida?"),
    ("chat", "Honestly, I wouldn't trust that guy with my wallet.", "Sinceramente, eu não confiaria minha carteira àquele cara."),
    ("chat", "I'm like, whatever, I don't even care anymore.", "Eu tô tipo, tanto faz, nem me importo mais."),
    ("chat", "Well, I mean, it depends on how you look at it, right?", "Bom, quer dizer, depende de como você olha pra isso, né?"),
    ("work", "Thanks everyone for joining, let's get started with the quarterly review.",
     "Obrigado a todos por participarem, vamos começar com a revisão trimestral."),
    ("work", "We need to push the deadline to next Friday because the client changed the requirements again.",
     "Precisamos adiar o prazo para a próxima sexta-feira porque o cliente mudou os requisitos de novo."),
    ("work", "Can you take a look at the pull request when you get a chance?",
     "Você pode dar uma olhada no pull request quando tiver um tempo?"),
    ("work", "The deployment failed last night, so we're rolling back to the previous version.",
     "O deploy falhou ontem à noite, então estamos voltando para a versão anterior."),
    ("work", "Our revenue grew by 12.5 percent compared to last year, which is above what we expected.",
     "Nossa receita cresceu 12,5 por cento em comparação com o ano passado, o que está acima do que esperávamos."),
    ("work", "I'll send you the slides after the call, and we can schedule a follow-up for Tuesday at 3 PM.",
     "Vou te enviar os slides depois da call, e podemos agendar um acompanhamento para terça-feira às 15h."),
    ("work", "So basically what we're trying to do here is reduce the latency without hurting the accuracy",
     "Então, basicamente, o que estamos tentando fazer aqui é reduzir a latência sem prejudicar a precisão"),
    ("work", "Sorry, I was on mute. Can everyone see my screen?", "Desculpa, eu estava no mudo. Todo mundo está vendo minha tela?"),
    ("work", "John from marketing will be joining us next week to talk about the new campaign.",
     "O John, do marketing, vai participar com a gente na semana que vem para falar da nova campanha."),
    ("work", "I'm not sure that's going to work, but let's give it a shot.", "Não tenho certeza se isso vai funcionar, mas vamos tentar."),
    ("work", "I took the bus to the office and my team was already there.", "Peguei o ônibus até o escritório e meu time já estava lá."),
    ("game", "Watch out, there's a sniper on the roof!", "Cuidado, tem um sniper no telhado!"),
    ("game", "I'm low on health, can someone heal me?", "Estou com pouca vida, alguém pode me curar?"),
    ("game", "We just need to capture the last point and we win the round.",
     "A gente só precisa capturar o último ponto e ganhamos a rodada."),
    ("game", "My ping is terrible right now, the game keeps lagging.", "Meu ping está horrível agora, o jogo não para de lagar."),
    ("game", "Nice shot! That was a clean headshot from across the map.", "Belo tiro! Foi um headshot limpo do outro lado do mapa."),
    ("game", "Okay, so the boss has three phases, and in the second phase you have to dodge the fire attacks",
     "Beleza, então o chefe tem três fases, e na segunda fase você tem que desviar dos ataques de fogo"),
    ("game", "Who's carrying this game? Because it's definitely not you, bro.",
     "Quem está carregando esse jogo? Porque com certeza não é você, mano."),
    ("game", "I died like ten times on that level and I almost rage quit.", "Eu morri tipo dez vezes naquela fase e quase dei rage quit."),
    ("tech", "The new graphics card has sixteen gigabytes of memory and supports ray tracing.",
     "A nova placa de vídeo tem dezesseis gigabytes de memória e suporta ray tracing."),
    ("tech", "You can install the package by running pip install and then restarting your terminal.",
     "Você pode instalar o pacote executando pip install e depois reiniciando o terminal."),
    ("tech", "OpenAI released a new model yesterday that's supposed to be way faster than the previous one.",
     "A OpenAI lançou um novo modelo ontem que supostamente é muito mais rápido que o anterior."),
    ("tech", "If you're on Windows 11, you have to enable developer mode in the settings first.",
     "Se você está no Windows 11, precisa ativar o modo de desenvolvedor nas configurações primeiro."),
    ("tech", "The battery lasts about eight hours, but it charges really fast, like zero to eighty percent in thirty minutes.",
     "A bateria dura cerca de oito horas, mas carrega muito rápido, tipo de zero a oitenta por cento em trinta minutos."),
    ("tech", "so the server is down because someone pushed a bad config to production",
     "então o servidor está fora do ar porque alguém enviou uma configuração ruim para produção"),
    ("tech", "Click the left mouse button and drag the file to the screen.",
     "Clique com o botão esquerdo do mouse e arraste o arquivo para a tela."),
    ("cut", "so what I was thinking is that we could maybe", "então o que eu estava pensando é que a gente poderia talvez"),
    ("cut", "if you go to the settings menu and then click on", "se você for ao menu de configurações e depois clicar em"),
    ("cut", "yeah I was just about to say that the", "é, eu estava prestes a dizer que o"),
    ("cut", "and then he told me that he wasn't going to be able to", "e então ele me disse que não ia conseguir"),
    ("names", "My name is Sarah Johnson and I live in San Francisco, but I was born in Rio de Janeiro.",
     "Meu nome é Sarah Johnson e eu moro em San Francisco, mas nasci no Rio de Janeiro."),
    ("names", "The meeting is on March 15th at 9:30 in the morning, room 204.",
     "A reunião é no dia 15 de março, às 9h30 da manhã, na sala 204."),
    ("names", "It costs $49.99 a month, or $499 if you pay for the whole year.",
     "Custa US$ 49,99 por mês, ou US$ 499 se você pagar o ano inteiro."),
    ("names", "Elon Musk said that Tesla will ship a million cars this year.",
     "Elon Musk disse que a Tesla vai entregar um milhão de carros este ano."),
    ("short", "Yeah, exactly.", "É, exatamente."),
    ("short", "Good game, everyone!", "Bom jogo, pessoal!"),
    ("short", "What's up, guys?", "E aí, pessoal?"),
    ("short", "Hold on a second.", "Espera um segundo."),
    ("short", "I'll be right back.", "Já volto."),
    ("short", "Thank you so much for having me", "Muito obrigado por me receber"),
    ("long", "Okay, so yesterday I was playing with my friends and the server crashed. We lost all our progress. I'm so done with this game.",
     "Beleza, ontem eu estava jogando com meus amigos e o servidor caiu. Perdemos todo o nosso progresso. Cansei desse jogo."),
    ("long", "Hi, welcome back to the channel! Today we're going to talk about how to build your own computer from scratch, and I promise it's way easier than you think.",
     "Oi, bem-vindos de volta ao canal! Hoje vamos falar sobre como montar o seu próprio computador do zero, e eu prometo que é muito mais fácil do que você pensa."),
]

# termos de call/jogo/dev: (inglês, regex que DEVE aparecer, regex que NÃO pode aparecer) -> mede o glossário de app/mt.py
TERMS = [
    ("Can you unmute yourself?", r"microfone", r"desmist|desmarc|desat"),
    ("Please unmute your mic.", r"microfone", r"desmist|desmarq"),
    ("Let me unmute real quick.", r"microfone", r"desat|desmist|desmarc"),
    ("Are you unmuted?", r"microfone", r"desmud"),
    ("Hold on, I need to unmute.", r"microfone", r"desmist|desmarc|desat"),
    ("Everyone, please unmute your mics.", r"microfone", r"desmist|desmarc|desat|desfa"),
    ("Can everyone unmute so we can start?", r"microfone", r"desmist|desmarc|desat"),
    ("Please mute yourselves when you're not talking.", r"microfone", r"calem|calar"),
    ("Can you mute yourself, please? There's a lot of noise.", r"microfone", r"calar|cale"),
    ("Sorry, I'm on mute.", r"mudo", r"silêncio"),
    ("You're muted.", r"mud|mut|silenc", None),
    ("Click the unmute button.", r"botão.*microfone", r"desmist"),
    ("Can you take a look at the pull request?", r"pull request", r"retirada|puxar"),
    ("I opened two pull requests yesterday.", r"pull requests", r"retirada|pedidos"),
    ("Please approve my pull request.", r"pull request", r"retirada"),
    ("We deploy to production every Friday.", r"produção", r"desloc"),
    ("I deployed the fix this morning.", r"correção", r"coloquei"),
    ("The army deployed troops to the border.", r"tropas", r"liber|lanç"),
    ("I have a lot of lag.", r"\blag\b", r"atraso"),
    ("There's way too much lag today.", r"\blag\b", r"atraso"),
    ("The game is lagging.", r"lag", r"atrasado"),
    ("My ping is really high.", r"ping", None),
    ("I died ten times and I almost rage quit.", r"raiva", r"fumar|demit"),
    ("He rage quit after the third death.", r"raiva", r"desligou|demit|mal"),
    ("She rage quit the game.", r"raiva", r"demit"),
    ("Next slide, please.", r"slide", r"diapositivo"),
    ("This slide shows our revenue.", r"slide", r"diapositivo"),
    ("Slide five has the numbers.", r"slide", r"diapositivo"),
    ("Nice headshot!", r"headshot", r"foto"),
    ("I'm low on health, can someone heal me?", r"vida", r"saúde"),
    ("There's a nasty bug in the inventory screen.", r"bug", r"inseto"),
    ("Sorry I'm late, my build took forever.", r"build", r"prédio"),
    ("We need to build a new house.", r"constr", r"\bbuild\b"),
    ("The people build houses.", r"constr", r"\bbuild\b"),
    ("Don't forget to push your branch before you log off.", r"branch", r"galho|ramo"),
    ("We can test it on staging first.", r"staging", r"palco"),
    ("The deploy is scheduled for Friday.", r"deploy", r"serviço"),
    ("Thanks for the follow, welcome to the stream!", r"stream", r"córrego"),
    ("The stream was cold and clear.", r"córrego|riacho|ribeiro|corrente|fluxo|stream", None),
    ("Did you update the drivers?", r"drivers", r"motorista"),
    ("The bus driver was late.", r"motorista", r"driver"),
    ("Camp the spawn, they'll come out eventually.", r"spawn", r"desova"),
    ("The matchmaking is trash today.", r"matchmaking", r"combina"),
    ("The patch notes say they nerfed the sniper rifle again.", r"nerfaram", r"nervaram"),
    ("Discord crashed on me.", r"Discord\b", r"Discórdia"),
    ("Check the Discord.", r"Discord\b", r"Discórdia"),
    ("Great. Is Wednesday realistic?", r"^Ótimo\. ", None),
    ("At times, it works. Last but not least, great idea!", r"^Às vezes.*Ótima ideia", None),
    ("Cool.", r"legal", r"fixe"),
    ("Mental health is important.", r"saúde mental", None),
    ("He is in good health.", r"saúde", r"vida"),
    ("Can you go back one slide?", r"slide", r"diapositivo"),
    ("Let me share my screen.", r"tela", None),
    ("Can you turn your camera on?", r"câmera", None),
    ("I got disconnected.", r"desconect", None),
]

# frases para latência: 4 / 12 / 25 palavras
LAT = {
    4: "Can you hear me?",
    12: "I think we should probably push the release to next week, honestly.",
    25: "So yesterday I was playing with my friends and the server crashed, we lost all our progress, and honestly I'm so done with this game.",
}

LAT_PT = {  # PT->EN
    4: "Vocês tão me ouvindo?",
    12: "Acho que a gente devia adiar o lançamento pra semana que vem, sinceramente.",
    25: "Então ontem eu tava jogando com os meus amigos e o servidor caiu, a gente perdeu todo o progresso e sinceramente eu cansei desse jogo.",
}

# marcadores de português de Portugal (devem ser raros/ausentes na saída)
PTPT = re.compile(r"\b(est(?:ou|ás|á|amos|ão)\s+a\s+\w+[aei]r|ecrã|ficheiros?|utilizador(?:es)?|equipa|telemóvel|autocarro|rato|miúdos?|"
                  r"pequeno-almoço|comboio|frigorífico|a\s+gente\s+vamos|tu\s+\w+)\b", re.I)


def _ngr(s: str, n: int) -> Counter:
    s = "".join(s.split())
    return Counter(s[i:i + n] for i in range(len(s) - n + 1))


def chrf(hyps: list[str], refs: list[str], order: int = 6, beta: int = 2) -> float:
    """chrF corpus-level (char 1..6, sem n-gramas de palavras, beta 2): mesma conta do sacreBLEU padrão."""
    tot = [[0, 0, 0] for _ in range(order)]  # hyp, ref, acertos
    for h, r in zip(hyps, refs):
        for n in range(1, order + 1):
            hn, rn = _ngr(h, n), _ngr(r, n)
            t = tot[n - 1]
            t[0] += sum(hn.values())
            t[1] += sum(rn.values())
            t[2] += sum((hn & rn).values())
    ps = [m / h for h, r, m in tot if h and r]
    rs = [m / r for h, r, m in tot if h and r]
    if not ps:
        return 0.0
    p, r = sum(ps) / len(ps), sum(rs) / len(rs)
    return 100 * (1 + beta ** 2) * p * r / (beta ** 2 * p + r) if p + r else 0.0


def median_ms(fn, text: str, n: int = 25, warm: int = 3) -> tuple[float, float]:
    for _ in range(warm):
        fn(text)
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn(text)
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    return statistics.median(ts), ts[int(0.9 * (len(ts) - 1))]


def report(name: str, fn, device: str, verbose: bool, lat: bool, n: int, pten: bool = False) -> dict:
    srcs = [pt if pten else en for _, en, pt in DATA]  # pten: a referência em português vira a origem
    refs = [en if pten else pt for _, en, pt in DATA]
    t = time.perf_counter()
    hyps = [fn(x) for x in srcs]
    dt = (time.perf_counter() - t) / len(DATA) * 1000
    out = {"all": chrf(hyps, refs)}
    for tag in dict.fromkeys(c for c, _, _ in DATA):
        idx = [i for i, (c, _, _) in enumerate(DATA) if c == tag]
        out[tag] = chrf([hyps[i] for i in idx], [refs[i] for i in idx])
    ptpt = 0 if pten else sum(bool(PTPT.search(h)) for h in hyps)
    print(f"\n### {name} [{device}]  chrF={out['all']:.1f}  " + " ".join(f"{k}={v:.0f}" for k, v in out.items() if k != "all")
          + f"  ptpt={ptpt}  media/frase={dt:.0f} ms")
    if verbose:
        for (tag, *_), src, ref, h in zip(DATA, srcs, refs, hyps):
            print(f"  [{tag}] SRC: {src}\n         REF: {ref}\n         HYP: {h}")
    if lat:
        res = {}
        for w, text in (LAT_PT if pten else LAT).items():
            med, p90 = median_ms(fn, text, n)
            res[w] = med
            print(f"  latência {w:>2} palavras: mediana {med:6.1f} ms  p90 {p90:6.1f} ms   -> {fn(text)}")
        out["lat"] = res
    out["hyps"] = hyps
    return out


# ---- candidatos CT2 (só para escolher o modelo; o produto usa app/mt.py) ----
# nome -> (tipo, pasta em --root, [token de idioma]). Pastas = HF baixado com snapshot_download (ou convertido, scripts/convert_mt.py):
#   opus-big-ct2=ooeoeo/opus-mt-tc-big-en-pt-ct2-float16  romance-ct2=michaelfeil/ct2fast-opus-mt-en-ROMANCE
#   nllb600-ct2=JustFrederik/nllb-200-distilled-600M-ct2-int8  nllb1300-ct2=OpenNMT/nllb-200-distilled-1.3B-ct2-int8
#   nllb3300-ct2=OpenNMT/nllb-200-3.3B-ct2-int8  madlad3b-ct2=Nextcloud-AI/madlad400-3b-mt-ct2-int8
#   lmt06-ct2/lmt17-ct2=NiuTrans/LMT-60-0.6B/1.7B e tgemma4b-bf16-ct2=Infomaniak-AI/vllm-translategemma-4b-it (espelho aberto do
#   google/translategemma-4b-it), convertidos: ct2-transformers-converter --quantization float16|bfloat16 --copy_files tokenizer.json
CANDS = {
    "opus": ("marian", "opus-big-ct2"), "nllb600": ("nllb", "nllb600-ct2"), "nllb1300": ("nllb", "nllb1300-ct2"),
    "nllb3300": ("nllb", "nllb3300-ct2"), "madlad3b": ("madlad", "madlad3b-ct2"),
    "lmt06": ("lmt", "lmt06-ct2"), "lmt17": ("lmt", "lmt17-ct2"), "tgemma4b": ("gemma", "tgemma4b-bf16-ct2"),
    "opus_por": ("marian", "opus-big-ct2", ">>por<<"), "romance": ("marian", "romance-ct2", ">>pt_br<<"),
}
# LLMs de tradução (decoder-only): prompt de chat + token de fim
LLM_TPL = {
    "lmt": ("<|im_start|>user\nTranslate the following text from English into Portuguese:\nEnglish: {t}\nPortuguese:<|im_end|>\n"
            "<|im_start|>assistant\n", "<|im_end|>"),
    "gemma": ("<bos><start_of_turn>user\nYou are a professional English (en) to Portuguese (pt-BR) translator. Your goal is to "
              "accurately convey the meaning and nuances of the original English text while adhering to Portuguese grammar, "
              "vocabulary, and cultural sensitivities.\nProduce only the Portuguese translation, without any additional "
              "explanations or commentary. Please translate the following English text into Portuguese:\n\n\n{t}<end_of_turn>\n"
              "<start_of_turn>model\n", "<end_of_turn>"),
}
SPLIT = re.compile(r"(?<=[.!?])\s+")


def load_cand(name: str, root: Path, device: str, ct: str, beam: int, threads: int):
    from app.cuda_dlls import setup_cuda_dlls
    setup_cuda_dlls()
    import ctranslate2
    import sentencepiece as spm
    kind, sub, *extra = CANDS[name]
    lang = extra[0] if extra else ">>pob<<"
    p = root / sub
    if kind in LLM_TPL:
        from tokenizers import Tokenizer
        gen = ctranslate2.Generator(str(p), device=device, compute_type=ct, intra_threads=threads)
        tk, (tpl, end) = Tokenizer.from_file(str(p / "tokenizer.json")), LLM_TPL[kind]

        def translate_llm(text: str) -> str:
            sents = [x for x in SPLIT.split(text.strip()) if x]
            src = [tk.encode(tpl.format(t=x), add_special_tokens=False).tokens for x in sents]
            n = max(len(tk.encode(x, add_special_tokens=False).ids) for x in sents)
            r = gen.generate_batch(src, max_length=2 * n + 16, beam_size=beam, sampling_topk=1, include_prompt_in_result=False,
                                   end_token=end)
            return " ".join(tk.decode([tk.token_to_id(t) for t in x.sequences[0]], skip_special_tokens=True).strip() for x in r)

        return translate_llm
    tr = ctranslate2.Translator(str(p), device=device, compute_type=ct, intra_threads=threads)
    sp = lambda f: spm.SentencePieceProcessor(model_file=str(p / f))  # noqa: E731
    if kind == "marian":
        s_in, s_out = sp("source.spm"), sp("target.spm")
        pre = lambda s: [lang] + s_in.encode(s, out_type=str) + ["</s>"]  # noqa: E731
        post, tgt = s_out.decode, None
    elif kind == "nllb":
        s = sp("sentencepiece.bpe.model") if (p / "sentencepiece.bpe.model").exists() else sp("../nllb600-ct2/sentencepiece.bpe.model")
        pre = lambda x: ["eng_Latn"] + s.encode(x, out_type=str) + ["</s>"]  # noqa: E731
        post, tgt = (lambda h: s.decode(h[1:])), ["por_Latn"]
    else:  # madlad
        s = sp("spiece.model")
        pre = lambda x: s.encode("<2pt> " + x, out_type=str) + ["</s>"]  # noqa: E731
        post, tgt = s.decode, None

    def translate(text: str) -> str:
        sents = [x for x in SPLIT.split(text.strip()) if x]
        src = [pre(x) for x in sents]
        r = tr.translate_batch(src, target_prefix=[tgt] * len(src) if tgt else None, beam_size=beam,
                               max_decoding_length=2 * max(map(len, src)) + 10)
        return " ".join(post(x.hypotheses[0]) for x in r)

    return translate


def check_terms(tr, verbose: bool = True) -> None:
    """Roda TERMS sem e com o glossário de app/mt.py e mostra quantos passam."""
    import app.mt as mt
    pre, post = mt._PRE, mt._POST
    try:
        for label, tables in (("sem glossário", ([], [])), ("com glossário", (pre, post))):
            mt._PRE, mt._POST = tables
            ok = 0
            for en, must, mustnot in TERMS:
                out = tr.translate(en)
                good = (not must or re.search(must, out, re.I)) and not (mustnot and re.search(mustnot, out, re.I))
                ok += bool(good)
                if verbose:
                    print(f"  [{'ok' if good else 'XX'}] {en}  ->  {out}")
            print(f"### termos {label}: {ok}/{len(TERMS)}")
    finally:
        mt._PRE, mt._POST = pre, post


def main() -> None:
    sys.stdout.reconfigure(errors="replace")  # console cp1252
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms", action="store_true", help="testa o glossário de termos de call/jogo (app.mt)")
    ap.add_argument("--no-glossary", action="store_true", help="avalia o app.mt sem o glossário (chrF do modelo cru)")
    ap.add_argument("--cand", nargs="*", help="candidatos CT2 (veja CANDS); sem isso avalia app.mt.Translator")
    ap.add_argument("--root", default=str(ROOT / "models" / "mt-cand"))
    ap.add_argument("--device", default="both", choices=["cuda", "cpu", "both"])
    ap.add_argument("--ct", default="int8_float16", help="compute_type dos candidatos (GPU); CPU usa int8")
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("-n", type=int, default=25, help="rodadas de latência (mediana)")
    ap.add_argument("--lat", action="store_true", help="mede latência (4/12/25 palavras)")
    ap.add_argument("--dir", default="ente", choices=["ente", "pten"], help="ente = EN->PT (padrão); pten = PT->EN (só app.mt)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    devs = ["cuda", "cpu"] if a.device == "both" else [a.device]
    if a.cand:
        for d in devs:
            for name in a.cand:
                fn = load_cand(name, Path(a.root), d, a.ct if d == "cuda" else "int8", a.beam, a.threads)
                report(f"{name} {a.ct if d == 'cuda' else 'int8'} beam{a.beam}", fn, d, a.v, a.lat, a.n)
        return
    import app.mt as mt
    if a.no_glossary:
        mt._PRE, mt._POST = [], []
    pten = a.dir == "pten"
    for d in devs:
        tr = mt.Translator(pair="pt-en" if pten else "en-pt", device=d)
        tr.warmup()
        if a.terms and not pten:
            check_terms(tr, a.v)
            continue
        report(f"app.mt.Translator {tr.pair}", tr.translate, d, a.v, True, a.n, pten)


if __name__ == "__main__":
    main()
