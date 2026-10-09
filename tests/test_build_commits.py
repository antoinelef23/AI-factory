"""Every app is a git project before any agent runs, and every build's work is a commit (H1.c).

So the app's .git is the factory's (mounted read-only in the sandbox), and the gates, IT's approval and the
publish all judge one commit: never a loose folder (audit A1, A19, A31, A32)."""

import subprocess

from factory.project import porcelain
from tests.conftest import ScriptedExecutor
from tests.test_claudo_build import PlanThenBuildAgent, ScriptedClaudo, at_ship_review


def log(app):
    return subprocess.run(
        ["git", "log", "--format=%s"], cwd=app, capture_output=True, text=True, encoding="utf-8"
    ).stdout.splitlines()


class WritingAgent(PlanThenBuildAgent):
    """A single build agent (below Claudo's rung) that writes code and never commits, like the real one."""

    def run(self, prompt, **kw):
        if "implementer of" in prompt:
            (kw["cwd"] / "app" / "feature.py").write_text("FEATURE = 1\n", encoding="utf-8")
        return super().run(prompt, **kw)


def to_ship_review_single_agent(foreman, agent):
    foreman.runner = agent
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(item, "business")
    return foreman.approve(item, "owner")


def test_the_scaffold_is_committed_before_the_agent_runs_and_the_agents_work_after(foreman):
    seen = {}

    class Spy(WritingAgent):
        def run(self, prompt, **kw):
            if "implementer of" in prompt:
                seen["log"] = log(kw["cwd"])  # what the app holds when the agent starts
            return super().run(prompt, **kw)

    item = to_ship_review_single_agent(foreman, Spy())
    app = foreman.app_dir(item)
    assert seen["log"] == ["chore: scaffold from the IT golden path"]
    assert log(app)[0] == f"feat({item.slug}): the app built by the build agent"
    assert porcelain(app) == [] and item.gated_sha
    assert "1 file(s) committed" in next(
        h["detail"] for h in item.history if (h["stage"], h["event"]) == ("build", "done")
    )


def test_the_lockfile_is_made_with_the_scaffold_only_when_an_agent_builds(foreman):
    locked = []
    foreman.locker = lambda app: locked.append(app) or (1, "resolution failed")
    item = to_ship_review_single_agent(foreman, WritingAgent())
    assert locked == [foreman.app_dir(item)]
    assert any("uv lock failed (1): resolution failed" in h["detail"] for h in item.history)


def test_an_offline_build_also_commits_its_lockfile_with_the_scaffold(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    app = foreman.app_dir(item)
    assert log(app) == ["chore: scaffold from the IT golden path"] and (app / "uv.lock").is_file()


def test_a_fix_agent_that_changes_nothing_after_claudo_commits_nothing(foreman):
    foreman.executor = ScriptedExecutor([(1, "E501 line too long"), (0, "ok")])
    item = at_ship_review(foreman, ScriptedClaudo(), PlanThenBuildAgent())  # its "fix" edits no file
    builds = [h["detail"] for h in item.history if (h["stage"], h["event"]) == ("build", "done")]
    assert any("the fix agent changed nothing" in d for d in builds)
    assert not [a for a in item.ship_acks if a["kind"] == "fixed_after_review"]
