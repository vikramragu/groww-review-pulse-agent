"""Stages 7-8: the validation gate and the markdown renderer."""

from pulse.rendering.render import render_note
from pulse.rendering.validate import (
    ValidationFailure,
    ValidationResult,
    count_note_words,
    validate_note,
)

__all__ = [
    "ValidationFailure",
    "ValidationResult",
    "count_note_words",
    "render_note",
    "validate_note",
]
