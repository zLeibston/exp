"""Meta entry point for stability and forgetting repair."""
from harness.meta.agent.base_agent import AgentSystem
from harness.meta.agent.llm_withtools import chat_with_agent

from harness.meta.prompt import improve_instruction


class ImproveAgent(AgentSystem):
    def forward(self, repo_path, eval_path, iterations_left=None, *, stage,
                context_paths=(), instruction=None):
        if instruction is None:
            instruction = improve_instruction(repo_path, eval_path, iterations_left, context_paths, stage=stage)
        return chat_with_agent(instruction, model=self.model, msg_history=[],
                               logging=self.log, tools_available="all", finish_on_budget=True)
