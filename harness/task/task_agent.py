"""Generic initial task harness, adapted from HyperAgents TaskAgent."""

from harness.task.agent.base_agent import AgentSystem
from harness.task.agent.llm_withtools import chat_with_agent
from harness.task.utils.common import parse_json

from harness.task.prompt import task_instruction
from harness.task.agent.trace import Trace
from pathlib import Path


class TaskAgent(AgentSystem):
    def forward(self, inputs):
        """Solve an arbitrary task input and return its response and full history.

        Terminal tasks use the existing tools to modify their environment.
        Other domains retain the original tool-free seed behavior.
        """
        with Trace(Path(self.logger_manager.log_file).parent):
            instruction = task_instruction(inputs)
            history = chat_with_agent(instruction, model=self.model, msg_history=[],
                                      logging=self.log,
                                      tools_available="all" if inputs.get("domain") == "terminal" else None)
            prediction = None
            try:
                prediction = parse_json(history[-1]["text"])["response"]
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                self.log(f"Error extracting prediction: {exc}")
            return prediction, history
