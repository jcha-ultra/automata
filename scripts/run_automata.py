"""Run a specific automaton and its sub-automata."""

from functools import lru_cache
import functools
from pathlib import Path
import sys
from typing import Callable, Dict, List, Protocol, Union

from langchain import LLMChain, PromptTemplate
from langchain.agents import (
    ZeroShotAgent,
    Tool,
    AgentExecutor,
    load_tools,
    initialize_agent,
    Tool,
    AgentType,
)
from langchain.chat_models import ChatOpenAI
from langchain.llms import BaseLLM, OpenAI
from langchain.tools import BaseTool
from langchain.prompts.chat import ChatPromptTemplate
from langchain.chat_models import ChatOpenAI
from langchain import PromptTemplate, LLMChain
from langchain.prompts.chat import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    AIMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from langchain.schema import AIMessage, HumanMessage, SystemMessage
import yaml

sys.path.append("")

from src.globals import AUTOMATON_AFFIXES


class Automaton(Protocol):
    """Protocol for automata. Uses the same interface as the Langchain `Tool` class."""

    name: str
    """Name of the automata. Viewable to delegators."""
    run: Callable[[str], str]
    """Function that takes in a query and returns a response."""
    description: str
    """Description of the automata. Viewable to delegators."""


def find_model(engine: str) -> BaseLLM:
    """Find the model to use for a given reasoning type."""
    if engine is None:
        return None
    if engine in ["gpt-3.5-turbo", "gpt-4"]:
        return ChatOpenAI(temperature=0, model_name=engine)
    raise ValueError(f"Engine {engine} not supported yet.")


def save_file(action_input: str, function_name: str) -> str:
    """Save a file to the scratchpad."""
    try:
        input_json = json.loads(action_input)
        path = input_json["path"]
        content = input_json["content"]
    except (KeyError, json.JSONDecodeError):
        return "Could not parse input. Please provide the input in the following format: {path: <path>, content: <content>}"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(str(content), encoding="utf-8")
    return f"{function_name}: saved file to `{path}`"


def load_function(file_name: str, data: dict) -> Automaton:
    """Load a function, which uses the same interface as automata but does not make decisions."""

    model = find_model(data["engine"])
    supported_functions = ["llm_assistant", "reflect", "human", "save_file"]

    full_name = f"{data['name']} ({data['role']} {data['rank']})"
    input_requirements = "\n".join([f"- {req}" for req in data["input_requirements"]]) if data["input_requirements"] else "None"
    description_and_input = (
        data["description"] + f" Input requirements:\n{input_requirements}"
    )

    if file_name == "llm_assistant":
        template = "You are a helpful assistant who can help generate a variety of content. However, if anyone asks you to access files, or refers to something from a past interaction, you will immediately inform them that the task is not possible."
        system_message_prompt = SystemMessagePromptTemplate.from_template(template)
        human_template = "{text}"
        human_message_prompt = HumanMessagePromptTemplate.from_template(human_template)
        chat_prompt = ChatPromptTemplate.from_messages(
            [system_message_prompt, human_message_prompt]
        )
        assistant_chain = LLMChain(llm=model, prompt=chat_prompt)
        return Tool(full_name, assistant_chain.run, description=description_and_input)

    if file_name == "save_file":
        return Tool(
            data["name"],
            partial(save_file, function_name=full_name),
            description=description_and_input,
        )

    if file_name == "reflect":
        return Tool(
            full_name,
            lambda reflection: f"I haven't done anything yet, and need to carefully consider what to do next. My current reflection is: {reflection}",
            description=description_and_input
        )

    if file_name == "human":
        return Tool(
            full_name, load_tools(["human"])[0].run, description=description_and_input
        )
    
        )

    raise NotImplementedError(
        f"Unsupported function name: {file_name}. Only {supported_functions} are supported for now."
    )


def get_role_info(role: str) -> Dict:
    """Get the role info for a given role."""
    return yaml.load(
        Path(f"src/prompts/roles/{role}.yml").read_text(encoding="utf-8"),
        Loader=yaml.FullLoader,
    )


def create_automaton_prompt(
    input_requirements: str,
    self_instructions: List[str],
    self_imperatives: List[str],
    role_info: Dict[str, str],
    sub_automata: List[Tool],
) -> PromptTemplate:
    """Put together a prompt for an automaton."""

    imperatives = role_info["imperatives"] + (self_imperatives or [])
    imperatives = "\n".join([f"- {imperative}" for imperative in imperatives])

    instructions = role_info["instructions"] + (self_instructions or [])
    instructions = "\n".join([f"- {instruction}" for instruction in instructions])

    prefix = AUTOMATON_AFFIXES["prefix"].format(
        input_requirements=input_requirements,
        role_description=role_info["description"],
        imperatives=imperatives,
        instructions=instructions,
    )
    suffix = AUTOMATON_AFFIXES["suffix"]
    prompt = ZeroShotAgent.create_prompt(
        sub_automata,
        prefix=prefix,
        suffix=suffix,
        input_variables=["input", "agent_scratchpad"],
        format_instructions=role_info["output_format"],
    )
    return prompt


def add_run_handling(
    run: Callable, name: str, suppress_errors: bool = False
) -> Callable:
    """Handle errors during execution of a query."""
    preprint = f"\n\n---{name}: Start---"
    postprint = f"\n\n---{name}: End---"

    @functools.wraps(run)
    def wrapper(*args, **kwargs):
        print(preprint)
        try:
            result = run(*args, **kwargs)
            print(postprint)
            return result
        except Exception as error:
            if not suppress_errors:
                raise error
            # ignore all errors since delegators should handle automaton failures
            return (
                str(error)
                .replace(
                    "Could not parse LLM output: ",
                    "The sub-automaton ran into an error while processing the query. Its last thought was: ",
                )
                .replace("`", "```")
            )
        except KeyboardInterrupt:
            # manual interruption should escape back to the delegator
            print(postprint)
            return "Sub-automaton took too long to process and was stopped."

    return wrapper


@lru_cache(maxsize=None)
def load_automaton(file_name: str) -> Automaton:
    """Load an automaton from a YAML file."""
    data = yaml.load(
        (Path("automata") / f"{file_name}.yml").read_text(encoding="utf-8"),
        Loader=yaml.FullLoader,
    )
    full_name = f"{data['name']} ({data['role']} {data['rank']})"
    engine = data["engine"]
    input_requirements = "\n".join([f"- {req}" for req in data["input_requirements"]])
    description_and_input = (
        data["description"] + f" Input requirements:\n{input_requirements}"
    )

    if data["role"] == "function":  # functions are loaded individually
        return load_function(file_name, data)

    llm = find_model(engine)
    sub_automata = data["sub_automata"]
    sub_automata = [load_automaton(name) for name in sub_automata]
    prompt = create_automaton_prompt(
        input_requirements=input_requirements,
        self_instructions=data["instructions"],
        self_imperatives=data["imperatives"],
        role_info=get_role_info(data["role"]),
        sub_automata=sub_automata,
    )
    # print(prompt.format(input="blah", agent_scratchpad={}))
    # breakpoint()
    llm_chain = LLMChain(llm=llm, prompt=prompt)
    agent = ZeroShotAgent(
        llm_chain=llm_chain,
        allowed_tools=[sub_automaton.name for sub_automaton in sub_automata],
    )
    agent_executor = AgentExecutor.from_agent_and_tools(
        agent=agent,
        tools=sub_automata,
        verbose=True,
        max_iterations=data["rank"] * 10 + 5,
        max_execution_time=data["rank"] * 200 + 60,
    )
    automaton = Tool(
        full_name,
        add_run_handling(agent_executor.run, name=full_name),
        description_and_input,
    )
    return automaton


def main():
    quiz_creator = load_automaton("quiz_creator")
    quiz_creator.run(
        "Create a math quiz suitable for a freshman college student, with 10 questions, then write it to a file."
    )


if __name__ == "__main__":
    main()
