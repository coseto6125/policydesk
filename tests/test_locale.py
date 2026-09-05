"""
The reply is written in the customer's language, read off the customer's message.

Ported from enoract's detector: FastText names the language, the script on the page
overrides it, a short Latin token names nothing, and OpenCC splits zh-TW from zh-CN.
"""

import inspect

import pytest

from policydesk.agent import executor, i18n
from policydesk.agent import locale as lang
from policydesk.core.db import Database


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    return pool


@pytest.mark.parametrize(
    ("text", "locale"),
    [
        ("我想了解目前的保單保什麼", "zh-TW"),
        ("我想了解目前的保单保什么", "zh-CN"),
        ("How much is my premium this month?", "en"),
        ("保険料はいくらですか", "ja"),
        ("보험료는 얼마인가요", "ko"),
    ],
)
def test_a_message_names_its_language(text: str, locale: str):
    assert lang.detect(text) == locale


@pytest.mark.parametrize("text", ["ok", "CL9926-658746", "658746", "", "!!!"])
def test_a_token_with_no_language_names_none(text: str):
    """A policy number scored as Russian at 0.31. It is not a change of language."""
    assert lang.detect(text) == lang.UNKNOWN


def test_shared_characters_alone_read_as_the_desks_own_script():
    """「我是月繳」 has no character that differs between the two scripts."""
    assert lang.detect("我是月繳") == "zh-TW"


async def test_a_reply_language_never_stays_unknown(db):
    """UNKNOWN falls back to the conversation, then to zh-TW: the desk's own."""
    found, spoken = await lang.resolve(db, case_id=-1, text="ok")
    assert found == lang.UNKNOWN
    assert spoken == lang.DEFAULT


def test_both_model_calls_end_on_the_language_hint():
    """The router's free answer and the scenario answer are both read by the customer."""
    source = inspect.getsource(executor)
    assert source.count("i18n.hint(turn.locale)") == 2


def test_the_hint_is_in_the_target_language_where_one_is_written():
    assert i18n.hint("zh-TW").startswith("以台灣繁體中文")
    assert i18n.hint("ja") == "日本語で返信してください。"
    assert i18n.hint("fr") == "Reply in the language tagged fr."


async def test_the_chips_are_rendered_in_the_customers_language(db):
    chips = ("可以改成年繳嗎？", "逾期了還能補繳嗎？")
    assert await i18n.translate(db, "zh-TW", chips) == chips
    assert await i18n.translate(db, "en", chips) == ("Can I switch to annual payments?", "Can I still pay after the due date?")
    assert await i18n.translate(db, "zh-CN", ("可以改成年繳嗎？",)) == ("可以改成年缴吗？",)


async def test_a_chip_without_a_row_keeps_its_english_then_its_own_text(db):
    """Never a blank where a chip was."""
    assert await i18n.translate(db, "ja", ("可以改成年繳嗎？", "沒有這一句")) == (
        "Can I switch to annual payments?", "沒有這一句",
    )


def test_the_calculator_is_a_scenario_choice_and_not_a_desk_fixture():
    """1+1=2 went out to a customer because every answer call carried the calculator."""
    assert executor._answer_schema(())["properties"]["calculations"]["maxItems"] == 0
    assert "maxItems" not in executor._answer_schema((), calculator=True)["properties"]["calculations"]
    from policydesk.agent.scenario import CATALOGUE

    assert not [s.name for s in CATALOGUE if s.calculator], "no scenario asks the model to compute today"


def test_an_off_topic_message_is_answered_with_the_desks_scope():
    from policydesk.agent.scenario import ROUTER_INSTRUCTIONS

    assert "Stay on the desk's subject" in ROUTER_INSTRUCTIONS
    assert "The request itself stays unanswered" in ROUTER_INSTRUCTIONS
