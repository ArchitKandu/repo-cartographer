"""Which agent runs on which model, and on whose budget.

No model is built here and no request is made. Everything asserted is a decision
`models.py` makes from the environment before anything is constructed, which is
why `ModelChoice` exists as a separate thing from the chat model it becomes.

The reason this file exists is that every mistake available in that module is
silent. A profile key of the wrong shape does not raise — deepagents leaves its
default sub-agent in place and the run looks normal. A role name misspelled in
`ASSIST_ROLES` moves nothing. An assist model that resolves to the primary halves
the budget it was supposed to double, and reports the same numbers either way.
And a retry cap that drifts back above the SDK default stops capping anything at
all while remaining a line of code that says `max_retries`.

None of that shows up in an answer. It shows up as a 429 a week later, or as a
usage dashboard reading 17 against a limit of 15.
"""

from __future__ import annotations

import pytest
from langchain_google_genai import ChatGoogleGenerativeAI

from repo_cartographer import models
from repo_cartographer.models import (
    _MAX_RETRIES,
    _REQUESTS_PER_MINUTE,
    DOC_WRITER,
    EXPLORER,
    ORCHESTRATOR,
    ROLES,
    ModelChoice,
    _assist_roles,
    _provider_of,
    _rate_limiter,
    _resolve_assist,
    _resolve_primary,
    choice_for,
)

GOOGLE = ModelChoice("google", "gemini-3.5-flash-lite")
GOOGLE_ASSIST = ModelChoice("google", "gemini-3.1-flash-lite")
FREE = ModelChoice("openrouter", "nvidia/nemotron-3-super-120b-a12b:free")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A clean environment, because the developer's own .env is already loaded.

    `models.py` calls `load_dotenv(override=True)` at import, so every key in the
    real .env is in `os.environ` by the time a test runs — and a test asserting
    "no OpenRouter key means no assist model" would pass or fail depending on
    whose machine it ran on. Clearing them makes each test state its own world.
    """
    for name in (
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "LLM_PROVIDER",
        "GOOGLE_MODEL",
        "OPENROUTER_MODEL",
        "ASSIST_MODEL",
        "ASSIST_ROLES",
        "REQUESTS_PER_MINUTE",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --------------------------------------------------------------------------- #
# The profile key, whose wrong answer is silence
# --------------------------------------------------------------------------- #


def test_a_gemini_profile_key_is_joined_with_its_provider() -> None:
    assert GOOGLE.profile_key == "google_genai:gemini-3.5-flash-lite"


def test_an_openrouter_profile_key_is_the_bare_identifier() -> None:
    """One colon, not two, and this is the whole reason for the special case.

    deepagents refuses a profile key carrying more than one colon, and an
    OpenRouter name already has one (`vendor/model:free`). Prefixing it would not
    error — the lookup would fall through to the provider-wide `openai`
    registration, which is far broader than intended, and the general-purpose
    sub-agent this project switches off would quietly come back.
    """
    assert FREE.profile_key == "nvidia/nemotron-3-super-120b-a12b:free"
    assert FREE.profile_key.count(":") == 1


@pytest.mark.parametrize(
    ("name", "provider"),
    [
        ("gemini-3.5-flash-lite", "google"),
        ("gemini-3.1-flash-lite", "google"),
        ("nvidia/nemotron-3-super-120b-a12b:free", "openrouter"),
        ("inclusionai/ling-3.0-flash:free", "openrouter"),
        ("openrouter/free", "openrouter"),
    ],
)
def test_a_provider_is_read_off_the_shape_of_the_model_name(name: str, provider: str) -> None:
    """A slash means OpenRouter. It is inference, so it is worth pinning.

    The alternative was a second environment variable next to `ASSIST_MODEL`, and
    the two namespaces cannot collide — no Gemini name contains a slash, and every
    OpenRouter identifier does.
    """
    assert _provider_of(name) == provider


# --------------------------------------------------------------------------- #
# The primary
# --------------------------------------------------------------------------- #


def test_the_primary_follows_whichever_key_is_present(env: pytest.MonkeyPatch) -> None:
    env.setenv("OPENROUTER_API_KEY", "test")
    assert _resolve_primary() == FREE

    env.setenv("GEMINI_API_KEY", "test")
    assert _resolve_primary() == GOOGLE, "GEMINI_API_KEY should win when both are set"


def test_an_explicit_provider_beats_the_key_that_happens_to_be_there(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("OPENROUTER_API_KEY", "test")
    env.setenv("LLM_PROVIDER", "openrouter")
    assert _resolve_primary() == FREE


def test_no_key_at_all_names_both_ways_to_fix_it(env: pytest.MonkeyPatch) -> None:
    """The message is the test: this failure greets anyone cloning the project."""
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        _resolve_primary()


def test_a_provider_asked_for_without_its_key_says_which_key(env: pytest.MonkeyPatch) -> None:
    env.setenv("OPENROUTER_API_KEY", "test")
    env.setenv("LLM_PROVIDER", "google")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not set"):
        _resolve_primary()


def test_an_unrecognised_provider_is_refused_rather_than_guessed(env: pytest.MonkeyPatch) -> None:
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(RuntimeError, match="not recognised"):
        _resolve_primary()


# --------------------------------------------------------------------------- #
# The assist, and the budget it is there to be separate from
# --------------------------------------------------------------------------- #


def test_the_assist_is_the_other_provider(env: pytest.MonkeyPatch) -> None:
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("OPENROUTER_API_KEY", "test")
    assert _resolve_assist(GOOGLE) == FREE


def test_one_key_means_no_assist_and_no_complaint(env: pytest.MonkeyPatch) -> None:
    """The split is capacity, not correctness, so its absence is not an error.

    A clone with a single key must run exactly as it did before any of this
    existed, every agent on the primary.
    """
    env.setenv("GEMINI_API_KEY", "test")
    assert _resolve_assist(GOOGLE) is None


def test_a_google_assist_is_a_different_gemini_than_the_primary(env: pytest.MonkeyPatch) -> None:
    """Two models, two budgets — the same model twice would be one budget.

    This is the case where the primary is OpenRouter and the Gemini key is the
    spare one. Returning `_DEFAULT_GOOGLE_MODEL` here would look right and buy
    nothing if that were also the primary, so the assist deliberately names the
    other Flash-Lite.
    """
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("OPENROUTER_API_KEY", "test")
    assist = _resolve_assist(FREE)
    assert assist == GOOGLE_ASSIST
    assert assist != GOOGLE


def test_an_explicit_assist_model_is_honoured(env: pytest.MonkeyPatch) -> None:
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("OPENROUTER_API_KEY", "test")
    env.setenv("ASSIST_MODEL", "inclusionai/ling-3.0-flash:free")
    assert _resolve_assist(GOOGLE) == ModelChoice("openrouter", "inclusionai/ling-3.0-flash:free")


def test_two_models_from_one_provider_are_still_two_budgets(env: pytest.MonkeyPatch) -> None:
    """Limits are published per model, so one key can still hold two budgets."""
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("ASSIST_MODEL", "gemini-3.1-flash-lite")
    assert _resolve_assist(GOOGLE) == GOOGLE_ASSIST


def test_an_assist_equal_to_the_primary_is_no_assist(env: pytest.MonkeyPatch) -> None:
    """Otherwise the doc-writer would be routed onto the budget it is leaving."""
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("ASSIST_MODEL", "gemini-3.5-flash-lite")
    assert _resolve_assist(GOOGLE) is None


def test_an_assist_model_without_a_key_is_refused_not_ignored(env: pytest.MonkeyPatch) -> None:
    """Ignoring it would silently leave every agent on one budget.

    The person who set the variable would have no way to tell it did nothing
    except by counting requests per provider in a trace.
    """
    env.setenv("GEMINI_API_KEY", "test")
    env.setenv("ASSIST_MODEL", "inclusionai/ling-3.0-flash:free")
    with pytest.raises(RuntimeError, match="needs a key for 'openrouter'"):
        _resolve_assist(GOOGLE)


# --------------------------------------------------------------------------- #
# Which roles move
# --------------------------------------------------------------------------- #


def test_by_default_only_the_doc_writer_moves(env: pytest.MonkeyPatch) -> None:
    """The default is the claim `models.py` argues for, so it is asserted.

    The doc-writer is the only agent whose failure mode is guarded twice — it
    cannot reach the repository (Phase 4) and its citations are checked with no
    model (Phase 6). The orchestrator's regressions on a weaker model are silent
    and unguarded, and the explorer both matters most and spends most.
    """
    assert _assist_roles() == {DOC_WRITER}
    assert ORCHESTRATOR not in _assist_roles()
    assert EXPLORER not in _assist_roles()


def test_the_roles_can_be_named_explicitly(env: pytest.MonkeyPatch) -> None:
    env.setenv("ASSIST_ROLES", "doc-writer, orchestrator")
    assert _assist_roles() == {DOC_WRITER, ORCHESTRATOR}


def test_an_empty_list_puts_everyone_back_on_one_model(env: pytest.MonkeyPatch) -> None:
    env.setenv("ASSIST_ROLES", "")
    assert _assist_roles() == frozenset()


def test_a_misspelled_role_is_refused(env: pytest.MonkeyPatch) -> None:
    """`ASSIST_ROLES=docwriter` would otherwise be a config change that does nothing."""
    env.setenv("ASSIST_ROLES", "docwriter")
    with pytest.raises(RuntimeError, match="not roles"):
        _assist_roles()


def test_an_unknown_role_cannot_be_asked_for_a_model() -> None:
    with pytest.raises(RuntimeError, match="not a role"):
        choice_for("link-checker")


def test_the_link_checker_is_not_a_role() -> None:
    """Phase 6's claim, asserted where a fourth role would be added.

    It holds no model, so there is nothing to route. A `link-checker` entry here
    would be the first sign that had stopped being true.
    """
    assert set(ROLES) == {ORCHESTRATOR, EXPLORER, DOC_WRITER}


def test_routing_falls_back_to_the_primary_with_no_assist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "ASSIST", None)
    assert {role: choice_for(role) for role in ROLES} == dict.fromkeys(ROLES, models.PRIMARY)


def test_the_profile_key_follows_the_orchestrator_and_not_the_doc_writer() -> None:
    """The harness profile is registered against the *main* agent's model.

    Routing the doc-writer elsewhere must not move it, or the general-purpose
    sub-agent `agent.py` disables comes back — silently, because a key that fails
    to match leaves the default in place rather than raising.
    """
    assert choice_for(ORCHESTRATOR).profile_key == models.MODEL_PROFILE_KEY


# --------------------------------------------------------------------------- #
# The buckets, and the retries that spend them without asking
# --------------------------------------------------------------------------- #


def test_one_model_means_one_bucket_however_many_agents_share_it() -> None:
    """The bug this replaced: three agents, three limiters, three times the spend.

    The provider counts every request against one model together, so agents on
    the same model must meter against one bucket. Identity is the assertion —
    two limiters at the same rate are not the same thing as one.
    """
    assert _rate_limiter(GOOGLE) is _rate_limiter(GOOGLE)


def test_two_models_mean_two_buckets() -> None:
    """And the converse, which is the whole point of the split.

    Sharing one bucket across two models would throttle a budget nothing was
    spending — the assist model's requests would queue behind the primary's for
    no reason at all.
    """
    assert _rate_limiter(GOOGLE) is not _rate_limiter(FREE)


@pytest.mark.parametrize("provider", ["google", "openrouter"])
def test_a_bucket_is_paced_below_its_provider_ceiling(provider: str) -> None:
    """Headroom, because a limiter meters our requests and not their accounting."""
    limiter = _rate_limiter(ModelChoice(provider, f"probe-{provider}"))
    per_minute = limiter.requests_per_second * 60
    assert 0 < per_minute < _REQUESTS_PER_MINUTE[provider]


def test_the_retry_cap_is_actually_below_the_default_it_replaces() -> None:
    """Asserting the point rather than the number.

    `max_retries` exists here because the default is six, and every one of those
    six is a real request the provider counts — against the per-minute limit that
    caused the rejection *and* the per-day limit that had nothing to do with it.
    Pinning `== 2` would keep passing if the library dropped its default to 1, at
    which point this line would be configuration that changes nothing. Pinning
    the relationship fails instead, which is the useful outcome.
    """
    sdk_default = ChatGoogleGenerativeAI.model_fields["max_retries"].default
    assert sdk_default > _MAX_RETRIES, (
        f"max_retries={_MAX_RETRIES} no longer caps anything: the client's own "
        f"default is now {sdk_default}"
    )


def test_a_retry_is_still_allowed_because_one_429_is_not_an_outage() -> None:
    """Zero would be wrong, and the API said so itself: 'retry in 1.010311967s'."""
    assert _MAX_RETRIES >= 1
