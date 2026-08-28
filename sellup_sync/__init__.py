"""SellUp bulk stock update tool for Mister Mobile.

Reads a POS masterlist export and a SellUp bulk inventory template, applies the
confirmed SKU links held in the registry, and writes quantities back into the
SellUp file touching only columns G, I and K.
"""

from __future__ import annotations

__version__ = "3.0.0"

from . import (
    config,
    discriminators,
    inventory,
    matching,
    normalize,
    pipeline,
    pos,
    registry,
    seed,
)

__all__ = [
    "config",
    "discriminators",
    "inventory",
    "matching",
    "normalize",
    "pipeline",
    "pos",
    "registry",
    "seed",
]
