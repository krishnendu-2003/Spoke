from spoke.config import SEED_BLOCKLIST, SEED_REPLACEMENTS, SEED_VOCAB
from spoke.vocab import (
    PROMPT_TOKEN_BUDGET,
    WHISPER_PROMPT_TOKEN_LIMIT,
    apply_replacements,
    build_prompt,
    estimate_tokens,
    is_hallucination,
)

R = SEED_REPLACEMENTS


def test_basic_replacements_case_insensitive():
    assert apply_replacements("we store it in tiger beetle", R) == "we store it in TigerBeetle"
    assert apply_replacements("Tiger Beetle is fast", R) == "TigerBeetle is fast"
    assert apply_replacements("TIGER-BEETLE", R) == "TigerBeetle"


def test_multiword_and_punctuated_keys():
    assert apply_replacements("the next js app and nest js api", R) == "the Next.js app and NestJS api"
    assert apply_replacements("file the itr 4 and the fira", R) == "file the ITR-4 and the FIRA"
    assert apply_replacements("push to super base.", R) == "push to Supabase."


def test_whole_words_only():
    assert apply_replacements("prismatic erik", R) == "prismatic erik"
    assert apply_replacements("seriously", R) == "seriously"


def test_idempotent_on_canonical_forms():
    s = "Next.js, NestJS, TigerBeetle, ITR-4, pnpm."
    assert apply_replacements(s, R) == s
    assert apply_replacements(apply_replacements("next js", R), R) == "Next.js"


def test_sentence_end_after_dotted_term():
    assert apply_replacements("we use next.js.", R) == "we use Next.js."


def test_replacement_value_with_backslash_is_literal():
    assert apply_replacements("path", {"path": r"C:\temp"}) == r"C:\temp"


def test_prompt_contains_vocab_and_fits_limit():
    p = build_prompt(SEED_VOCAB)
    assert p.startswith("Vocabulary:")
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
