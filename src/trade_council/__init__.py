"""trade_council - autonomous FX swing-trading council. v2 of claude_council."""
from .debate import run_debate
from .specialist import Specialist
from .moderator import Moderator
from .transcript import Transcript, Turn, Decision
from .formats import get_format, FORMATS
from .plan_generator import generate_plan, GeneratedPlan

__version__ = "0.2.0"
__all__ = [
    "run_debate", "Specialist", "Moderator",
    "Transcript", "Turn", "Decision", "get_format", "FORMATS",
    "generate_plan", "GeneratedPlan",
]
