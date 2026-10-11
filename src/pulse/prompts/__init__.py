"""Prompt construction.

Kept separate from the stages that call the model so prompts can be inspected, diffed, and
tested as text. A prompt that changes shape between runs is a cache miss at best and an
unexplained quality shift at worst, so these functions are pure and deterministic.
"""

from pulse.prompts.taxonomy import TAXONOMY_RULES, render_batch, taxonomy_prompt

__all__ = ["TAXONOMY_RULES", "render_batch", "taxonomy_prompt"]
