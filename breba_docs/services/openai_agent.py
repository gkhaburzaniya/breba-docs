import json
import os
from pathlib import Path

from openai import OpenAI

from breba_docs.services.agent import Agent
from breba_docs.services.reports import CommandReport


class OpenAIAgent(Agent):
    INSTRUCTIONS_GENERAL = """
You are assisting a software program to validate contents of a document.
"""

    INSTRUCTIONS_ANALYZE_OUTPUT = f"""
You are assisting a software program to validate contents of a document. After running commands from the
documentation, the user received some output and needs help understanding the output. 
Here are important instructions:
0) Never return markdown. You will respond with JSON without special formatting
1) The user will present you with output of the commands that were just run.
2) The command has failed if there are errors and the user did not achieve intended goal
3) You will respond with a json that looks like this:
{CommandReport.example_str()}
"""

    INSTRUCTIONS_RESPONSE = """
You are assisting a software program to run commands. We are trying to determine if 
the command is stuck waiting for user input. The goal is to answer all the prompts so that the command can finish 
executing. Given a command's output, you will need to provide a response.

Here are important instructions:

1) You will provide an exact answer to any prompt if user input is actually expected. This will be passed directly to 
the terminal. 
2) If the command output indicates that the command is stuck waiting for user to answer a prompt, 
you will come up with the answer to the prompt to the best of your abilities. 
3) If and only if the command output 
ends with something that is not a prompt for user input, you will respond with "breba-noop".

Important Note: Always check for any subsequent output after a prompt. If there is any output following a prompt, 
it means the command is no longer waiting for input, and you should not provide an answer to that prompt."""

    INSTRUCTIONS_GET_COMMANDS_FOR_TASK = """
You are an expert in Quality Control for documentation. You are 
assisting a software program to create a list of terminal commands that will accomplish a given task.
Important: Never return markdown. You will return text without special formatting.
Important: The user is usually expecting a list of commands that will be run in the terminal sequentially. 
Important: When reading the document, you will only use terminal commands in the document exactly as they are written 
    in the document even if there are typos or errors.
Important: Do not use commands that are not in the document, unless asked to do so explicitly 

{}
"""

    INSTRUCTIONS_GET_GOALS = """
You are an expert in Quality Control for documentation. You  are assisting a software 
program to check that the tasks described in the following document actually work. NEVER USE MARKDOWN. JUST
PROVIDE JSON THAT CAN BE USED IN A SOFTWARE PROGRAM. The goals should be high level tasks that a user would want
to achieve. Do not make too many goals.

Respond using json like:
{
  "goals": [
    {
      "name": "getting started",
      "description": "As a user I would like to get started with the software"
    }
  ]
}
"""

    INSTRUCTIONS_FETCH_MODIFY_FILE_COMMANDS = """
You are an expert in Quality Control for documentation. You are 
assisting a software program to correct issues in the documentation.

IMPORTANT: NEVER RETURN MARKDOWN. YOU WILL RETURN TEXT WITHOUT SPECIAL FORMATTING. 
Important: Return only the commands to run because your response will be used by a software program to run these
   commands in the terminal.

You will respond with a json list that contains a field called "commands". 

When generating commands to modify the file, change the entire line in the file. Do not simply replace one word.


Here is the document:
{}
"""

    INPUT_FIRST_COMMAND_SYSTEM_INSTRUCTION = """
You are assisting a software program to run commands. We are trying to determine if 
the command is stuck waiting for user input. The goal is to answer all the prompts so that the command can finish 
executing.

You will answer with minimal text and not use formatting or markdown.
"""

    INPUT_FIRST_MESSAGE = """
    Does the last sentence in the command output contain a prompt asking the user for input in order to complete the command execution? 
    Answer with "Yes" if the last thing in the command output is a prompt asking the user for some input.
    Answer with "No" if the last thing in the command output is not a user prompt:
    
    Important: If there is additional text after the user prompt, that means the prompt was answered your answer must be "No"
    """

    INPUT_FIRST_MESSAGE_VERIFY = """
    Is the user prompt the last sentence of the command output? Answer only with "Yes" or "No"
    """

    INPUT_FOLLOW_UP_COMMAND_INSTRUCTION = """You need to come up with the right answer to this prompt. To the best of your 
abilities what should be put into the terminal to continue executing this command? 
Reply with exact response that will go into the terminal.

You will answer with minimal text and not use formatting or markdown.
"""

    INPUT_FOLLOW_UP_MESSAGE = """What should the response in the terminal be? Provide the exact answer to put into the
    terminal in order to answer the prompt."""

    def __init__(self):
        self.client = OpenAI()
        self.assistant = self.client.beta.assistants.create(
            name="Breba Docs",
            instructions=OpenAIAgent.INSTRUCTIONS_GENERAL,
            model="gpt-4o-mini"
        )
        self.thread = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def get_last_message(self):
        messages = self.client.beta.threads.messages.list(
            thread_id=self.thread.id
        )

        return messages.data[0].content[0].text.value

    def do_run(self, message, instructions, new_thread=True):
        print(f"Message: {message}")
        print(f"Instructions: {instructions}")

        if new_thread:
            self.thread = self.client.beta.threads.create()

        self.client.beta.threads.messages.create(
            thread_id=self.thread.id,
            role="user",
            content=message
        )

        run = self.client.beta.threads.runs.create_and_poll(
            thread_id=self.thread.id,
            assistant_id=self.assistant.id,
            instructions=instructions,
            temperature=0.0,
            top_p=1.0,
        )

        if run.status == 'completed':
            agent_response = self.get_last_message()
            print(f"Agent Response: {agent_response}")
            return agent_response
        else:
            print(f"OpenAI run.status: {run.status}")

    def fetch_goals(self, doc: str) -> list[dict]:
        message = ("Provide a list of goals that a user can accomplish via a terminal based on "
                   "the documentation. Headings and titles can be used as an indicator of a task. \n")
        assistant_output = self.do_run(message, OpenAIAgent.INSTRUCTIONS_GET_GOALS + doc)
        # TODO: create class for Goal that will parse the string using json.loads
        assistant_output = json.loads(assistant_output)
        return assistant_output["goals"]

    def fetch_commands(self, doc: str, goal: str) -> list[str]:
        message = goal + "\n"
        message += (
            """Provide a list of terminal commands that accomplish the task. This is a broad definition of 
the task. Make sure to list all commands needed to accomplish this task. I am a software program that will 
run these commands on a system that has little installed other than python. Do not assume any software is 
installed. Only use the commands from the document exactly as they are written in the document. Do not modify 
commands from the document. Do not invent new commands. Respond using a comma separated list. If there are no 
commands listed in the document support completing this task, return an empty list.""")
        assistant_output = self.do_run(message, OpenAIAgent.INSTRUCTIONS_GET_COMMANDS_FOR_TASK.format(doc))
        return [cmd.strip() for cmd in assistant_output.split(",")]

    def analyze_output(self, text: str) -> CommandReport:
        message = "Here is the output after running the commands. What is your conclusion? \n"
        message += text
        analysis = self.do_run(message, OpenAIAgent.INSTRUCTIONS_ANALYZE_OUTPUT)
        return CommandReport.from_string(analysis)

    def provide_input(self, text: str) -> str:
        message = OpenAIAgent.INPUT_FIRST_MESSAGE + "\n" + text
        has_prompt = self.do_run(message, OpenAIAgent.INPUT_FIRST_COMMAND_SYSTEM_INSTRUCTION)
        if has_prompt == "Yes":
            prompt_verified = self.do_run(OpenAIAgent.INPUT_FIRST_MESSAGE_VERIFY,
                                          OpenAIAgent.INPUT_FIRST_COMMAND_SYSTEM_INSTRUCTION,
                                          False)
            if prompt_verified == "Yes":
                prompt_answer = self.do_run(OpenAIAgent.INPUT_FOLLOW_UP_MESSAGE,
                                            OpenAIAgent.INPUT_FOLLOW_UP_COMMAND_INSTRUCTION,
                                            False)
                return prompt_answer
        return "breba-noop"

    def fetch_modify_file_commands(self, filepath: Path, command_report: CommandReport) -> list[str]:
        message = f"I have this output from trying to accomplish my goal:\n {command_report.insights}\n"
        message += f"Here is the file:\n {filepath}\n"
        message += f"Can you write a sed command to fix this issue in the file?"
        print("WORKING DIR: ", os.getcwd())
        with open(filepath, "r") as f:
            document = f.read()
            instructions = OpenAIAgent.INSTRUCTIONS_FETCH_MODIFY_FILE_COMMANDS.format(document)
            raw_response = self.do_run(message, instructions)
            commands = json.loads(raw_response)["commands"]  # should be a list. TODO: validate?

        return commands

    def close(self):
        self.client.beta.assistants.delete(self.assistant.id)
