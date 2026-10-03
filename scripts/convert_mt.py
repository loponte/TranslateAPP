"""Converte um modelo de tradução do Hugging Face para CTranslate2, em ambiente descartável (NÃO instale no .venv).

Plano B do app.mt.ensure_model (que já converte o zip oficial do OPUS-MT sem torch) e receita dos candidatos de
tests/eval_mt.py. Da raiz do projeto:
  uv run --no-project --with ctranslate2 --with transformers --with torch --with sentencepiece scripts/convert_mt.py [repo] [saida] [quant] [arquivos...]
Padrão: Helsinki-NLP/opus-mt-tc-big-en-pt -> models/mt-en-pt em float16 (+ source.spm target.spm). Exemplos dos candidatos:
  ... scripts/convert_mt.py NiuTrans/LMT-60-1.7B  <saida> float16  tokenizer.json tokenizer_config.json
  ... scripts/convert_mt.py Infomaniak-AI/vllm-translategemma-4b-it <saida> bfloat16 tokenizer.json tokenizer_config.json
(Gemma/T5 estouram em float16: use bfloat16.)
"""
import json
import sys
from pathlib import Path

from ctranslate2.converters import TransformersConverter

repo = sys.argv[1] if len(sys.argv) > 1 else "Helsinki-NLP/opus-mt-tc-big-en-pt"  # CC-BY-4.0; token >>pob<< = pt-BR
out = Path(sys.argv[2] if len(sys.argv) > 2 else "models/mt-en-pt")
quant = sys.argv[3] if len(sys.argv) > 3 else "float16"
copy = sys.argv[4:] or ["source.spm", "target.spm"]  # tokenizador fica junto do modelo
TransformersConverter(repo, copy_files=copy).convert(str(out), quantization=quant, force=True)
(out / "info.json").write_text(json.dumps({"name": repo.split("/")[-1], "repo": repo, "quantization": quant}), encoding="utf-8")
print("modelo salvo em", out)
