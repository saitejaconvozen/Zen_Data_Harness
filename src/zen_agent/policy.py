from __future__ import annotations

from dataclasses import dataclass

from .config import HarnessConfig
from .models import ToolRisk


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: str
    reason: str


class PolicyEngine:
    def __init__(self, config: HarnessConfig):
        self.config = config

    def evaluate(self, tool_name: str, risk: ToolRisk) -> PolicyDecision:
        if risk in self.config.approval_risks:
            return PolicyDecision("needs_approval", f"{risk.value} requires human approval")
        if risk in self.config.allowed_risks:
            return PolicyDecision("allow", f"{tool_name} is allowed at risk {risk.value}")
        return PolicyDecision(self.config.default_tool_effect, f"risk {risk.value} is not allowed")
