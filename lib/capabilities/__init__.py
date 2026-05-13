# lib/capabilities/__init__.py
# Capability modules. Each capability knows:
#   * which metrics make sense (passed to lib/eval.py)
#   * which Auditor checks to run
#   * which technique families are valid arms for the bandit
#   * how to execute one EvalCase against a Variant
#
# The registry below is consulted at the start of /run to dispatch to the
# right capability based on Mission.composition.task_modality.

from .base import CapabilityBase, CapabilityContext
from .registry import get_capability, register_capability, list_capabilities

# Importing each capability registers it as a side effect (decorator pattern).
from . import rag_tuning      # noqa: F401
from . import nlq             # noqa: F401
from . import research_agent  # noqa: F401
from . import insight_agent   # noqa: F401
from . import search_engine   # noqa: F401
from . import chatbot         # noqa: F401

__all__ = [
    "CapabilityBase", "CapabilityContext",
    "get_capability", "register_capability", "list_capabilities",
]
