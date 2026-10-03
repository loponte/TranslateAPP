"""Tipos compartilhados entre os módulos (contrato). Não editar sem avisar o orquestrador."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

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
    final=True com en == "" significa "descarte essa linha" (alucinação/ruído)."""
    utt_id: int
    speaker: int | None  # índice 0-based; None = ainda desconhecido
    en: str
    pt: str
    final: bool
    t0: float
    t_end: float         # fim do áudio contemplado por este update
    t_ready: float       # time.monotonic() quando o texto ficou pronto; atraso = t_ready - t_end


@dataclass
class Status:
    """Mensagem de status (pt-BR) para a barra de status da UI."""
    text: str
    level: Literal["info", "warn", "error", "ready", "download"] = "info"
    progress: float | None = None  # 0..1 em "download"; None = indeterminado
