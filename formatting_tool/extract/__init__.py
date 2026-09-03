"""Stage 1-2: read decks and derive the spec they are measured against."""

from .deck_reader import DeckReadError, read_deck
from .master_spec import derive_master_spec

__all__ = ["read_deck", "DeckReadError", "derive_master_spec"]
