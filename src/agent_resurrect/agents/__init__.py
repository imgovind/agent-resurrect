"""Agent adapter registry."""
from typing import List, Optional

from .base import AgentAdapter, HookContext


def all_agents() -> List[AgentAdapter]:
    from .claude import ClaudeAdapter  # imported on use: the registry itself stays cheap to import
    return [ClaudeAdapter()]


def get_agent(name: str) -> Optional[AgentAdapter]:
    for agent in all_agents():
        if agent.name == name:
            return agent
    return None


__all__ = ["AgentAdapter", "HookContext", "all_agents", "get_agent"]
