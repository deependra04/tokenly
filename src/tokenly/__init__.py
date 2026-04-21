"""tokenly — One line to track every AI API cost.

    import tokenly
    tokenly.init()

Then use OpenAI / Anthropic / Google SDKs normally. Run `tokenly stats` to see costs.
"""
from __future__ import annotations

from .core import BudgetExceeded, configure, init, track

__version__ = "0.1.0"
__all__ = ["init", "track", "configure", "BudgetExceeded", "__version__"]
