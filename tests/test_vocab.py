from spoke.config import SEED_BLOCKLIST, SEED_REPLACEMENTS, SEED_VOCAB
from spoke.vocab import (
    PROMPT_TOKEN_BUDGET,
    WHISPER_PROMPT_TOKEN_LIMIT,
    apply_replacements,
    build_prompt,
    estimate_tokens,
    is_hallucination,
    is_prompt_echo,
)

R = SEED_REPLACEMENTS


def test_basic_replacements_case_insensitive():
    assert apply_replacements("we write it in type script", R) == "we write it in TypeScript"
    assert apply_replacements("Type Script is typed", R) == "TypeScript is typed"
    assert apply_replacements("TYPE-SCRIPT", R) == "TypeScript"


def test_multiword_and_punctuated_keys():
    assert apply_replacements("the next js app and fast api server", R) == "the Next.js app and FastAPI server"
    assert apply_replacements("open a pr on git hub and postgre sql", R) == "open a pr on GitHub and PostgreSQL"
    assert apply_replacements("push to super base.", R) == "push to Supabase."


def test_whole_words_only():
    assert apply_replacements("typescripted githubs", R) == "typescripted githubs"
    assert apply_replacements("seriously", R) == "seriously"


def test_idempotent_on_canonical_forms():
    s = "Next.js, FastAPI, TypeScript, PostgreSQL, pnpm."
    assert apply_replacements(s, R) == s
    assert apply_replacements(apply_replacements("next js", R), R) == "Next.js"


def test_sentence_end_after_dotted_term():
    assert apply_replacements("we use next.js.", R) == "we use Next.js."


def test_replacement_value_with_backslash_is_literal():
    assert apply_replacements("path", {"path": r"C:\temp"}) == r"C:\temp"


def test_prompt_contains_vocab_and_fits_limit():
    p = build_prompt(SEED_VOCAB)
    assert p.startswith("GitHub, TypeScript")
    assert "Vocabulary" not in p  # a label word gets echoed back by Whisper
    for term in SEED_VOCAB:
        assert term in p
    assert estimate_tokens(p) <= PROMPT_TOKEN_BUDGET < WHISPER_PROMPT_TOKEN_LIMIT


def test_prompt_truncates_keeping_priority_order():
    vocab = [f"Term{i:03d}Something" for i in range(200)]
    p = build_prompt(vocab)
    assert "Term000Something" in p
    assert "Term199Something" not in p
    assert estimate_tokens(p) <= PROMPT_TOKEN_BUDGET


def test_empty_prompt():
    assert build_prompt([]) == ""
    assert build_prompt(["", "  "]) == ""


def test_hallucinations_dropped():
    for text in ["Thank you for watching.", "Thanks for watching!", " you ", "You.",
                 "Subtitles by the Amara.org community", "", "...", "Transcribed by ESO"]:
        assert is_hallucination(text, SEED_BLOCKLIST), text


def test_real_speech_kept():
    for text in ["Thank you.", "Thank you for watching the deploy, it went fine.",
                 "You should check the ledger.", "Okay.", "The doc was translated by Priya."]:
        assert not is_hallucination(text, SEED_BLOCKLIST), text


def test_bad_regex_in_blocklist_does_not_crash():
    assert not is_hallucination("hello", ["re:(unclosed"])


def test_seen_mishearings_fixed():
    assert apply_replacements("push it to Sopabase", R) == "push it to Supabase"
    assert apply_replacements("deploy on kubernetes", R) == "deploy on Kubernetes"


def test_prompt_echo_detected():
    assert is_prompt_echo("GitHub, TypeScript, JavaScript, Python.", SEED_VOCAB)
    assert is_prompt_echo("Vocabulary: GitHub, TypeScript, Kubernetes", SEED_VOCAB)


def test_real_sentences_with_vocab_are_not_echo():
    assert not is_prompt_echo("We store every row in PostgreSQL.", SEED_VOCAB)
    assert not is_prompt_echo("Deploy Kubernetes", SEED_VOCAB)  # too short to judge
    assert not is_prompt_echo("FastAPI and Next.js talk to Supabase from the API layer", SEED_VOCAB)
