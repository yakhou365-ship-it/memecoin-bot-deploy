"""Unified trading strategy package (V541).

Single source of truth for the bot's entry gates, exit ladder and
capital-defense rules. Used by BOTH the paper-trading engine and the
live executor so the whole bot follows ONE strategy regardless of the
selected sizing profile.
"""
from .unified import (
    unified_exit_reason,
    unified_entry_check,
    unified_defense_snapshot,
    register_unified_buy,
    UNIFIED_PARAMS,
)

__all__ = [
    "unified_exit_reason",
    "unified_entry_check",
    "unified_defense_snapshot",
    "register_unified_buy",
    "UNIFIED_PARAMS",
]
