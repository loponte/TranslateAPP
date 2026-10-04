"""Tipos compartilhados entre os módulos (contrato). Não editar sem avisar o orquestrador."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Lang = Literal["en", "pt"]  # idiomas suportados (falado e legenda)

SR = 16_000  # Hz; todo áudio interno é mono float32 em [-1, 1] a 16 kHz


@dataclass
class SegEvent:
    """Saída do Segmenter. `audio` = áudio da utterance até agora (partial) ou completo (final)."""
    kind: Literal["start", "partial", "final"]
    utt_id: int
    audio: np.ndarray | None  # None em "start"
    t0: float                 # time.monotonic() em que a fala começou
    t_end: float              # time.monotonic() da última amostra incluída em `audio`


@dataclass
class Update:
    """Saída do Pipeline -> UI. Mesmo utt_id = mesma linha (a parcial é substituída pela final).
    final=True com orig == "" significa "descarte essa linha" (alucinação/ruído)."""
    utt_id: int
    speaker: int | None  # id estável >= 0 (pode passar de 7 quando há perfis salvos); None = desconhecido/provisório
    orig: str            # transcrição no idioma falado (era `en`)
    sub: str             # legenda no idioma escolhido (era `pt`); = orig se o falado já é o da legenda; "" se a MT falhou (UI mostra orig)
    final: bool
    t0: float
    t_end: float         # fim do áudio contemplado por este update
    t_ready: float       # time.monotonic() quando o texto ficou pronto; atraso = t_ready - t_end
    lang: str = "en"     # idioma falado desta linha (detectado ou fixado): "en" | "pt"


@dataclass
class Status:
    """Mensagem de status (pt-BR) para a barra de status da UI."""
    text: str
    level: Literal["info", "warn", "error", "ready", "download"] = "info"
    progress: float | None = None  # 0..1 em "download"; None = indeterminado
