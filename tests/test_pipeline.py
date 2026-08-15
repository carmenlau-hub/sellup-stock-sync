"""Regression tests for the parsing and matching rules.

Run with:  python -m pytest tests/ -v

These are pure-logic tests with no file fixtures, so they run anywhere and
guard the rules that are expensive to get wrong: which POS column is read,
which SellUp column receives a quantity, and which rows are excluded.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sellup_sync import config  # noqa: E402
from sellup_sync.normalize import (  # noqa: E402
    KIND_AUDIO,
    KIND_COMPUTER,
    KIND_PHONE,
    KIND_TABLET,
    KIND_WATCH,
    colours_match,
    device_kind,
    normalise_colour,
    parse_pos_model,
    parse_sellup_specs,
    to_gb,
)
from sellup_sync.pos import _is_export_set, _is_freebie  # noqa: E402


# --------------------------------------------------------------------------
# Column mapping -- the single most costly thing to get wrong
# --------------------------------------------------------------------------

def test_writable_columns_are_g_i_k():
    assert config.COL_NA_QTY == 7    # G
    assert config.COL_A_QTY == 9     # I
    assert config.COL_EXC_QTY == 11  # K
    assert config.WRITABLE_COLUMNS == {7, 9, 11}


def test_pos_reads_column_f():
    assert config.POS_COL_AVAILABLE_QTY == 6  # F, not G


def test_slot_column_mapping():
    assert config.SLOT_TO_COLUMN[config.SLOT_NEW_NA] == config.COL_NA_QTY
    assert config.SLOT_TO_COLUMN[config.SLOT_NEW_A] == config.COL_A_QTY
    assert config.SLOT_TO_COLUMN[config.SLOT_USED_EXCELLENT] == config.COL_EXC_QTY


# --------------------------------------------------------------------------
# POS model parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,storage,ram,network,channel,activation",
    [
        ("S26 ULTRA 256GB/12 5G-S948B PRIMARY", 256, 12, "5G", "PRIMARY", "NA"),
        ("ONE PLUS 13S 512GB/12 5G", 512, 12, "5G", "", "NA"),
        ("17 PRO MAX 256GB NA", 256, None, "", "", "NA"),
        ("17 PRO MAX 256GB A", 256, None, "", "", "A"),
        ("16E 128GB NA", 128, None, "", "", "NA"),
        ("S23 ULTRA 1TB/12 5G TELCO", 1024, 12, "5G", "TELCO", "NA"),
    ],
)
def test_parse_pos_model(model, storage, ram, network, channel, activation):
    spec = parse_pos_model(model)
    assert spec.storage_gb == storage
    assert spec.ram_gb == ram
    assert spec.network == network
    assert spec.channel == channel
    assert spec.activation == activation


def test_activation_token_defaults_to_not_activated():
    """Only Apple phones carry NA/A; everything else is Not Activated."""
    assert parse_pos_model("S25 ULTRA 256GB/12 5G").activation == "NA"


def test_terabyte_normalised_to_gigabytes():
    assert to_gb(1, "TB") == 1024
    assert parse_pos_model("17 PRO 1TB NA").storage_gb == 1024


def test_watch_case_and_band_stripped():
    """The strap description must not drown out the model name."""
    spec = parse_pos_model(
        "SERIES 11 42MM GPS JET BLACK ALUMINIUM CASE BLACK SPORT BAND M/L"
    )
    assert spec.case_size_mm == 42
    assert "SPORT" not in spec.base
    assert "ALUMINIUM" in spec.base


# --------------------------------------------------------------------------
# SellUp spec parsing -- RAM and storage are the opposite way round
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,specs,storage,ram,network,case",
    [
        ("Galaxy S25", "12/256GB", 256, 12, "", None),
        ("iPhone 17 Pro Max", "1TB", 1024, None, "", None),
        ("iPad Pro 11 (M4)", "256GB, Wi-Fi + Cellular", 256, None, "CELLULAR", None),
        ("iPad Air 11-inch (M3)", "512GB, Wi-Fi", 512, None, "WIFI", None),
        ("Watch SE 3 Aluminium", "40mm, GPS", None, None, "WIFI", 40),
        ("AirPods Pro 3", "", None, None, "", None),
    ],
)
def test_parse_sellup_specs(model, specs, storage, ram, network, case):
    spec = parse_sellup_specs(model, specs)
    assert spec.storage_gb == storage
    assert spec.ram_gb == ram
    assert spec.network == network
    assert spec.case_size_mm == case


def test_pos_and_sellup_storage_agree_despite_opposite_order():
    """POS writes 256GB/12, SellUp writes 12/256GB -- same device."""
    pos_spec = parse_pos_model("S25 ULTRA 256GB/12 5G")
    sellup_spec = parse_sellup_specs("Galaxy S25 Ultra", "12/256GB")
    assert pos_spec.storage_gb == sellup_spec.storage_gb == 256
    assert pos_spec.ram_gb == sellup_spec.ram_gb == 12


# --------------------------------------------------------------------------
# Exclusions
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model",
    [
        "PIXEL 10 256GB/12 5G JP",
        "13 PRO 256GB US",
        "15 PRO 512GB CN",
        "S23 ULTRA 512GB/12 5G MY-S918B",
    ],
)
def test_export_sets_excluded(model):
    assert _is_export_set(model)


def test_us_charger_bundle_is_not_an_export_set():
    """OnePlus sets bundled with a US charger are normal Singapore stock."""
    assert not _is_export_set("ONE PLUS 15R 256GB/12 5G W US 80W CHARGER")


def test_freebies_excluded():
    assert _is_freebie("GALAXY BUDS FREEBIE")
    assert not _is_freebie("GALAXY BUDS 3 PRO")


def test_telco_is_a_separate_pool():
    """PRIMARY and TELCO must never be combined."""
    assert parse_pos_model("S25 256GB/8 5G TELCO").channel == "TELCO"
    assert parse_pos_model("S25 256GB/8 5G PRIMARY").channel == "PRIMARY"
    assert config.CHANNEL_TELCO in config.EXCLUDED_CHANNELS


# --------------------------------------------------------------------------
# Device kind -- stops laptops matching tablets
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "brand,model,expected",
    [
        ("APPLE", "MACBOOK AIR 13-INCH 2020 M1 256GB/8 A2337", KIND_COMPUTER),
        ("APPLE", "IMAC 24-INCH 2021 M1 512GB/8 A2438", KIND_COMPUTER),
        ("APPLE", "AIRPODS PRO 3", KIND_AUDIO),
        ("APPLE WATCH", "SERIES 11 42MM GPS JET BLACK ALUMINIUM CASE BLACK SPORT BAND M/L", KIND_WATCH),
        ("IPAD", "MINI 7 128GB WIFI", KIND_TABLET),
        ("SAMSUNG", "S25 ULTRA 256GB/12 5G", KIND_PHONE),
        ("ONE PLUS", "15 512GB/16 5G W US 80W CHARGER", KIND_PHONE),
    ],
)
def test_device_kind(brand, model, expected):
    assert device_kind(brand, model) == expected


# --------------------------------------------------------------------------
# Colour handling
# --------------------------------------------------------------------------

def test_colour_abbreviations_expand():
    assert normalise_colour("Awes.Lilac") == "AWESOME LILAC"


def test_colour_spelling_variants():
    assert colours_match("Space Gray", "Space Grey")
    assert colours_match("Blueblack", "Blue Black")


def test_word_order_does_not_matter():
    assert colours_match("TITANIUM BLACK", "Black Titanium")


def test_single_shared_word_is_not_a_match():
    """'BLACK' must not swallow 'TITANIUM BLACK'."""
    assert not colours_match("BLACK", "TITANIUM BLACK")


# --------------------------------------------------------------------------
# Buffer
# --------------------------------------------------------------------------

def test_anti_oversell_buffer():
    from sellup_sync.pipeline import apply_buffer

    assert apply_buffer(5, 0) == 5      # disabled
    assert apply_buffer(2, 2) == 0      # at threshold
    assert apply_buffer(3, 2) == 3      # above threshold
    assert apply_buffer(0, 2) == 0
