"""An LLM capability analyst on the keywords (P3-9): quotes checked, keywords kept, the radar decides."""

import json

from factory.agents import AgentResult
from factory.design import capabilities_prompt, detect_capabilities, grounded_capabilities

# The live1 case: no keyword (store, save, records, history, track...) yet a database is plainly needed.
IDEA = "An expense log: employees enter their expenses and managers list them by employee and month."
OFFERED = ["frontend", "database", "ai", "messaging"]


def test_the_keywords_alone_miss_the_database_of_the_live1_idea():
    assert "database" not in detect_capabilities(IDEA)


def test_a_quoted_capability_is_accepted():
    answer = json.dumps({"capabilities": [{"id": "database", "quote": "list them by employee and month"}]})
    assert grounded_capabilities(answer, IDEA, OFFERED) == (["database"], [])


def test_an_ungrounded_or_unknown_claim_is_dropped():
    answer = json.dumps(
        {
            "capabilities": [
                {"id": "ai", "quote": "summarise the expenses with a model"},  # not in the idea
                {"id": "blockchain", "quote": "expense log"},  # not a capability on offer
                {"id": "database", "quote": "LIST them  by employee"},  # case and spacing ignored
            ]
        }
    )
    accepted, problems = grounded_capabilities(answer, IDEA, OFFERED)
    assert accepted == ["database"]
    assert problems == ["ai: quote not found in the idea, ignored", "unknown capability 'blockchain' ignored"]


def test_a_non_json_answer_yields_nothing():
    assert grounded_capabilities("I think a database.", IDEA, OFFERED) == (
        [],
        ["the capability analyst's answer is not the expected JSON"],
    )


def test_the_prompt_offers_only_the_given_capabilities_and_forbids_choosing_technologies():
    prompt = capabilities_prompt(IDEA, ["database", "frontend"])
    assert "- database:" in prompt and "- frontend:" in prompt and "- ai:" not in prompt
    assert "Do not choose technologies" in prompt and IDEA in prompt


# ------------------------------------------------------------------ in the foreman


class Analyst:
    """Answers the capability analyst; any other prompt gets a plain answer."""

    name = "claude"

    def __init__(self, answer, ok=True):
        self.answer, self.ok, self.prompts = answer, ok, []

    def run(self, prompt, **kw):
        self.prompts.append((prompt, kw))
        if "capability analyst" in prompt:
            return AgentResult(self.ok, self.answer, 0.004, "" if self.ok else "boom")
        return AgentResult(True, "ok", 0.0)


def triage(foreman, analyst, idea=IDEA):
    foreman.runner, foreman.cfg.capability_analyst = analyst, True
    item = foreman.intake("Expense log", idea, "poc")
    foreman._do_triage(item)
    return item


def test_the_analyst_adds_what_the_keywords_missed_and_the_radar_still_chooses(foreman):
    analyst = Analyst(json.dumps({"capabilities": [{"id": "database", "quote": "by employee and month"}]}))
    item = triage(foreman, analyst)
    assert "database" in item.capabilities and item.cost_usd > 0
    assert any(h["event"] == "capabilities" and "analyst added database" in h["detail"] for h in item.history)
    _, kw = analyst.prompts[0]
    assert kw["tools"] == [] and kw["max_turns"] == 1 and kw["model"] == foreman.cfg.models.get("triage")
    from factory.design import choose_stack

    choice = choose_stack(foreman.radar, item.capabilities, "poc", [])
    assert "database" in choice.stack  # the TECHNOLOGY comes from the radar, never from the model


def test_keywords_are_never_lost_even_if_the_analyst_disagrees(foreman):
    item = triage(foreman, Analyst(json.dumps({"capabilities": []})), idea="A dashboard that stores records.")
    assert {"frontend", "database"} <= set(item.capabilities)


def test_a_failed_analyst_falls_back_to_the_keywords(foreman):
    item = triage(foreman, Analyst("", ok=False))
    assert item.capabilities == detect_capabilities(IDEA)
    assert any("analyst failed, keywords only" in h["detail"] for h in item.history)


def test_offline_or_switched_off_nothing_changes(foreman):
    item = foreman.intake("Expense log", IDEA, "poc")
    foreman._do_triage(item)  # runner None
    assert item.capabilities == detect_capabilities(IDEA)
    analyst = Analyst(json.dumps({"capabilities": [{"id": "database", "quote": "by employee and month"}]}))
    foreman.runner, foreman.cfg.capability_analyst = analyst, False
    item = foreman.intake("Expense log 2", IDEA, "poc")
    foreman._do_triage(item)
    assert "database" not in item.capabilities and analyst.prompts == []


def test_the_factory_turns_the_analyst_on(factory_root):
    from factory.config import load_config

    cfg = load_config(factory_root)
    assert cfg.capability_analyst is True and cfg.models.get("triage") == "sonnet"
