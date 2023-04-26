"""Run a specific automaton and its sub-automata."""

import ast
from functools import lru_cache, partial
import functools
import json
from pathlib import Path
import sys
from typing import Callable, Dict, List, Tuple, Union

from langchain import LLMChain, PromptTemplate
from langchain.agents import (
    ZeroShotAgent,
    Tool,
    AgentExecutor,
    load_tools,
    Tool,
)
from langchain.chat_models import ChatOpenAI
from langchain.llms import BaseLLM
from langchain.chat_models import ChatOpenAI
from langchain import PromptTemplate, LLMChain
from langchain.prompts.chat import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
import yaml

sys.path.append("")

from src.globals import AUTOMATON_AFFIXES, resource_metadata
from src.llm_function import make_llm_function
from src.types import Automaton, AutomatonOutputParser


def find_model(engine: str) -> BaseLLM:
    """Create the model to use."""
    if engine is None:
        return None
    if engine in ["gpt-3.5-turbo", "gpt-4"]:
        return ChatOpenAI(temperature=0, model_name=engine)
    raise ValueError(f"Engine {engine} not supported yet.")


def save_file(action_input: str, self_name: str, workspace_name: str) -> str:
    """Save a file."""
    try:
        input_json = json.loads(action_input)
        file_name = input_json["file_name"]
        content = input_json["content"]
        description = input_json.get("description", "")
    except (KeyError, json.JSONDecodeError):
        return "Could not parse input. Please provide the input in the following format: {file_name: <file_name>, description: <description>, content: <content>}"
    path: Path = Path("workspace") / workspace_name / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), encoding="utf-8")
    resource_metadata.set_description(str(path), description)
    return f"{self_name}: saved file to `{path.relative_to('workspace')}`"


def load_file(action_input: str, self_name: str) -> str:
    """Load a file."""
    try:
        input_json = json.loads(action_input)
        file_name = input_json["file_name"]
        path: Path = Path("workspace") / file_name
    except (KeyError, json.JSONDecodeError):
        return "Could not parse input. Please provide the input in the following format: {file_name: <file_name>}"
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"{self_name}: file `{file_name}` not found. Please view your workspace to see which files are available, and use the full path given."
    return content


def view_workspace_files(_, self_name: str, workspace_name: str) -> str:
    """View files in a workspace."""
    path: Path = Path("workspace") / workspace_name
    file_info = (
        f"- `{file.relative_to('workspace')}`: {resource_metadata.get_description(str(file))}"
        for file in path.iterdir()
    )
    if not path.exists():
        raise FileNotFoundError(f"Workspace `{workspace_name}` not found.")
    files = "\n".join(file_info)
    return f"{self_name}: files in your workspace:\n{files}"


def load_function(
    file_name: str,
    data: dict,
    delegator: Union[str, None] = None,
    input_validator: Callable[[str], Tuple[bool, str]] = None,
    suppress_errors: bool = False,
) -> Automaton:
    """Load a function, which uses the same interface as automata but does not make decisions."""

    model = find_model(data["engine"])
    supported_functions = [
        "llm_assistant",
        "think",
        "human",
        "save_file",
        "load_file",
        "view_workspace",
        "finalize",
    ]

    full_name = f"{data['name']} ({data['role']} {data['rank']})"
    input_requirements = (
        "\n".join([f"- {req}" for req in data["input_requirements"]])
        if data["input_requirements"]
        else "None"
    )
    description_and_input = (
        data["description"] + f" Input requirements:\n{input_requirements}"
    )

    if file_name == "llm_assistant":
        template = "You are a helpful assistant who can help generate a variety of content. However, if anyone asks you to access files, or refers to something from a past interaction, you will immediately inform them that the task is not possible, and provide no further information."
        system_message_prompt = SystemMessagePromptTemplate.from_template(template)
        human_template = "{text}"
        human_message_prompt = HumanMessagePromptTemplate.from_template(human_template)
        chat_prompt = ChatPromptTemplate.from_messages(
            [system_message_prompt, human_message_prompt]
        )
        assistant_chain = LLMChain(llm=model, prompt=chat_prompt)
        run = assistant_chain.run

    elif file_name == "save_file":
        run = partial(save_file, self_name=full_name, workspace_name=delegator)

    elif file_name == "load_file":
        run = partial(load_file, self_name=full_name)

    elif file_name == "view_workspace":
        run = partial(
            view_workspace_files, self_name=full_name, workspace_name=delegator
        )

    elif file_name == "think":
        run = (
            lambda thought: f"I haven't done anything yet, and need to carefully consider what to do next. My previous thought was: {thought}",
        )

    elif file_name == "human":
        run = load_tools(["human"])[0].run

    elif file_name == "finalize":
        run = lambda _: None # not meant to actually be run; the finalize action should be caught by the parser first

    else:
        raise NotImplementedError(
            f"Unsupported function name: {file_name}. Only {supported_functions} are supported for now."
        )

    run = add_run_handling(
        run,
        name=full_name,
        suppress_errors=suppress_errors,
        input_validator=input_validator,
    )

    return Tool(full_name, run, description_and_input)


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


def inspect_input_specs(input: str, requirements: List[str]) -> Dict[str, str]:
    """
    Validate whether the input for a function adheres to the requirements of the function.

    This function checks the given input string against a list of requirements and returns a dictionary. The dictionary
    contains two keys: "success" and "message". The "success" key has a boolean value indicating whether the input
    meets all the requirements, and the "message" key has a string value with an error message if the input does not
    meet a requirement, or an empty string if the input meets all the requirements.

    :param input: The input string to be validated.
    :type input: str
    :param requirements: A list of requirements that the input string should meet.
    :type requirements: List[str]
    :return: A dictionary containing a boolean value indicating whether the input meets all requirements and an error message.
    :rtype: Dict[str, str]

    Examples:
    >>> inspect_input("1 + 1", ["A math expression"])
    {"success": true, "message": ""}
    >>> inspect_input("x = 5", ["A math expression", "Contains a variable", "Variable is named 'y'"])
    {"success": false, "message": "The input does not have a variable named 'y'."}
    >>> inspect_input("def example_function():", ["A function definition", "Named 'example_function'"])
    {"success": true, "message": ""}
    >>> inspect_input("x + y * z", ["A math expression", "Uses all basic arithmetic operations"])
    {"success": false, "message": "The input does not use all basic arithmetic operations."}
    >>> inspect_input("How are you?", ["A question", "About well-being"])
    {"success": true, "message": ""}
    >>> inspect_input("The quick brown fox jumps over the lazy dog.", ["A sentence", "Contains all English letters"])
    {"success": true, "message": ""}
    >>> inspect_input("Once upon a time...", ["A narrative", "Begins with a common opening phrase"])
    {"success": true, "message": ""}
    >>> inspect_input("How old are you?", ["A question", "About age", "Uses the word 'years'"])
    {"success": false, "message": "The input does not use the word 'years'."}
    >>> inspect_input("The sun sets in the east.", ["A statement", "Describes a natural phenomenon", "Factually accurate"])
    {"success": false, "message": "The input is not factually accurate."}
    >>> inspect_input("Are you going to the party tonight?", ["A question", "About attending an event", "Mentions a specific person"])
    {"success": false, "message": "The input does not mention a specific person."}
    >>> inspect_input("I prefer dogs over cats.", ["A preference", "Involves animals", "Prefers cats"])
    {"success": false, "message": "The input expresses a preference for dogs, not cats."}
    """
    ...


def validate_input(
    run_input: str, input_inspector: Callable[[str], str], full_name: str
) -> Tuple[bool, str]:
    """Validate input against input requirements, using an input inspector. The input inspector is intended to be powered by an LLM."""
    expected_output_keys = ["success", "message"]
    output = input_inspector(run_input)

    try:
        output = json.loads(output)
    except json.JSONDecodeError:
        output = ast.literal_eval(
            output.replace("true", "True").replace("false", "False")
        )
    except Exception as error:
        raise ValueError("Input inspector output is not a valid dictionary.") from error
    try:
        if output["success"]:
            return True, ""
        return (
            output["success"],
            f"{full_name}: {output['message']} Please check the input requirements of this automaton and try again.",
        )
    except KeyError as error:
        raise ValueError(
            f"Input inspector output does not have the correct format. Expected keys: {expected_output_keys}"
        ) from error


def add_run_handling(
    run: Callable,
    name: str,
    input_validator: Union[Callable[[str], Tuple[bool, str]], None] = None,
    suppress_errors: bool = False,
) -> Callable:
    """Handle errors and printouts during execution of a query."""
    preprint = f"\n\n---{name}: Start---"
    postprint = f"\n\n---{name}: End---"

    @functools.wraps(run)
    def wrapper(*args, **kwargs):
        if input_validator:
            valid, error = input_validator(args[0])
            if not valid:
                return error
        print(preprint)
        try:
            result = run(*args, **kwargs)
            print(postprint)
            return result
        # except Exception as error:
        #     if not suppress_errors:
        #         raise error
        #     # ignore all errors since delegators should handle automaton failures
        #     return (
        #         str(error)
        #         .replace(
        #             "Could not parse LLM output: ",
        #             "The sub-automaton ran into an error while processing the query. Its last thought was: ",
        #         )
        #         .replace("`", "```")
        #     )
        except KeyboardInterrupt:
            # manual interruption should escape back to the delegator
            print(postprint)
            return "Sub-automaton took too long to process and was stopped."

    return wrapper


@lru_cache(maxsize=None)
def load_automaton(
    file_name: str, delegator: Union[str, None] = None, suppress_errors: bool = False
) -> Automaton:
    """Load an automaton from a YAML file."""

    data = yaml.load(
        (Path("automata") / f"{file_name}.yml").read_text(encoding="utf-8"),
        Loader=yaml.FullLoader,
    )
    full_name = f"{data['name']} ({data['role']} {data['rank']})"
    engine = data["engine"]
    input_requirements = (
        "\n".join([f"- {req}" for req in data["input_requirements"]])
        if data["input_requirements"]
        else "None"
    )
    description_and_input = (
        data["description"] + f" Input requirements:\n{input_requirements}"
    )

    # create input validation
    validator_engine = data["input_validator_engine"]
    if validator_engine:
        inspect_input = make_llm_function(
            inspect_input_specs, model=find_model(validator_engine)
        )
        inspect_input = partial(
            inspect_input, input_requirements=data["input_requirements"]
        )

    input_validator = (
        partial(validate_input, input_inspector=inspect_input, full_name=full_name)
        if validator_engine
        else None
    )

    if (
        data["role"] == "function"
    ):  # functions are loaded separately from higher-order automata
        return load_function(
            file_name,
            data,
            delegator,
            input_validator=input_validator,
            suppress_errors=True,
        )

    llm = find_model(engine)

    # wrap rest of loader inside a function to delay loading of sub-automata until needed
    def load_and_run(*args, **kwargs) -> str:
        sub_automata = [
            load_automaton(name, delegator=file_name, suppress_errors=True)
            for name in data["sub_automata"]
        ]
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
        agent_executor = AgentExecutor.from_agent_and_tools(
            agent=ZeroShotAgent(
                llm_chain=llm_chain,
                allowed_tools=[sub_automaton.name for sub_automaton in sub_automata],
                output_parser=AutomatonOutputParser(),
            ),
            tools=sub_automata,
            verbose=True,
            max_iterations=None,
            max_execution_time=None,
        )
        return agent_executor.run(*args, **kwargs)

    automaton = Tool(
        full_name,
        add_run_handling(
            load_and_run,
            name=full_name,
            suppress_errors=suppress_errors,
            input_validator=input_validator,
        ),
        description_and_input,
    )
    return automaton


def main():
    quiz_creator = load_automaton("quiz_creator")
    quiz_creator.run(
        # "Create a math quiz suitable for a freshman college student, with 10 questions, then write it to a file called `math_quiz.txt`."
        "Find an existing quiz in your workspace, load it, and figure out how many questions there is in it."
    )

    # assistant = load_automaton("llm_assistant")
    # result = assistant.run(
    #     "generate a math quiz and save it to a file called `math_quiz.txt`."
    # )
    breakpoint()


if __name__ == "__main__":
    main()
