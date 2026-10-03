"""Tradução EN -> PT-BR 100% local: OPUS-MT tc-big (Marian) rodando em CTranslate2.

Escolhido por medição (tests/eval_mt.py): melhor qualidade dentro do orçamento de latência (~60 ms p/ 25 palavras na GPU).
O token >>pob<< força português do Brasil (>>por<< mistura Portugal; NLLB/MADLAD vazam "estás a fazer", "ecrã").
Uso: ensure_model() uma vez (baixa ~860 MB e converte para models/mt-en-pt) e depois Translator().translate(texto)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

from app.cuda_dlls import setup_cuda_dlls

# Modelo oficial do Helsinki-NLP (Tatoeba-MT, CC-BY-4.0; atribuição: OPUS-MT). Marian npz -> CTranslate2 aqui mesmo,
# só com ctranslate2+pyyaml (sem torch). Zip imutável (data no nome) e com checksum fixado.
ZIP_URL = "https://object.pouta.csc.fi/Tatoeba-MT-models/eng-por/opusTCv20210807+bt_transformer-big_2022-03-13.zip"
ZIP_SHA256 = "62aeb8916c2463351a2dd8d1ea51fcf3929fb3daab7261dcae8d5599e886c008"
LANG = ">>pob<<"
_FILES = ("model.bin", "config.json", "shared_vocabulary.json", "source.spm", "target.spm")

_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_END = re.compile(r"[.!?…]\s*$")
_UNK = chr(0xE000)  # caractere privado: marca o <unk> no texto de saída
_ACC = "àâêíóôú"  # letras que faltam, em maiúscula, no vocabulário de saída do OPUS
_I = re.I

# Glossário mínimo: erros sistemáticos medidos em frases de call/jogo/dev (python tests/eval_mt.py --terms: 17/55 -> 55/55).
# Sem ele: "unmute" -> "desmistificar", "mute yourself" -> "cale-se", "pull request" -> "pedido de retirada", "slide" -> "dia",
# "rage quit" -> "parei de fumar", "deploy" -> "nos deslocamos", "update the drivers" -> "atualizou os motoristas".
# ponytail: tabela manual, só cobre estes termos (gíria nova continua errando) e é frágil: o MT muda até entre duas conversões
# do mesmo modelo. Upgrade: MT maior (MADLAD-3B acerta mais jargão, mas ~600 ms) ou LLM; estender só com erro medido em --terms.
# termos que o PT-BR diz em inglês e o MT traduz errado: vão em CamelCase (o MT copia) e voltam ao normal depois
_ALWAYS = ("pull request", "slide", "headshot", "matchmaking", "spawn")
_NOUN = ("build", "branch", "stream")  # só depois de artigo: também são verbo ("build a house") ou outra coisa (córrego)
_PREP = ("staging",)  # só depois de on/in/to/from (senão vira "palco")
_KEEP = {t: "".join(w.capitalize() for w in t.split()) for t in (*_ALWAYS, *_NOUN, *_PREP)} | {"headshot": "HeadShot"}
_BACK = {v: k for k, v in _KEEP.items()}
_DEV = re.compile(r"\b(?:prod|production|staging|servers?|cloud|apps?|builds?|pipeline|code|versions?|branch|fix|release)\b", _I)  # deploy militar fica
_REL = {"deploy": "release", "deploys": "releases", "deployed": "released", "deploying": "releasing"}


def _keep(m: re.Match) -> str:
    return m.group(1) + _KEEP[m.group(2).lower()] + m.group(3)


def _back(m: re.Match) -> str:
    """Devolve o termo em inglês (minúsculo, menos no começo de frase)."""
    t = _BACK[m.group(1)] + m.group(2)
    return t.capitalize() if m.start() == 0 or m.string[m.start() - 2:m.start()] in (". ", "! ", "? ") else t


_PRE = [  # reescreve o inglês p/ algo que o MT acerta
    (re.compile(r"€\s?(\d[\d.,]*)"), r"\1 euros"),  # € e £ não existem no vocabulário de entrada: o valor ficaria sem moeda
    (re.compile(r"£\s?(\d[\d.,]*)"), r"\1 pounds"),
    (re.compile(r"\bdeploy(?:s|ed|ing)?\b", _I), lambda m: _REL[m.group().lower()] if _DEV.search(m.string) else m.group()),
    (re.compile(r"()\b(%s)(e?s?)\b" % "|".join(_ALWAYS), _I), _keep),
    (re.compile(r"\b((?:the|an?|my|your|our|this|that|new|each|every|last|next) )(%s)(e?s?)\b" % "|".join(_NOUN), _I), _keep),
    (re.compile(r"\b((?:on|in|to|from) )(%s)()\b" % "|".join(_PREP), _I), _keep),
    (re.compile(r"\b(a|the|that|this|my) rage[- ]?quit\b", _I), r"\1 angry exit"),  # substantivo
    (re.compile(r"\brage[- ]?quit(s|ting)?(?= (?:the|an?|my|this|that|his|her|their|our|your)\b)", _I), r"angrily quit\1"),
    (re.compile(r"\brage[- ]?quit(s|ting)?\b", _I), r"quit\1 the game angrily"),
    (re.compile(r"\bunmute (button|icon|key|option)\b", _I), r"microphone \1"),
    (re.compile(r"\bunmute(?: (?:your(?:self|selves)|themselves|myself))?(?: (?:your|their|my|the) (?:mics?|microphones?))?\b", _I),
     "turn the mic on"),
    (re.compile(r"\bunmuted\b", _I), "with the microphone turned on"),
    (re.compile(r"\bmute your(?:self|selves)\b", _I), "turn the mic off"),
    (re.compile(r"\blow (?:on )?health\b", _I), "low on life"),
]
_POST = [  # (condição na fonte | None, padrão na saída, troca)
    (None, re.compile(r"\b(%s)(e?s?)\b" % "|".join(_BACK)), _back),
    (re.compile(r"\bpull requests?\b", _I), re.compile(r"\b(?:pedido|solicitaç(?:ão|ões))s? de (?:retirada|pull|puxar|extração)s?\b"),
     lambda m: "pull requests" if m.group().split()[0].endswith(("s", "ões")) else "pull request"),  # o MT às vezes ignora o CamelCase
    (None, re.compile(r"\bfixe\b", _I), lambda m: "Legal" if m.group()[0].isupper() else "legal"),  # PT-PT que escapa do >>pob<<
    (re.compile(r"\bbugs?\b", _I), re.compile(r"\binsetos?\b", _I), lambda m: "bugs" if m.group().lower().endswith("s") else "bug"),
    (re.compile(r"\bdeploy\b", _I), re.compile(r"\b(?:destacamento|serviço|desdobramento)\b", _I), "deploy"),
    (re.compile(r"^(?=.*\bdrivers?\b)(?=.*\b(?:update[ds]?|install\w*|gpu|nvidia|amd|windows|graphics|usb|bios|printer)\b)", _I | re.S),
     re.compile(r"\bmotoristas?\b", _I), lambda m: "drivers" if m.group().lower().endswith("s") else "driver"),  # driver de PC
    (re.compile(r"\bnerf", _I), re.compile(r"\bnerv(ar|aram|ou|ado|ados|ando|ei)\b", _I), r"nerf\1"),
    (re.compile(r"\bDiscord\b"), re.compile(r"\bDiscórdia\b"), "Discord"),  # nome da plataforma
    (re.compile(r"\bon mute\b", _I), re.compile(r"\bem silêncio\b"), "no mudo"),
    (re.compile(r"\blag\b", _I), re.compile(r"\batrasos?\b", _I), lambda m: "lags" if m.group().lower().endswith("s") else "lag"),
    (re.compile(r"\blagg(?:ed|ing|y)\b", _I), re.compile(r"\b(est[áa]|estão|continua|fica|ficou)\s+atrasad[oa]s?\b"),
     lambda m: m.group(1) + " com lag"),
]


def ensure_model(models_dir: str = "models", on_progress=None) -> str:
    """Idempotente: baixa o modelo oficial (~860 MB, só na 1ª vez), converte e guarda em <models_dir>/mt-en-pt.
    on_progress(frac | None): só quando há download (None = sem % conhecido, ex.: convertendo)."""
    d = Path(models_dir) / "mt-en-pt"
    if all((d / f).exists() for f in _FILES):
        return str(d)
    setup_cuda_dlls()  # antes de importar o ctranslate2
    from ctranslate2.converters import OpusMTConverter
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=d.parent) as tmp:  # tudo na pasta temporária: queda no meio não deixa modelo pela metade
            tmp = Path(tmp)
            print("Baixando o modelo de tradução (~860 MB, só na 1a vez)...", flush=True)
            with urllib.request.urlopen(ZIP_URL, timeout=60) as r, open(tmp / "m.zip", "wb") as f:
                total, got = int(r.headers.get("Content-Length") or 0), 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if on_progress:
                        on_progress(got / total if total else None)
            with open(tmp / "m.zip", "rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != ZIP_SHA256:
                    raise ValueError("checksum do download não confere")
            if on_progress:
                on_progress(None)  # convertendo (~1 min)
            zipfile.ZipFile(tmp / "m.zip").extractall(tmp / "src")
            OpusMTConverter(str(tmp / "src")).convert(str(tmp / "out"), quantization="float16")
            for name in ("source.spm", "target.spm", "README.md", "LICENSE"):
                if (tmp / "src" / name).exists():
                    shutil.copy(tmp / "src" / name, tmp / "out" / name)
            (tmp / "out" / "info.json").write_text(json.dumps({"name": "opus-mt-tc-big-en-pt", "url": ZIP_URL, "lang": LANG,
                                                               "license": "CC-BY-4.0"}), encoding="utf-8")
            shutil.rmtree(d, ignore_errors=True)  # sobra de execução anterior
            shutil.move(str(tmp / "out"), str(d))
    except Exception as e:  # sem rede, servidor fora do ar, disco cheio...
        raise RuntimeError(f"Não foi possível preparar o modelo de tradução: {type(e).__name__}: {e}") from e
    return str(d)


def _split(text: str) -> list[str]:
    """Quebra em frases (. ? !). 'Mr.', 'U.S.' e reticências soltas ficam grudadas na vizinha."""
    out: list[str] = []
    for p in _SPLIT.split(text):
        if out and (not re.search(r"\w", p) or (out[-1].endswith(".") and len(out[-1]) <= 4)):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


class Translator:
    def __init__(self, *, model_dir: str = "models/mt-en-pt", device: str = "auto", on_progress=None):
        d = Path(model_dir)
        if not (d / "model.bin").exists() and d.name == "mt-en-pt":
            ensure_model(str(d.parent), on_progress)  # 1ª execução sem o setup.ps1: baixa agora (idempotente)
        if not (d / "model.bin").exists():
            raise FileNotFoundError(f"Modelo de tradução ausente em {d}; rode app.mt.ensure_model()")
        setup_cuda_dlls()
        import ctranslate2
        import sentencepiece as spm
        if device == "auto":
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        self.device = device
        # GPU: float16 (num modelo pequeno é ~1,8x mais rápido que int8_float16); CPU: int8, 8 threads (núcleos P; mais piora)
        ct = "float16" if device == "cuda" else "int8"
        if ct not in ctranslate2.get_supported_compute_types(device):
            ct = "default"  # GPU antiga etc.: o CTranslate2 escolhe
        self._tr = ctranslate2.Translator(str(d), device=device, compute_type=ct,
                                          intra_threads=0 if device == "cuda" else min(8, os.cpu_count() or 4))
        self._sp_in = spm.SentencePieceProcessor(model_file=str(d / "source.spm"))
        self._sp_out = spm.SentencePieceProcessor(model_file=str(d / "target.spm"))

    def warmup(self) -> None:
        self.translate("Hello there, how are you doing today?")

    def _tokens(self, s: str) -> list[str]:
        """Frase -> tokens do Marian com o token de idioma; [] se não sobrar nada traduzível."""
        sp = self._sp_in
        for pat, rep in _PRE:
            s = pat.sub(rep, s)
        ids = sp.encode(s)
        if sp.unk_id() in ids:
            # caractere fora do vocabulário (ë, ç, €, ♪, CJK…): tira o acento e descarta o resto, senão sai " ⁇ "
            s = "".join(c for c in unicodedata.normalize("NFKD", s)
                        if not unicodedata.combining(c) and sp.unk_id() not in sp.encode(c))
            ids = sp.encode(s)
        return [LANG, *map(sp.id_to_piece, ids), "</s>"] if ids else []

    def _decode(self, src: list[str], hyp: list[str]) -> str:
        """Tokens do alvo -> texto. O vocabulário de saída do OPUS não tem À Â Ê Í Ó Ô Ú maiúsculos: eles viram <unk>
        ("Ótimo" -> "timo", "Às vezes" -> "s vezes"). Põe a letra que o próprio modelo acha mais provável (score_batch);
        só custa algo quando aparece <unk> (~15 ms)."""
        if "<unk>" not in hyp:
            return self._sp_out.decode(hyp)
        text = "".join(_UNK if p == "<unk>" else p for p in hyp).replace("▁", " ")
        while _UNK in text:
            i = text.index(_UNK)
            if (i and text[i - 1].isalpha()) or not text[i + 1:i + 2].islower():  # meio de palavra (ü, ñ...) ou símbolo: descarta
                text = text[:i] + text[i + 1:]
                continue
            cands = [self._sp_out.encode((text[:i] + c + text[i + 1:]).strip(), out_type=str) for c in _ACC]
            sc = self._tr.score_batch([src] * len(_ACC), cands)
            best = max(range(len(_ACC)), key=lambda k: sum(sc[k].log_probs))
            text = text[:i] + _ACC[best].upper() + text[i + 1:]
        return text

    def translate(self, text: str) -> str:
        """EN -> PT-BR. Aceita parcial (sem pontuação final, cortado, minúsculo); várias frases vão em lote."""
        text = " ".join(text.split())
        if not re.search(r"\w", text):
            return text  # vazio ou só pontuação
        src = [t for t in map(self._tokens, _split(text)) if t]
        if not src:
            return ""
        res = self._tr.translate_batch(src, beam_size=1, max_decoding_length=2 * max(map(len, src)) + 10)
        # o OPUS foi treinado com legendas: tira o travessão de diálogo que ele põe no começo de frases curtas
        out = " ".join(re.sub(r"^[-–—]\s*(?=[^\d\s])", "", self._decode(s, r.hypotheses[0])) for s, r in zip(src, res))
        for cond, pat, rep in _POST:
            if cond is None or cond.search(text):
                out = pat.sub(rep, out)
        out = " ".join(out.split())
        if not _END.search(text):
            out = re.sub(r"[.…]+$", "", out)  # parcial/sem pontuação final: não inventa ponto final nem reticências
        return out


if __name__ == "__main__":  # check executável: python -m app.mt
    import time
    t = time.perf_counter()
    tr = Translator(model_dir=ensure_model())
    tr.warmup()
    print(f"[{tr.device}] pronto em {time.perf_counter() - t:.1f} s")
    assert tr.translate("") == "" and tr.translate("  ...  ") == "..."
    pt = tr.translate("Hey guys, can you hear me? I'm on the bus and my cell phone is dead. We lost the round!")
    print(pt)
    assert pt.count("?") == 1 and pt.count("!") == 1 and "ônibus" in pt and "celular" in pt
    part = tr.translate("so what I was thinking is that we could maybe")  # parcial: sem ponto inventado
    print(part)
    assert not part.endswith(".") and "⁇" not in tr.translate("Zoë met Müller at the café ♪")
    assert "pull request" in tr.translate("Can you review the pull request?").lower()
    assert "desmist" not in tr.translate("Can you unmute yourself?").lower()
    assert tr.translate("Great.").startswith("Ótimo") and tr.translate("At times, it works.").startswith("Às vezes")  # <unk> de maiúscula
    t = time.perf_counter()
    tr.translate("So yesterday I was playing with my friends and the server crashed, we lost all our progress, and honestly I'm so done with this game.")
    print(f"25 palavras: {(time.perf_counter() - t) * 1000:.0f} ms")
    print("ok")
