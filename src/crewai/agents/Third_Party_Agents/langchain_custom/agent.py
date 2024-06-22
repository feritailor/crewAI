import os
from typing import Any, List, Optional, Tuple

from langchain.agents.agent import RunnableAgent
from langchain.tools import BaseTool
from langchain.tools.render import render_text_description
from langchain_core.agents import AgentAction
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from pydantic import Field, InstanceOf, model_validator

from crewai.agents import CacheHandler, CrewAgentExecutor, CrewAgentParser
from crewai.memory.contextual.contextual_memory import ContextualMemory
from crewai.utilities import Prompts
from crewai.utilities.token_counter_callback import TokenCalcHandler, TokenProcess
from crewai.agents.third_party_agents.base_agent import BaseAgent
from crewai.agents.third_party_agents.langchain_custom.tools.task_tools import (
    LangchainCustomTools,
)


class LangchainAgent(BaseAgent):
    """Represents an langchain based agent in a system."""

    token_process: TokenProcess = TokenProcess()

    max_execution_time: Optional[int] = Field(
        default=None,
        description="Maximum execution time for an agent to execute a task",
    )
    cache_handler: InstanceOf[CacheHandler] = Field(
        default=None, description="An instance of the CacheHandler class."
    )
    step_callback: Optional[Any] = Field(
        default=None,
        description="Callback to be executed after each step of the agent execution.",
    )
    llm: Any = Field(
        default_factory=lambda: ChatOpenAI(
            model=os.environ.get("OPENAI_MODEL_NAME", "gpt-4o")
        ),
        description="Language model that will run the agent.",
    )
    function_calling_llm: Optional[Any] = Field(
        description="Language model that will run the agent.", default=None
    )
    callbacks: Optional[List[InstanceOf[BaseCallbackHandler]]] = Field(
        default=None, description="Callback to be executed"
    )
    system_template: Optional[str] = Field(
        default=None, description="System format for the agent."
    )
    prompt_template: Optional[str] = Field(
        default=None, description="Prompt format for the agent."
    )
    response_template: Optional[str] = Field(
        default=None, description="Response format for the agent."
    )

    def __init__(__pydantic_self__, **data):
        config = data.pop("config", {})
        super().__init__(**config, **data)

    @model_validator(mode="after")
    def set_agent_executor(self) -> "LangchainAgent":
        """set agent executor is set."""
        if hasattr(self.llm, "model_name"):
            token_handler = TokenCalcHandler(self.llm.model_name, self.token_process)

            # Ensure self.llm.callbacks is a list
            if not isinstance(self.llm.callbacks, list):
                self.llm.callbacks = []

            # Check if an instance of TokenCalcHandler already exists in the list
            if not any(
                isinstance(handler, TokenCalcHandler) for handler in self.llm.callbacks
            ):
                self.llm.callbacks.append(token_handler)

        if not self.agent_executor:
            if not self.cache_handler:
                self.cache_handler = CacheHandler()
            self.set_cache_handler(self.cache_handler)
        return self

    def execute_task(
        self,
        task: Any,
        context: Optional[str] = None,
        tools: Optional[List[Any]] = None,
    ) -> str:
        """Execute a task with the agent.

        Args:
            task: Task to execute.
            context: Context to execute the task in.
            tools: Tools to use for the task.

        Returns:
            Output of the agent
        """
        if self.tools_handler:
            # type: ignore # Incompatible types in assignment (expression has type "dict[Never, Never]", variable has type "ToolCalling")
            self.tools_handler.last_used_tool = {}

        task_prompt = task.prompt()

        if context:
            task_prompt = self.i18n.slice("task_with_context").format(
                task=task_prompt, context=context
            )

        if self.crew and self.crew.memory:
            contextual_memory = ContextualMemory(
                self.crew._short_term_memory,
                self.crew._long_term_memory,
                self.crew._entity_memory,
            )
            memory = contextual_memory.build_context_for_task(task, context)
            if memory.strip() != "":
                task_prompt += self.i18n.slice("memory").format(memory=memory)

        tools = tools or self.tools
        # type: ignore # Argument 1 to "_parse_tools" of "Agent" has incompatible type "list[Any] | None"; expected "list[Any]"
        parsed_tools = self._parse_tools(tools or [])
        self.create_agent_executor(tools=tools)
        self.agent_executor.tools = parsed_tools
        self.agent_executor.task = task

        self.agent_executor.tools_description = render_text_description(parsed_tools)
        self.agent_executor.tools_names = self.__tools_names(parsed_tools)

        result = self.agent_executor.invoke(
            {
                "input": task_prompt,
                "tool_names": self.agent_executor.tools_names,
                "tools": self.agent_executor.tools_description,
            }
        )["output"]
        if self.max_rpm:
            self._rpm_controller.stop_rpm_counter()
        return result

    def format_log_to_str(
        self,
        intermediate_steps: List[Tuple[AgentAction, str]],
        observation_prefix: str = "Observation: ",
        llm_prefix: str = "",
    ) -> str:
        """Construct the scratchpad that lets the agent continue its thought process."""
        thoughts = ""
        for action, observation in intermediate_steps:
            thoughts += action.log
            thoughts += f"\n{observation_prefix}{observation}\n{llm_prefix}"
        return thoughts

    def create_agent_executor(self, tools=None) -> None:
        """Create an agent executor for the agent.

        Returns:
            An instance of the CrewAgentExecutor class.
        """
        tools = tools or self.tools

        agent_args = {
            "input": lambda x: x["input"],
            "tools": lambda x: x["tools"],
            "tool_names": lambda x: x["tool_names"],
            "agent_scratchpad": lambda x: self.format_log_to_str(
                x["intermediate_steps"]
            ),
        }

        executor_args = {
            "llm": self.llm,
            "i18n": self.i18n,
            "crew": self.crew,
            "crew_agent": self,
            "tools": self._parse_tools(tools),
            "verbose": self.verbose,
            "original_tools": tools,
            "handle_parsing_errors": True,
            "max_iterations": self.max_iter,
            "max_execution_time": self.max_execution_time,
            "step_callback": self.step_callback,
            "tools_handler": self.tools_handler,
            "function_calling_llm": self.function_calling_llm,
            "callbacks": self.callbacks,
        }

        if self._rpm_controller:
            executor_args["request_within_rpm_limit"] = (
                self._rpm_controller.check_or_wait
            )

        prompt = Prompts(
            i18n=self.i18n,
            tools=tools,
            system_template=self.system_template,
            prompt_template=self.prompt_template,
            response_template=self.response_template,
        ).task_execution()

        execution_prompt = prompt.partial(
            goal=self.goal,
            role=self.role,
            backstory=self.backstory,
        )

        stop_words = [self.i18n.slice("observation")]

        if self.response_template:
            stop_words.append(
                self.response_template.split("{{ .Response }}")[1].strip()
            )

        bind = self.llm.bind(stop=stop_words)

        inner_agent = agent_args | execution_prompt | bind | CrewAgentParser(agent=self)
        self.agent_executor = CrewAgentExecutor(
            agent=RunnableAgent(runnable=inner_agent), **executor_args
        )

    def get_delegation_tools(self, agents: List[BaseAgent]):
        agent_tools = LangchainCustomTools(agents=agents)
        tools = agent_tools.tools()
        return tools

    def _parse_tools(self, tools: List[Any]) -> List[BaseTool]:
        """Parse tools to be used for the task."""
        tools_list = []
        try:
            # tentatively try to import from crewai_tools import BaseTool as CrewAITool
            from crewai_tools import BaseTool as CrewAITool

            for tool in tools:
                if isinstance(tool, CrewAITool):
                    tools_list.append(tool.to_langchain())
                else:
                    tools_list.append(tool)
        except ModuleNotFoundError:
            for tool in tools:
                tools_list.append(tool)
        return tools_list

    @staticmethod
    def __tools_names(tools) -> str:
        return ", ".join([t.name for t in tools])

    def __repr__(self):
        return f"Agent(role={self.role}, goal={self.goal}, backstory={self.backstory})"