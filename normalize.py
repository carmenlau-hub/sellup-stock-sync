"""Text and specification normalisation shared by the POS and SellUp parsers.

The two systems describe the same device very differently:

    POS     "S25 ULTRA 256GB/12 5G-S948B PRIMARY"   colour "TITANIUM BLACK"
    SellUp  model "Galaxy S25 Ultra"  specs "12/256GB"  colour "Titanium Black"

Everything in this module exists to reduce both sides to a comparable
``DeviceSpec`` so the matcher can work on structured fields instead of raw
strings.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import config

# --------------------------------------------------------------------------
# Basic string helpers
# --------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]+")


def clean(value: object) -> str:
    """Collapse a raw cell value to a trimmed, single-spaced string."""
    if value is None:
        return ""
    text = str(value)
    # Normalise unicode dashes / non-breaking spaces that creep in from exports.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


def upper(value: object) -> str:
    """Clean and upper-case a value."""
    return clean(value).upper()


def alnum_key(value: object) -> str:
    """Aggressive key: upper-case, punctuation stripped, single-spaced.

    ``"Galaxy S25 Ultra"`` and ``"GALAXY  S25-ULTRA"`` both become
    ``"GALAXY S25 ULTRA"``.
    """
    text = upper(value)
    text = _NON_ALNUM_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def squash(value: object) -> str:
    """Key with all spaces removed -- used as a last-resort comparison."""
    return alnum_key(value).replace(" ", "")


# --------------------------------------------------------------------------
# Capacity / RAM / network parsing
# --------------------------------------------------------------------------

# POS writes storage first: "256GB/12", "1TB/16", or a bare "128GB".
_POS_CAP_RAM_RE = re.compile(r"\b(\d+)\s*(GB|TB)\s*/\s*(\d+)\b")
_BARE_CAP_RE = re.compile(r"\b(\d+)\s*(GB|TB)\b")

# SellUp writes RAM first: "12/256GB", "16/1TB", or a bare "256GB".
_SELLUP_RAM_CAP_RE = re.compile(r"\b(\d+)\s*/\s*(\d+)\s*(GB|TB)\b")

# Watch case size, e.g. "40mm", "44MM".
_CASE_SIZE_RE = re.compile(r"\b(\d{2})\s*MM\b")

_NETWORK_TOKENS = ("5G", "4G", "LTE", "WIFI", "WI-FI", "BLUETOOTH", "GPS", "CELL")

# Trailing POS model code such as "-S948B", "-A376B", "-F976".
_MODEL_CODE_RE = re.compile(r"-[A-Z]{0,2}\d{2,4}[A-Z]?\b")


def to_gb(amount: int, unit: str) -> int:
    """Convert a capacity to gigabytes so 1TB and 1024GB compare equal."""
    return amount * 1024 if unit.upper() == "TB" else amount


def normalise_network(token: str) -> str:
    """Fold equivalent connectivity names onto one canonical token.

    LTE and 4G are the same thing; on watches Bluetooth and Wi-Fi are the
    non-cellular option, while GPS+Cellular and LTE are the cellular one.
    """
    t = upper(token).replace("-", "").replace(" ", "")
    if t in {"4G", "LTE"}:
        return "4G"
    if t in {"WIFI", "BLUETOOTH", "BLUETOOTHWIFI", "GPS"}:
        return "WIFI"
    if t in {"CELLULAR", "CELL", "GPSCELLULAR", "WIFICELLULAR"}:
        return "CELLULAR"
    return t


@dataclass(frozen=True)
class DeviceSpec:
    """Structured view of a device variant, comparable across both systems."""

    base: str = ""            # model name with specs stripped out
    storage_gb: int | None = None
    ram_gb: int | None = None
    network: str = ""         # canonical connectivity token
    case_size_mm: int | None = None
    channel: str = ""         # PRIMARY | TELCO | ""
    activation: str = ""      # NA | A | ""
    raw: str = ""

    @property
    def storage_label(self) -> str:
        """Human-readable storage for display in the review table."""
        if self.storage_gb is None:
            return ""
        if self.storage_gb >= 1024 and self.storage_gb % 1024 == 0:
            return f"{self.storage_gb // 1024}TB"
        return f"{self.storage_gb}GB"

    @property
    def connectivity_label(self) -> str:
        """Human-readable connectivity for display in the review table."""
        parts: list[str] = []
        if self.case_size_mm:
            parts.append(f"{self.case_size_mm}mm")
        if self.network:
            parts.append(self.network)
        return ", ".join(parts)

    def identity(self) -> tuple:
        """The tuple that must be equal for two specs to be the same variant."""
        return (
            squash(self.base),
            self.storage_gb,
            self.ram_gb,
            self.network,
            self.case_size_mm,
        )

    def loose_identity(self) -> tuple:
        """Identity ignoring RAM -- SellUp often omits RAM where POS states it."""
        return (
            squash(self.base),
            self.storage_gb,
            self.network,
            self.case_size_mm,
        )


def _strip_us_charger(text: str) -> tuple[str, bool]:
    """Remove the OnePlus 'bundled US charger' phrase before export detection."""
    stripped = text
    found = False
    for phrase in config.US_CHARGER_PHRASES:
        if phrase in stripped:
            stripped = stripped.replace(phrase, " ")
            found = True
    return _WHITESPACE_RE.sub(" ", stripped).strip(), found


def parse_pos_model(model: object) -> DeviceSpec:
    """Decompose a POS model string into a :class:`DeviceSpec`.

    ``"S25 ULTRA 256GB/12 5G-S948B PRIMARY"`` yields base ``"S25 ULTRA"``,
    storage 256, RAM 12, network ``"5G"``, channel ``"PRIMARY"``.
    """
    raw = upper(model)
    work = raw

    # 1. Channel suffix.
    channel = ""
    for suffix in (config.CHANNEL_PRIMARY, config.CHANNEL_TELCO):
        if re.search(rf"\b{suffix}$", work):
            channel = suffix
            work = re.sub(rf"\b{suffix}$", "", work).strip()
            break

    # 2. Activation token -- must be tested AFTER the channel is removed so
    #    "16 128GB NA PRIMARY" is handled, and before anything else eats it.
    activation = ""
    if re.search(r"\bNA$", work):
        activation = config.ACTIVATION_NOT_ACTIVATED
        work = re.sub(r"\bNA$", "", work).strip()
    elif re.search(r"\bA$", work):
        activation = config.ACTIVATION_ACTIVATED
        work = re.sub(r"\bA$", "", work).strip()

    # 3. Trailing internal model code (-S948B) carries no matching value.
    work = _MODEL_CODE_RE.sub(" ", work)

    # 4. US-charger phrase is part of the identity, keep a marker for it.
    work, has_us_charger = _strip_us_charger(work)

    # 5. Storage / RAM.
    storage_gb = ram_gb = None
    m = _POS_CAP_RAM_RE.search(work)
    if m:
        storage_gb = to_gb(int(m.group(1)), m.group(2))
        ram_gb = int(m.group(3))
        work = work[: m.start()] + " " + work[m.end():]
    else:
        m = _BARE_CAP_RE.search(work)
        if m:
            storage_gb = to_gb(int(m.group(1)), m.group(2))
            work = work[: m.start()] + " " + work[m.end():]

    # 6. Watch case size.
    case_size_mm = None
    m = _CASE_SIZE_RE.search(work)
    if m:
        case_size_mm = int(m.group(1))
        work = work[: m.start()] + " " + work[m.end():]

    # 6b. Watch case / band description.
    #     POS spells a watch out in full:
    #       "SERIES 11 42MM GPS JET BLACK ALUMINIUM CASE BLACK SPORT BAND M/L"
    #     while SellUp lists it as "Watch Series 11 Aluminium". Everything from
    #     "CASE" onwards describes the strap and finish, so it is dropped --
    #     but the material immediately preceding it (ALUMINIUM, TITANIUM) is
    #     kept because SellUp uses it to distinguish models.
    case_pos = work.find(" CASE")
    if case_pos != -1:
        head = work[:case_pos].strip()
        material = ""
        for candidate in ("ALUMINIUM", "ALUMINUM", "TITANIUM", "STAINLESS STEEL", "CERAMIC"):
            if candidate in head:
                material = "TITANIUM" if candidate == "TITANIUM" else candidate
                head = head.replace(candidate, " ")
                break
        work = f"{head} {material}".strip()

    # 7. Network token.
    network = ""
    for token in _NETWORK_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", work):
            network = normalise_network(token)
            work = re.sub(rf"\b{re.escape(token)}\b", " ", work)
            break
    # A watch described as "CELL" is cellular regardless of the token order.
    if re.search(r"\bCELL\b", work):
        network = "CELLULAR"
        work = re.sub(r"\bCELL\b", " ", work)

    base = alnum_key(work)
    if has_us_charger:
        base = f"{base} USCHARGER".strip()

    return DeviceSpec(
        base=base,
        storage_gb=storage_gb,
        ram_gb=ram_gb,
        network=network,
        case_size_mm=case_size_mm,
        channel=channel,
        activation=activation or config.DEFAULT_ACTIVATION,
        raw=raw,
    )


def parse_sellup_specs(model: object, specs: object) -> DeviceSpec:
    """Decompose a SellUp ``Model`` + ``Specs`` pair into a :class:`DeviceSpec`.

    Specs look like ``"12/256GB"``, ``"256GB, Wi-Fi + Cellular"`` or
    ``"40mm, GPS"``. The model itself is left as the base.
    """
    raw_model = upper(model)
    raw_specs = upper(specs)

    storage_gb = ram_gb = None
    case_size_mm = None
    network = ""

    # Specs is a comma-separated list; handle each fragment independently.
    for part in [p.strip() for p in raw_specs.split(",") if p.strip()]:
        m = _SELLUP_RAM_CAP_RE.search(part)
        if m:
            ram_gb = int(m.group(1))
            storage_gb = to_gb(int(m.group(2)), m.group(3))
            continue
        m = _BARE_CAP_RE.search(part)
        if m and storage_gb is None:
            storage_gb = to_gb(int(m.group(1)), m.group(2))
            continue
        m = _CASE_SIZE_RE.search(part)
        if m:
            case_size_mm = int(m.group(1))
            # A watch fragment can be "40mm" alone; connectivity is separate.
            remainder = part[: m.start()] + part[m.end():]
            if remainder.strip():
                network = network or normalise_network(remainder)
            continue
        # Anything else in the specs list is connectivity.
        if "CELLULAR" in part:
            network = "CELLULAR"
        elif part:
            network = network or normalise_network(part)

    # Strip a leading brand word that SellUp repeats in the model
    # ("Apple iPhone 17 Pro" -> "IPHONE 17 PRO" is kept; brand is matched
    # separately, so only exact duplication is a problem in practice).
    base = alnum_key(raw_model)

    return DeviceSpec(
        base=base,
        storage_gb=storage_gb,
        ram_gb=ram_gb,
        network=network,
        case_size_mm=case_size_mm,
        raw=f"{raw_model} | {raw_specs}".strip(" |"),
    )


# --------------------------------------------------------------------------
# Colour normalisation
# --------------------------------------------------------------------------

# Marketplace colour names are frequently abbreviated. Expansions are applied
# before comparison so "Awes.Lilac" reaches "AWESOME LILAC".
_COLOUR_ABBREVIATIONS: dict[str, str] = {
    "AWES": "AWESOME",
    "TITAN": "TITANIUM",
    "TIT": "TITANIUM",
    "BLK": "BLACK",
    "WHT": "WHITE",
    "SLV": "SILVER",
    "GLD": "GOLD",
    "GRY": "GREY",
    "GRN": "GREEN",
    "BLU": "BLUE",
    "LAV": "LAVENDER",
    "MID": "MIDNIGHT",
    "NAT": "NATURAL",
    "PHANT": "PHANTOM",
    "SPC": "SPACE",
}

# Spelling variants that mean the same colour.
_MODEL_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("TYPE C", "USBC"),
    ("TYPE-C", "USBC"),
    ("USB C", "USBC"),
    ("USB-C", "USBC"),
)


def apply_model_synonyms(text: str) -> str:
    """Fold connector-name variants so 'Type C' and 'USB-C' compare equal."""
    out = upper(text)
    for src, dst in _MODEL_SYNONYMS:
        out = out.replace(src, dst)
    return out


_COLOUR_SYNONYMS: dict[str, str] = {
    "GRAY": "GREY",
    "BLUEBLACK": "BLUE BLACK",
    "JETBLACK": "JET BLACK",
    "OFFWHITE": "OFF WHITE",
}

# Filler words that add nothing to a colour comparison.
_COLOUR_NOISE: frozenset[str] = frozenset({"COLOR", "COLOUR", "EDITION", "FINISH"})


def normalise_colour(value: object) -> str:
    """Reduce a colour name to a canonical comparable form."""
    text = upper(value).replace(".", " ").replace("/", " ")
    text = _NON_ALNUM_RE.sub(" ", text)
    words: list[str] = []
    for word in text.split():
        if word in _COLOUR_NOISE:
            continue
        word = _COLOUR_ABBREVIATIONS.get(word, word)
        word = _COLOUR_SYNONYMS.get(word, word)
        words.append(word)
    joined = " ".join(words)
    # Apply multi-word synonyms after the word pass.
    for src, dst in _COLOUR_SYNONYMS.items():
        joined = joined.replace(src, dst)
    return _WHITESPACE_RE.sub(" ", joined).strip()


def colour_key(value: object) -> str:
    """Space-free colour key so 'BLUE BLACK' == 'BLUEBLACK'."""
    return normalise_colour(value).replace(" ", "")


def colours_match(a: object, b: object) -> bool:
    """True when two colour names refer to the same finish.

    Falls back to a containment test so ``"TITANIUM BLACK"`` matches
    ``"BLACK TITANIUM"`` only when one is a strict superset of the other's
    word set -- never on a single shared word like ``"BLACK"``.
    """
    ka, kb = colour_key(a), colour_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    wa = set(normalise_colour(a).split())
    wb = set(normalise_colour(b).split())
    if not wa or not wb:
        return False
    # Same words in a different order.
    if wa == wb:
        return True
    # One is a subset of the other AND the subset has more than one word,
    # which avoids "BLACK" swallowing "TITANIUM BLACK".
    if wa < wb or wb < wa:
        return min(len(wa), len(wb)) > 1
    return False


# --------------------------------------------------------------------------
# Brand normalisation
# --------------------------------------------------------------------------

# POS brands are granular (SAMSUNG WATCH, HONOR TABLET, IPHONE, IPAD) whereas
# SellUp uses a single manufacturer name per row. Mapping both onto a common
# manufacturer lets the matcher compare like with like.
_POS_BRAND_TO_MAKER: dict[str, str] = {
    "IPHONE": "APPLE",
    "IPAD": "APPLE",
    "APPLE": "APPLE",
    "APPLE WATCH": "APPLE",
    "SAMSUNG": "SAMSUNG",
    "SAMSUNG WATCH": "SAMSUNG",
    "HONOR": "HONOR",
    "HONOR TABLET": "HONOR",
    "OPPO": "OPPO",
    "OPPO WATCH": "OPPO",
    "GOOGLE": "GOOGLE",
    "GOOGLE WATCH": "GOOGLE",
    "ONE PLUS": "ONEPLUS",
    "ONEPLUS": "ONEPLUS",
    "XIAOMI": "XIAOMI",
    "NOTHING": "NOTHING",
    "SONY": "SONY",
    "LENOVO": "LENOVO",
    "HUAWEI": "HUAWEI",
    "DJI": "DJI",
}


def maker(brand: object) -> str:
    """Map a POS or SellUp brand onto a canonical manufacturer name."""
    b = upper(brand)
    if b in _POS_BRAND_TO_MAKER:
        return _POS_BRAND_TO_MAKER[b]
    return b.replace(" ", "")


# --------------------------------------------------------------------------
# Model-family hints
# --------------------------------------------------------------------------

# POS drops the manufacturer's marketing family from the model ("S25 ULTRA"),
# while SellUp keeps it ("Galaxy S25 Ultra"). These prefixes are stripped from
# the SellUp side so the two bases line up.
_FAMILY_PREFIXES: tuple[str, ...] = (
    "GALAXY",
    "APPLE",
    "REDMI",
    "POCO",
    "WATCH",
)


def strip_family_prefix(base: str) -> str:
    """Drop a leading marketing-family word from a SellUp model base."""
    words = base.split()
    while words and words[0] in _FAMILY_PREFIXES:
        words = words[1:]
    return " ".join(words) if words else base


# --------------------------------------------------------------------------
# Device kind
# --------------------------------------------------------------------------

# SellUp only sells four kinds of hardware, one per worksheet. Classifying the
# POS side the same way stops a MacBook being suggested as an iPad purely
# because they share a storage size and a colour name.
KIND_PHONE = "phone"
KIND_TABLET = "tablet"
KIND_WATCH = "watch"
KIND_AUDIO = "audio"
KIND_COMPUTER = "computer"    # never sold on SellUp
KIND_ACCESSORY = "accessory"  # never sold on SellUp
KIND_UNKNOWN = "unknown"

SHEET_TO_KIND: dict[str, str] = {
    "Smartphones": KIND_PHONE,
    "Tablets": KIND_TABLET,
    "Smartwatches": KIND_WATCH,
    "Audio": KIND_AUDIO,
}

# Kinds SellUp has no worksheet for.
UNSELLABLE_KINDS: frozenset[str] = frozenset({KIND_COMPUTER, KIND_ACCESSORY})

_AUDIO_WORDS = (
    "AIRPODS", "BUDS", "HEADPHONE", "HEADSET", "EARPHONE", "EARBUD",
    "SPEAKER", "SOUNDBAR", "WF-", "WH-", "FREEBUDS", "NOTHING EAR",
)
_COMPUTER_WORDS = (
    "MACBOOK", "IMAC", "MAC MINI", "MAC STUDIO", "MAC PRO", "IDEAPAD",
    "THINKPAD", "LAPTOP", "NOTEBOOK", "CHROMEBOOK", "SURFACE LAPTOP",
)
_ACCESSORY_WORDS = (
    "PENCIL", "CHARGER", "ADAPTER", "CABLE", "COVER", "PROTECTOR",
    "KEYBOARD", "MOUSE", "STYLUS", "POWER BANK", "POWERBANK", "DOCK",
    "GIMBAL", "TRIPOD", "BAND ONLY", "STRAP ONLY", "PHONE CASE",
)
_WATCH_WORDS = ("WATCH", "GALAXY FIT", "MI BAND", "SMARTWATCH")
_TABLET_WORDS = ("IPAD", "TAB ", "TABLET", "MATEPAD", "PAD ")


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def device_kind(brand: object, model: object) -> str:
    """Classify a POS row into one of the SellUp device kinds.

    Brand is checked first because POS encodes the kind there for watches and
    tablets (``SAMSUNG WATCH``, ``HONOR TABLET``, ``IPAD``); the model string
    disambiguates the rest.
    """
    b = upper(brand)
    m = upper(model)
    # "ONE PLUS 15 512GB/16 5G W US 80W CHARGER" is a phone sold with a charger
    # in the box, not a charger. Strip the bundle phrase before classifying.
    for phrase in config.US_CHARGER_PHRASES:
        m = m.replace(phrase, " ")
    combined = f"{b} {m}"

    # Computers are ruled out first: no SellUp worksheet sells them.
    if _contains_any(combined, _COMPUTER_WORDS):
        return KIND_COMPUTER

    # Watches are tested before accessories. A POS watch model reads
    # "SERIES 11 42MM GPS JET BLACK ALUMINIUM CASE BLACK SPORT BAND M/L" --
    # the words "CASE" and "BAND" describe the watch itself, not a strap
    # being sold separately, so the brand is the reliable signal.
    if b.endswith("WATCH") or _contains_any(combined, _WATCH_WORDS):
        return KIND_WATCH

    if _contains_any(combined, _ACCESSORY_WORDS):
        return KIND_ACCESSORY

    if _contains_any(combined, _AUDIO_WORDS):
        return KIND_AUDIO
    if b in {"IPAD"} or b.endswith("TABLET") or _contains_any(combined, _TABLET_WORDS):
        return KIND_TABLET
    if b in {"IPHONE"}:
        return KIND_PHONE

    # A bare handset brand with a capacity is almost always a phone.
    return KIND_PHONE
