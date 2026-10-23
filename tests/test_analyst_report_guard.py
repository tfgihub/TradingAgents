import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts import turn
from tradingagents.agents.analysts.turn import WRAP_UP, not_a_report, take_turn

QUESTION = (
    "Could you clarify what specific analysis or information you need from the provided data?\n"
    "- Total shares sold?\n- Trends in sales over time?\n\nLet me know how I can assist!"
)
REPORT = "### Fundamentals of CRM\n" + "Revenue grew and margins held. " * 40 + "\n| Item | View |\n|---|---|"


class Fake(GenericFakeChatModel):
    """A chat model that answers from a list and ignores the tools bound to it."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, *args, **kwargs)


def make(*answers):
    model = Fake(messages=iter([AIMessage(content=a) for a in answers]))
    model.seen = []
    return model


PROMPT = ChatPromptTemplate.from_messages(
    [("system", "Tools: {tool_names}"), MessagesPlaceholder(variable_name="messages")]
).partial(tool_names="get_fundamentals")


def history():
    call = AIMessage(content="", tool_calls=[{"name": "get_fundamentals", "args": {}, "id": "1"}])
    return [HumanMessage(content="CRM"), call, ToolMessage(content="data", tool_call_id="1", name="get_fundamentals")]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (QUESTION, True),
        ("", True),
        ("   \n", True),
        ("Please provide the ticker.", True),
        (REPORT, False),
        ("- **overall_band**: Mildly Bullish\n- score: 0.2", False),  # short but a report
        ("x " * 600 + " let me know how I can assist", False),  # long: a report that says it
        (["a", "block"], False),
    ],
)
def test_what_counts_as_not_a_report(text, expected):
    assert not_a_report(text) is expected


@pytest.mark.unit
def test_a_report_is_returned_as_it_is_with_no_second_call():
    llm = make(REPORT)

    result, report = take_turn(PROMPT, llm, [], history())

    assert report == REPORT and result.content == REPORT
    assert len(llm.seen) == 1


@pytest.mark.unit
def test_a_question_is_asked_for_again_once_and_the_new_answer_is_used():
    llm = make(QUESTION, REPORT)

    result, report = take_turn(PROMPT, llm, [], history())

    assert report == REPORT and result.content == REPORT
    assert len(llm.seen) == 2
    retry = llm.seen[1]
    assert retry[-1].content == turn.RETRY  # the nudge comes last
    assert not any(getattr(m, "tool_calls", None) for m in retry)  # the history as text


@pytest.mark.unit
def test_a_second_question_leaves_a_note_that_there_is_no_report():
    llm = make(QUESTION, QUESTION)

    result, report = take_turn(PROMPT, llm, [], history())

    assert report == turn.UNUSABLE and result.content == turn.UNUSABLE
    assert "missing information" in report
    assert len(llm.seen) == 2  # asked once more, not in a loop


@pytest.mark.unit
def test_tool_calls_are_not_checked_or_retried():
    call = AIMessage(content="", tool_calls=[{"name": "get_fundamentals", "args": {}, "id": "2"}])
    llm = Fake(messages=iter([call]))
    llm.seen = []

    result, report = take_turn(PROMPT, llm, [], [HumanMessage(content="CRM")])

    assert report == "" and result.tool_calls
    assert len(llm.seen) == 1


@pytest.mark.unit
def test_the_wrap_up_turn_is_checked_too():
    llm = make(QUESTION, REPORT)
    messages = [*history(), HumanMessage(content=WRAP_UP)]

    result, report = take_turn(PROMPT, llm, [], messages)

    assert report == REPORT
    assert len(llm.seen) == 2
