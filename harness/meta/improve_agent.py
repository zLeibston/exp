"""Meta entry point for stability and forgetting repair."""
from pathlib import Path

from harness.meta.agent.base_agent import AgentSystem
from harness.meta.agent.llm_withtools import chat_with_agent

from harness.meta.prompt import improve_instruction, skill_instruction


class ImproveAgent(AgentSystem):
    def forward(self, repo_path, eval_path, iterations_left=None, *, stage,
                context_paths=(), instruction=None):
        base = improve_instruction(repo_path, eval_path, iterations_left, context_paths, stage=stage)
        if instruction:
            base += '\n\nAdditional instructions:\n' + instruction
        instruction = base
        # Required even for a caller-supplied instruction. This first user message
        # stays in ordinary conversation history throughout the tool loop.
        instruction += '\n\n' + skill_instruction(stage)
        return chat_with_agent(instruction, model=self.model, msg_history=[],
                               logging=self.log, tools_available="all", finish_on_budget=True,
                               state_path=Path(self.logger_manager.log_file).parent / "meta_state.json")
