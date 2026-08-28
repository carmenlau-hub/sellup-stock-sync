"""Hard discriminators — attributes that must match exactly or not at all.

Bug 2 (28 Aug 2026): the crosswalk scored model tokens fuzzily, so the single
token that distinguishes two products could be outvoted by colour, storage and
RAM all agreeing. 16 wrong locks resulted.

The failure that produced 7 of those 16::

    POS     "S26 PLUS 256GB/12 5G-S947B"
    SellUp  "Galaxy S26+"   and   "Galaxy S26"

``+`` was stripped as punctuation before tokenising, so BOTH SellUp listings
collapsed to the key ``S26``. POS spells it ``PLUS``, which survives, making
the POS string a superset of both — the containment branch scored each 0.914
and the tie fell to SKU sort order.

Everything here is extracted **before** punctuation is stripped, and a
disagreement disqualifies a pair outright rather than lowering its score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Plus normalisation -- must run before any punctuation stripping
# --------------------------------------------------------------------------

_PLUS_RE = re.compile(r"\+")

# "ONE PLUS 13S" is a brand name, not a Plus variant. Collapsing the brand to
# one token stops PLUS being read as a variant suffix there. SellUp already
# writes it closed up ("OnePlus 13S"), so this aligns the two sides.
_BRAND_COLLAPSE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bONE\s+PLUS\b"), "ONEPLUS"),
)


# SellUp names the processor in brackets: "iPad mini (A17 Pro)",
# "iPad Pro 11 (M4)". That "Pro" is the chip, not a product variant, and
# "A17" is a chip, not a model number -- reading either as a discriminator
# reports a conflict against POS "MINI 7 GEN 2024", which is the same tablet.
# Only bracketed groups that start with a chip designation are dropped;
# "(1st Gen)" is a real generation marker and is kept.
_CHIP_PARENS_RE = re.compile(r"\(\s*[AM]\d+[^)]*\)")


def normalise_plus(text: object) -> str:
    """Turn ``S26+`` into ``S26 PLUS`` so the token survives tokenising.

    Applied to **model strings only**. SellUp spec strings contain
    ``"Wi-Fi + Cellular"``, where the ``+`` is a conjunction rather than a
    variant marker, so specs must not be passed through here.
    """
    out = str(text or "").upper()
    out = _CHIP_PARENS_RE.sub(" ", out)
    for pattern, replacement in _BRAND_COLLAPSE:
        out = pattern.sub(replacement, out)
    return _PLUS_RE.sub(" PLUS ", out)


# --------------------------------------------------------------------------
# Variant suffix
# --------------------------------------------------------------------------

# Order matters: "PRO MAX" is tested before "PRO", otherwise every Pro Max
# is recorded as a Pro and the two become indistinguishable.
_VARIANT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("promax", re.compile(r"\bPRO\s*MAX\b")),
    ("plus", re.compile(r"\bPLUS\b")),
    ("ultra", re.compile(r"\bULTRA\b")),
    ("pro", re.compile(r"\bPRO\b")),
    ("fe", re.compile(r"\bFE\b")),
    ("lite", re.compile(r"\bLITE\b")),
    ("edge", re.compile(r"\bEDGE\b")),
)


def variant_suffix(text: object) -> str:
    """The variant marker in a model string, or ``""`` when there is none.

    Absence is itself a value: ``Galaxy S26`` (``""``) must not match
    ``S26 PLUS`` (``"plus"``).
    """
    work = normalise_plus(text)
    for name, pattern in _VARIANT_PATTERNS:
        if pattern.search(work):
            return name
    return ""


# --------------------------------------------------------------------------
# Model number / generation
# --------------------------------------------------------------------------

# A model-number token: optional letters, digits, optional decimal, optional
# trailing letters. Matches S26, V6, X7E, 11S, 10.5, 16E.
_MODEL_NUM_RE = re.compile(r"\b([A-Z]{0,2}\d+(?:\.\d+)?[A-Z]{0,2})\b")

# Years are not model numbers.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# Network markers are handled by their own discriminator. Left in, they get
# picked up as a model number whenever no real one precedes them, which made
# "Reno15 F 5G" report its number as "5G".
_NETWORK_AS_NUMBER = frozenset({"5G", "4G", "3G", "2G", "LTE"})

# Ordinals: SellUp writes "(3rd Gen)" where POS writes "SE 3".
_ORDINAL_RE = re.compile(r"^(\d+)(ST|ND|RD|TH)$")

# A single trailing letter belongs to the number before it. POS writes
# "RENO 15F" and SellUp writes "Reno15 F" -- the same phone, tokenised
# differently. Rejoining keeps them comparable.
_SINGLE_LETTER_RE = re.compile(r"^[A-Z]$")


def _tidy_number(token: str) -> str:
    """Normalise a model-number token to its comparable form."""
    if token.endswith(".0"):          # 11.0 and 11 are one generation
        token = token[:-2]
    ordinal = _ORDINAL_RE.match(token)
    if ordinal:                        # 3RD -> 3, 9TH -> 9
        token = ordinal.group(1)
    return token


def model_numbers(text: object) -> set[str]:
    """Every model-number token in a model string.

    A set rather than a single value, because the two systems put their
    numbers in a different order and sometimes state different ones. POS
    writes ``10.2 9 GEN 2021`` (screen size first) while SellUp writes
    ``iPad 9th Gen`` (generation only). Taking the first token from each
    compared a screen size against a generation and reported a conflict on
    two listings that were the same product.
    """
    work = normalise_plus(text)
    tokens = re.split(r"[^A-Z0-9.]+", work)
    numbers: set[str] = set()

    for index, token in enumerate(tokens):
        if not token or token in _NETWORK_AS_NUMBER or _YEAR_RE.match(token):
            continue
        match = _MODEL_NUM_RE.fullmatch(token)
        if not match:
            # A number embedded in a longer word ("RENO15") still counts.
            embedded = re.search(r"(\d+(?:\.\d+)?[A-Z]{0,2})$", token)
            if not embedded:
                continue
            token = embedded.group(1)
            if token in _NETWORK_AS_NUMBER or _YEAR_RE.match(token):
                continue

        tidy = _tidy_number(token)
        # Pull in a following single letter: "Reno15" + "F" -> "15F".
        if index + 1 < len(tokens) and _SINGLE_LETTER_RE.match(tokens[index + 1] or ""):
            numbers.add(tidy + tokens[index + 1])
        numbers.add(tidy)

    return numbers


def model_number(text: object) -> str:
    """The first model-number token, for display and for simple checks."""
    work = normalise_plus(text)
    for match in _MODEL_NUM_RE.finditer(work):
        token = match.group(1)
        if _YEAR_RE.match(token) or token in _NETWORK_AS_NUMBER:
            continue
        return _tidy_number(token)
    return ""


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Network suffix
# --------------------------------------------------------------------------

# SellUp carries the network in the *model* string ("Galaxy A15 5G"), while
# the Specs column holds only RAM and storage. The old parser read network
# from Specs alone, so the SellUp side came back empty and the
# "only when both sides state it" rule skipped the comparison entirely --
# which is how ML 30516 locked a 5G handset to the 4G listing.
_NETWORK_RE = re.compile(r"\b(5G|4G|LTE)\b")


def network_suffix(text: object) -> str:
    """The network marker stated in a model string, or ``""``.

    ``LTE`` and ``4G`` are the same thing and both normalise to ``4G``.
    """
    match = _NETWORK_RE.search(normalise_plus(text))
    if not match:
        return ""
    token = match.group(1)
    return "4G" if token in {"4G", "LTE"} else token


@dataclass(frozen=True)
class Discriminators:
    """The attributes that must agree before a pair is even a candidate."""

    variant: str = ""
    number: str = ""
    network: str = ""
    numbers: frozenset[str] = frozenset()

    @classmethod
    def of(cls, text: object) -> "Discriminators":
        return cls(
            variant=variant_suffix(text),
            number=model_number(text),
            network=network_suffix(text),
            numbers=frozenset(model_numbers(text)),
        )


def conflict(a: Discriminators, b: Discriminators) -> str | None:
    """Why two model strings cannot be the same product, or ``None``.

    Variant is compared unconditionally, because absence is meaningful —
    that is the whole ``S26`` vs ``S26+`` problem.

    Model number is compared only when **both** sides have one. SellUp
    sometimes drops it entirely (``iPhone Air`` against POS ``17 AIR``), and
    rejecting on a missing token would break locks that are currently correct.
    """
    if a.variant != b.variant:
        return f"variant {a.variant or '(none)'} vs {b.variant or '(none)'}"

    # Model numbers conflict only when both sides state some and share none.
    # Requiring a shared token rather than an identical first token is what
    # keeps "10.2 9 GEN" and "iPad 9th Gen" together while still separating
    # "iPad Pro 11" from "iPad Pro 10.5".
    if a.numbers and b.numbers and not (a.numbers & b.numbers):
        return (
            f"model number {'/'.join(sorted(a.numbers))} vs "
            f"{'/'.join(sorted(b.numbers))}"
        )

    if a.network and b.network and a.network != b.network:
        return f"network {a.network} vs {b.network}"
    return None
