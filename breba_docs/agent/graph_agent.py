import dataclasses
import json
from typing import TypedDict

from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from breba_docs.agent.agent import Agent
from breba_docs.agent.instruction_reader import get_instructions
from breba_docs.agent.openai_agent import OpenAIAgent
from breba_docs.services.command_executor import ContainerCommandExecutor, LocalCommandExecutor
from breba_docs.services.document import Document
from breba_docs.services.reports import GoalReport, CommandReport


class AgentState(TypedDict):
    messages: list[AnyMessage]
    goals: list[dict]
    goal_reports: list[GoalReport]


class GraphAgent:

    def __init__(self, doc: Document):
        self.agent: Agent = OpenAIAgent()
        self.model = ChatOpenAI(model="gpt-4o-mini", temperature=0)
        self.doc = doc

        self.system_instructions = None
        graph = StateGraph(AgentState)
        # TODO: can use ReAct agents for some of the looping
        # TODO: can update state without **state
        graph.add_node("identify_goals", self.identify_goals)
        graph.add_node("identify_commands", self.identify_commands)
        graph.add_node("execute_commands", self.execute_commands)
        graph.add_node("execute_mutator_commands", self.execute_mutator_commands)

        graph.set_entry_point("identify_goals")
        graph.add_edge("identify_goals", "identify_commands")
        graph.add_edge("identify_commands", "execute_commands")

        graph.add_conditional_edges(
            "execute_mutator_commands",
            self.process_more_goals,
            {True: "identify_commands", False: END}
        )
        graph.add_edge("execute_commands", "execute_mutator_commands")

        self.graph = graph.compile()

    def invoke(self):
        return self.graph.invoke({"messages": [], "goals": [], "goal_reports": []})

    def process_more_goals(self, state: AgentState):
        return len(state['goals']) > 0

    def execute_mutator_commands(self, state: AgentState):
        current_goal = dataclasses.replace(state['goal_reports'][-1])
        command_executor = LocalCommandExecutor(self.agent)
        for command_report in current_goal.command_reports:
            if not command_report.success:
                modify_commands = self.agent.fetch_modify_file_commands(self.doc.filepath, command_report)
                modify_report = command_executor.execute_commands_sync(modify_commands)
                current_goal.modify_command_reports += modify_report
        updated_goal_reports = [dataclasses.replace(report) for report in state['goal_reports'][:-1]]
        return {'goal_reports': updated_goal_reports + [current_goal]}

    def execute_commands(self, state: AgentState):
        # Grab the commands from the last goal report
        current_goal = state["goal_reports"][-1]
        commands = [command_report.command for command_report in current_goal.command_reports]
        # TODO: ContainerCommandExecutor needs to take an interface that provides input to prompts
        # TODO: Should not return command reports. That should be done by the graph
        command_reports = ContainerCommandExecutor(self.agent).execute_commands_sync(commands)
        updated_goal = GoalReport(current_goal.goal_name, current_goal.goal_description, command_reports)

        return {
            **state,
            'goal_reports': state['goal_reports'][:-1] + [updated_goal]
        }


    def identify_commands(self, state: AgentState):
        current_goal = state['goals'][0]  # Assume safe access
        system_instructions = get_instructions("fetch_commands", document=self.doc.content)

        # Remove old messages from the state because each goal will have an own clean slate
        messages = [SystemMessage(content=system_instructions)]

        message = HumanMessage(content=f"Give me commands for this goal: {json.dumps(current_goal)}")
        messages += [message]
        model_response = self.model.invoke(messages)
        messages.append(model_response)
        commands = [cmd.strip() for cmd in model_response.content.split(",")]

        command_reports = [CommandReport(command, None, None) for command in commands]
        goal_report = GoalReport(current_goal["name"], current_goal["description"], command_reports)

        # Create a new state object
        new_state = {
            **state,
            'goals': state['goals'][1:],  # Remove processed goal
            'messages': messages,
            'goal_reports': state['goal_reports'] + [goal_report]
        }

        return new_state

    def identify_goals(self, state: AgentState) -> AgentState:
        # Get system instructions
        system_instructions = get_instructions("identify_goals", document=self.doc.content)

        # Build new messages list
        new_messages: list[AnyMessage] = [SystemMessage(content=system_instructions)] + state['messages']
        new_messages.append(HumanMessage(content="What are my goals for this document?"))

        # Invoke the model
        response_message = self.model.invoke(new_messages)
        new_messages.append(response_message)

        # Parse goals from the response
        new_goals = json.loads(response_message.content)["goals"]

        # Create a new state dictionary with updated values
        new_state: AgentState = {
            **state,
            'messages': new_messages,
            'goals': new_goals,
        }

        return new_state

