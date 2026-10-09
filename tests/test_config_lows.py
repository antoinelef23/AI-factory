"""Configuration, identity and dependency policy fail closed (audit A68, A92, A96, A116-A118, A138, A143,
A149, A151, A158)."""

import json
from pathlib import Path

import pytest

from factory.config import ConfigError, load_config
from factory.design import GoldenPathError, load_golden_paths, pick_golden_path
from factory.foreman import FactoryError
from factory.gates import dependencies_gate
from factory.identity import IdentityError, load_roles
from factory.policy import PolicyError, parse_spec
from factory.radar import load_radar
from factory.sandbox import Sandbox
from tests.test_policy import make_app

REPO = Path(__file__).resolve().parents[1]
POC_GATES = 'poc = ["radar", "secrets", "immutable", "tests"]'


def set_toml(root, old, new):
    toml = root / "factory.toml"
    text = toml.read_text(encoding="utf-8")
    assert old in text, old
    toml.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("judge = false", 'judge = "false"', "judge = 'false' must be a bool"),
        ("max_build_attempts = 3", 'max_build_attempts = "three"', "must be a int"),
        ("[agent.models]", "[agent.models]\nweird = 3", "[agent.models] must map names to strings"),
        (POC_GATES, 'poc = "radar"', "poc must be a list of gate names"),
        (POC_GATES, 'poc = ["radar", "typo"]', "['typo'] are neither built-in"),
    ],
)
def test_a_wrong_type_or_name_is_a_config_error(factory_root, old, new, needle):
    set_toml(factory_root, old, new)
    with pytest.raises(ConfigError) as raised:
        load_config(factory_root)
    assert needle in str(raised.value)


def test_a_section_that_is_not_a_table_is_refused(factory_root):
    (factory_root / "factory.toml").write_text('agent = "claude"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[agent\] must be a table"):
        load_config(factory_root)


def test_values_are_read_with_their_types(factory_root):
    set_toml(factory_root, "[agent]", "[agent]\njudge_thinking_tokens = 0")
    cfg = load_config(factory_root)
    assert cfg.judge_thinking_tokens == 0 and isinstance(cfg.claudo_budget_usd, float)


def test_four_eyes_without_verified_identities_is_refused(factory_root):
    set_toml(factory_root, "four_eyes = false", "four_eyes = true")
    with pytest.raises(ConfigError, match="four_eyes needs provider"):
        load_config(factory_root)


def test_four_eyes_never_trusts_a_self_declared_approval(foreman):
    foreman.cfg.four_eyes = True
    foreman.identity = object()  # identity is on: _actor is replaced below
    foreman._actor = lambda role, by: ("bob", "github")
    item = foreman.intake("X", "an api", "poc")
    item.approvals.append({"stage": "spec_review", "role": "business", "by": "carol", "verified": ""})
    with pytest.raises(FactoryError, match="self-declared, so separation"):
        foreman._check_four_eyes(item, "it", "bob")


@pytest.mark.parametrize("body", ['roles = "it"\n', '[roles]\nit = ["alice", 3]\n'])
def test_a_malformed_roles_file_is_an_identity_error(tmp_path, body):
    (tmp_path / "roles.toml").write_text(body, encoding="utf-8")
    with pytest.raises(IdentityError):
        load_roles(tmp_path / "roles.toml")


def test_the_checked_proxy_container_is_the_configured_proxy():
    assert Sandbox(proxy="http://corp-proxy:3128").proxy_container == "corp-proxy"
    assert Sandbox(proxy="http://corp-proxy:3128", proxy_container="other").proxy_container == "other"
    assert Sandbox(proxy="not a url").proxy_container == "lab-egress-proxy"


def test_a_wildcard_with_an_ordering_operator_is_refused():
    with pytest.raises(PolicyError, match="a wildcard only goes with == or !="):
        parse_spec(">=1.*")
    assert parse_spec("==1.2.*") == [("==", "1.2.*")]


def test_a_package_without_licence_metadata_is_not_compliant(tmp_path):
    radar = load_radar(REPO / "radar.toml")
    app = make_app(tmp_path, {"mystery": "1.0"}, {"mystery": []})
    result = dependencies_gate(app, radar)
    assert not result.ok and "mystery 1.0: no licence in its metadata" in result.detail


def test_npm_versions_are_checked_against_the_radar(tmp_path):
    radar_file = tmp_path / "radar.toml"
    radar_file.write_text(
        '[[tech]]\nid = "react"\nname = "React"\ncategory = "frontend"\nring = "adopt"\nmatch = ["react"]\n'
        'version = ">=18"\n',
        encoding="utf-8",
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}", encoding="utf-8")
    lock = {"packages": {"": {}, "node_modules/react": {"version": "17.0.2"}, "node_modules/x": {}}}
    (web / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    result = dependencies_gate(tmp_path, load_radar(radar_file))
    assert not result.ok and "react 17.0.2 is locked (npm)" in result.detail


def write_manifest(root, body):
    (root / "gp").mkdir(parents=True, exist_ok=True)
    (root / "gp" / "golden.toml").write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("name = [", "golden.toml"),
        ('capabilities = ["backend", "teleport"]', "unknown capabilities"),
        ('capabilities = "backend"', "capabilities must be a list"),
        ('techs = "python"', "techs must be a list"),
        ('priority = "high"', "priority = 'high' must be an integer"),
        ('[gates]\ncommands = "true"', r"\[gates.commands\] must be a table"),
    ],
)
def test_a_malformed_golden_manifest_names_the_file(tmp_path, body, needle):
    write_manifest(tmp_path, body)
    with pytest.raises(GoldenPathError, match=needle):
        load_golden_paths(tmp_path)


def test_a_manifest_naming_a_tech_not_on_the_radar_is_an_error(tmp_path):
    write_manifest(tmp_path, 'techs = ["python", "pyest"]')
    paths = load_golden_paths(tmp_path)
    with pytest.raises(GoldenPathError, match=r"techs \['pyest'\] are not on the radar"):
        pick_golden_path(paths, load_radar(REPO / "radar.toml"), ["backend"], "poc")


def test_the_runtime_folders_are_not_part_of_the_factory_source():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"work/", "apps/", "radar.imported.toml"} <= set(ignored)
