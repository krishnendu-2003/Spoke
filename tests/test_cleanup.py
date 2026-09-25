import pytest

from spoke import cleanup


class FakeLLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def chat(self, *, model, messages, timeout, max_tokens):
        self.calls.append(dict(model=model, messages=messages, timeout=timeout, max_tokens=max_tokens))
        if self.exc:
            raise self.exc
        return self.reply


RAW = "um so the deploy is at 5 no 6 tomorrow you know"


def test_system_prompt_enforces_rules():
    p = cleanup.SYSTEM_PROMPT.lower()
    for must in ["punctuation", "filler", "self-correction", "never answer", "output only",
                 "code identifiers", "numbers", "language mix", "never translate", "do not rephrase, summarize"]:
        assert must in p, must


def test_transcript_is_wrapped_as_data():
    msgs = cleanup.build_messages("what time is it")
    assert msgs[0]["role"] == "system"
    assert msgs[-1] == {"role": "user", "content": "<transcript>what time is it</transcript>"}
    # few-shot includes a question that is cleaned, not answered
    assert any("capital of France is?" in m["content"] for m in msgs if m["role"] == "assistant")


def test_happy_path_passes_model_and_timeout():
    llm = FakeLLM("So the deploy is at 6 tomorrow.")
    out, status = cleanup.clean(RAW, llm, model="m1", timeout=1.0)
    assert (out, status) == ("So the deploy is at 6 tomorrow.", "ok")
    assert llm.calls[0]["model"] == "m1" and llm.calls[0]["timeout"] == 1.0


def test_skips_short_utterances_without_calling_llm():
    llm = FakeLLM("x")
    assert cleanup.clean("ship it now", llm, model="m") == ("ship it now", "skipped-short")
    assert llm.calls == []


def test_skips_when_disabled_or_no_client():
    llm = FakeLLM("x")
    assert cleanup.clean(RAW, llm, model="m", enabled=False) == (RAW, "skipped-disabled")
    assert cleanup.clean(RAW, None, model="m") == (RAW, "skipped-disabled")
    assert llm.calls == []


@pytest.mark.parametrize("exc", [TimeoutError("slow"), RuntimeError("500")])
def test_falls_back_to_raw_on_error_or_timeout(exc):
    out, status = cleanup.clean(RAW, FakeLLM(exc=exc), model="m")
    assert (out, status) == (RAW, "fallback-error")


@pytest.mark.parametrize("reply", [
    '"So the deploy is at 6 tomorrow."',
    "Here is the cleaned text: So the deploy is at 6 tomorrow.",
    "<transcript>So the deploy is at 6 tomorrow.</transcript>",
    "  So the deploy is at 6 tomorrow.\n",
])
def test_strips_quotes_preambles_and_tags(reply):
    out, status = cleanup.clean(RAW, FakeLLM(reply), model="m")
    assert (out, status) == ("So the deploy is at 6 tomorrow.", "ok")


def test_keeps_inner_quotes():
    assert cleanup.sanitize('He said "go" and "stop"') == 'He said "go" and "stop"'


def test_rejects_answering_a_question():
    q = "what is the capital of france and why is it famous"
    answer = ("The capital of France is Paris. It is famous for the Eiffel Tower, the Louvre, "
              "its cafes, fashion houses and a long history as a centre of art and culture.")
    assert cleanup.clean(q, FakeLLM(answer), model="m") == (q, "fallback-unfaithful")


def test_rejects_summarising():
    long = "okay so first we migrate the ledger then we backfill the balances then we flip the flag and watch the dashboards for an hour"
    assert cleanup.clean(long, FakeLLM("Migrate ledger."), model="m") == (long, "fallback-unfaithful")


def test_rejects_empty_output():
    assert cleanup.clean(RAW, FakeLLM(""), model="m") == (RAW, "fallback-unfaithful")


def test_accepts_heavy_filler_removal_on_short_input():
    raw = "um uh like so yeah ship it"
    assert cleanup.clean(raw, FakeLLM("Ship it."), model="m") == ("Ship it.", "ok")


def test_max_tokens_generous_for_indic_scripts():
    llm = FakeLLM("ठीक है, कल डिप्लॉय करो।")
    cleanup.clean("ठीक है उह कल डिप्लॉय करो", llm, model="m")
    assert llm.calls[0]["max_tokens"] >= len("ठीक है उह कल डिप्लॉय करो")
