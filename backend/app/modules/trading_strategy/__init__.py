"""Unified trading strategy package (V541 / V900 / V901).

Single source of truth for the bot's entry gates, exit ladder and
capital-defense rules. Used by BOTH the paper-trading engine and the
live executor so the whole bot follows ONE strategy regardless of the
selected sizing profile.

V901 PROFIT-FIRST (asymmetric TP + Raydium) is exported alongside V900.
The orchestrator selects V901 when settings.v901_enabled is True.
"""
from .unified import (
    unified_exit_reason,
    unified_entry_check,
    unified_defense_snapshot,
    register_unified_buy,
    UNIFIED_PARAMS,
    V901_PARAMS,
    v901_exit_reason,
    v901_position_size,
    v901_sniper_entry_check,
)

__all__ = [
    "unified_exit_reason",
    "unified_entry_check",
    "unified_defense_snapshot",
    "register_unified_buy",
    "UNIFIED_PARAMS",
    "V901_PARAMS",
    "v901_exit_reason",
    "v901_position_size",
    "v901_sniper_entry_check",
]
