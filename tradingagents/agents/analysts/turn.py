"""One turn of a tool-using analyst, and its last turn once its tool rounds are spent."""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

WRAP_UP = (
    "You have used every tool round this report allows. Write your final report "
    "now from the tool results above, and say which data you could not retrieve."
)

# A small model sometimes answers its data with a question to the user. Nobody can answer it,
# and the research team would then debate a report that says nothing.
NOT_A_REPORT = re.compile(
    r"could you (please )?clarify|please (clarify|provide|specify)|i need (more|you to)"
    r"|how can i (assist|help)|would you like me to|what (specific|exactly) (analysis|information)",
    re.IGNORECASE,
)
# Longer answers are reports even if they use one of those phrases in passing.
MAX_QUESTION_CHARS = 800
RETRY = (
    "Do not ask questions: nobody can answer them. Write your final report now from the tool "
    "results above, say which data you could not retrieve, and end with the Markdown table."
)
UNUSABLE = (
    "REPORT UNAVAILABLE: this analyst returned no usable report (it asked a question instead "
    "of writing one). Treat this section as missing information, not as a neutral view."
)


def not_a_report(content):
    """Whether an analyst's final answer is empty or a question rather than a report."""
    if not isinstance(content, str):
        return False
    text = content.strip()
    return not text or (len(text) < MAX_QUESTION_CHARS and bool(NOT_A_REPORT.search(text)))


def take_turn(prompt, llm, tools, messages):
    """The analyst's reply and its report, which stays empty while it calls tools.

    Told to wrap up, it answers without tools: a model offered tools may call one
    whatever it is told. Its earlier calls and their results reach it as text,
    since a provider may refuse a history holding tool calls when none are bound.

    A final answer that is a question, or empty, is asked for again once; if it still is not
    a report, the section says so instead of passing the question on.
    """
    if messages and isinstance(messages[-1], HumanMessage) and messages[-1].content == WRAP_UP:
        # The prompt lists the tools it offers; a model told of tools it cannot
        # call may still try, and some providers then answer with nothing.
        prompt = prompt.partial(tool_names="none; your tool rounds are spent")
        result = (prompt | llm).invoke(_as_text(messages))
        return _checked(prompt, llm, messages, result)
    result = (prompt | llm.bind_tools(tools)).invoke(messages)
    if result.tool_calls:
        return result, ""
    return _checked(prompt, llm, messages, result)


def _checked(prompt, llm, messages, result):
    """``result`` if it is a report; else one more attempt, else a note that there is none."""
    if not_a_report(result.content):
        retry = prompt.partial(tool_names="none; write the report")
        result = (retry | llm).invoke([*_as_text(messages), HumanMessage(content=RETRY)])
        if not_a_report(result.content):
            result = AIMessage(content=UNUSABLE)
    return result, result.content


def _as_text(messages):
    """The history with its tool calls and tool results written out as plain messages."""
    written = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            said = message.content if isinstance(message.content, str) else ""
            calls = "; ".join(f"{c['name']}({json.dumps(c['args'], default=str)})" for c in message.tool_calls)
            written.append(AIMessage(content=f"{said}\n[Called {calls}]".strip()))
        elif isinstance(message, ToolMessage):
            written.append(HumanMessage(content=f"[{message.name or 'Tool'} returned]\n{message.content}"))
        else:
            written.append(message)
    return written
