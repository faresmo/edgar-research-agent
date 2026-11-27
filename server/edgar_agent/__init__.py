"""edgar_agent: the research agent built on edgar_core.

edgar_core is plain Python with no AI in it. This package adds the parts that
need a model: tool definitions, the run loop, the calculator, the notes file,
the sourcing check on every answer, configuration, and the command line.
"""

from .calculator import CalculationError, calculate
from .config import Settings, resolve_settings
from .notes import Notes
from .sourcing import SourcingProblem, check_answer

__all__ = ["CalculationError", "Notes", "Settings", "SourcingProblem", "calculate", "check_answer", "resolve_settings"]
