"""Roles are no longer self-declared: a verified identity, roles.toml, and four-eyes."""

from types import SimpleNamespace

import pytest

from factory import identity as identity_module
from factory.foreman import FactoryError
from factory.identity import GhIdentity, Identity, IdentityError, RoleMap, load_roles
from tests.test_change_flow import shipped_app


class FakeIdentity:
    def __init__(self, login):
        self.login = login

    def current(self):
        return Identity(self.login, "github")


ROLES = RoleMap({"business": ["alice"], "it": ["bob", "carol"], "owner": ["dave", "bob"]})


def as_person(foreman, login, four_eyes=False):
    foreman.identity, foreman.roles, foreman.cfg.four_eyes = FakeIdentity(login), ROLES, four_eyes
    return foreman


def to_spec_review(foreman):
    return foreman.run(foreman.intake("X", "an api", "poc"))


# ------------------------------------------------------------------ who may act


def test_without_identity_roles_stay_self_declared_and_say_so(foreman):
    item = foreman.approve(to_spec_review(foreman), "business", by="Anyone")
    assert item.approvals[-1]["by"] == "Anyone" and item.approvals[-1]["verified"] == ""


def test_the_verified_account_is_recorded_and_by_is_filled_in(foreman):
    item = to_spec_review(foreman)
    as_person(foreman, "alice")
    item = foreman.approve(item, "business")
    assert item.approvals[-1]["by"] == "alice" and item.approvals[-1]["verified"] == "github"


def test_an_account_without_the_role_is_refused(foreman):
    item = to_spec_review(foreman)
    as_person(foreman, "bob")  # IT and owner, not business
    with pytest.raises(
        FactoryError, match="bob .github. does not hold the role 'business'.*holds: it, owner"
    ):
        foreman.approve(item, "business")
    assert item.status == "waiting" and item.approvals == []


def test_by_cannot_claim_someone_else(foreman):
    item = to_spec_review(foreman)
    as_person(foreman, "alice")
    with pytest.raises(FactoryError, match="--by 'Bob' is not the verified identity 'alice'"):
        foreman.approve(item, "business", by="Bob")
    assert foreman.approve(item, "business", by="ALICE").approvals[-1]["by"] == "alice"  # case-insensitive


def test_an_unknown_account_holds_no_role(foreman):
    item = to_spec_review(foreman)
    as_person(foreman, "mallory")
    with pytest.raises(FactoryError, match="holds: none"):
        foreman.reject(item, "business", "no")


@pytest.mark.parametrize(
    ("action", "role"),
    [
        (lambda f, it: f.allow(it, "django", "it"), "it"),
        (lambda f, it: f.promote(it, "mvp", "it"), "it"),
    ],
)
def test_it_only_actions_check_the_actor_too(foreman, action, role):
    app = shipped_app(foreman)
    as_person(foreman, "alice")  # business only
    with pytest.raises(FactoryError, match=f"does not hold the role '{role}'"):
        action(foreman, app)


def test_merge_publish_and_abandon_check_the_actor(foreman):
    from tests.test_change_flow import ChangeAgent, to_plan_review

    app = shipped_app(foreman)
    foreman.runner = ChangeAgent(edit=False)
    ch = foreman.approve(to_plan_review(foreman, foreman.intake_change(app.slug, "A", "a thing")), "owner")
    ch = foreman.approve(ch, "it")
    as_person(foreman, "alice")
    for act in (lambda: foreman.merge(ch, "it"), lambda: foreman.abandon(ch, "owner", "no")):
        with pytest.raises(FactoryError, match="does not hold the role"):
            act()
    as_person(foreman, "carol")
    assert foreman.merge(ch, "it").merged and "carol" in ch.history[-1]["detail"]


# ------------------------------------------------------------------ four eyes


def test_four_eyes_one_person_cannot_decide_two_roles_of_one_item(foreman):
    item = to_spec_review(foreman)
    as_person(foreman, "alice", four_eyes=True)
    item = foreman.approve(item, "business")  # POC: next is the owner's plan review
    as_person(foreman, "bob", four_eyes=True)
    item = foreman.approve(item, "owner")  # bob approves the plan as owner...
    assert item.stage == "ship_review"
    with pytest.raises(FactoryError, match="four-eyes: bob already decided 'plan_review' as owner"):
        foreman.approve(item, "it")  # ...and cannot ship it as IT
    as_person(foreman, "carol", four_eyes=True)
    assert foreman.approve(item, "it").status == "shipped"


def test_without_four_eyes_one_person_may_hold_several_roles(foreman):
    item = foreman.approve(to_spec_review(as_person(foreman, "alice")), "business")
    as_person(foreman, "bob")
    assert foreman.approve(foreman.approve(item, "owner"), "it").status == "shipped"


# ------------------------------------------------------------------ roles.toml and the gh adapter


def test_roles_toml_is_validated(tmp_path):
    path = tmp_path / "roles.toml"
    with pytest.raises(IdentityError, match="does not exist"):
        load_roles(path)
    path.write_text('[roles]\nit = ["bob"]\nadmin = ["eve"]\nowner = "dave"\n', encoding="utf-8")
    with pytest.raises(IdentityError, match="unknown role 'admin'.*owner: expected a list"):
        load_roles(path)
    path.write_text('[roles]\nit = ["Bob"]\n', encoding="utf-8")
    roles = load_roles(path)
    assert roles.holds("bob", "it") and not roles.holds("bob", "owner")


def test_the_factorys_own_roles_toml_loads():
    from pathlib import Path

    roles = load_roles(Path(__file__).resolve().parents[1] / "roles.toml")
    assert roles.holds("antoinelef23", "it")


def gh(monkeypatch, rc, out, err=""):
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    monkeypatch.setattr(identity_module.subprocess, "run", fake)
    return calls


def test_gh_identity_is_the_authenticated_login_and_is_cached(monkeypatch):
    calls = gh(monkeypatch, 0, "antoinelef23\n")
    who = GhIdentity()
    assert who.current() == Identity("antoinelef23", "github") and who.current().login == "antoinelef23"
    assert calls == [["gh", "api", "user", "-q", ".login"]]  # asked once


def test_gh_identity_failures_are_clear(monkeypatch):
    gh(monkeypatch, 1, "", "HTTP 401: Bad credentials")
    with pytest.raises(IdentityError, match="gh auth login"):
        GhIdentity().current()
    monkeypatch.undo()  # the real subprocess: a missing executable
    with pytest.raises(IdentityError, match="not on PATH"):
        GhIdentity("definitely-not-gh").current()


def test_the_cli_uses_the_verified_identity_when_configured(factory_root, monkeypatch, capsys):
    from factory.cli import main

    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    toml = factory_root / "factory.toml"
    head, sep, tail = toml.read_text(encoding="utf-8").partition("[identity]")
    toml.write_text(
        head + sep + tail.replace('provider = "none"', 'provider = "github"', 1), encoding="utf-8"
    )
    (factory_root / "roles.toml").write_text('[roles]\nbusiness = ["alice"]\n', encoding="utf-8")
    monkeypatch.setattr("factory.cli.GhIdentity", lambda: FakeIdentity("alice"))
    assert main(["intake", "Orders", "--idea", "an api", "--maturity", "poc"]) == 0
    assert main(["run", "orders"]) == 0
    assert main(["approve", "orders", "--as", "business", "--by", "Bob"]) == 2
    assert "is not the verified identity 'alice'" in capsys.readouterr().err
    assert main(["approve", "orders", "--as", "business"]) == 0
    monkeypatch.setattr("factory.cli.GhIdentity", lambda: FakeIdentity("mallory"))
    assert main(["approve", "orders", "--as", "owner"]) == 2
    assert "mallory (github) does not hold the role 'owner'" in capsys.readouterr().err
