"""Delivery: one private repo per app, one pull request per change. Real git against local bare repos."""

import subprocess
from types import SimpleNamespace

import pytest

from factory import delivery
from factory.config import load_config
from factory.delivery import DeliveryError, GhCli
from factory.foreman import FactoryError
from factory.project import current_branch
from tests.test_change_flow import ChangeAgent, git, shipped_app, to_plan_review
from tests.test_drift import radar_with


class FakeHost:
    """A git host whose 'remote' repositories are bare repos in a temp folder."""

    def __init__(self, base, existing=()):
        self.base, self.existing = base, set(existing)
        self.created, self.prs, self.state = [], [], "OPEN"

    def owner(self):
        return "acme"

    def repo_exists(self, owner, name):
        return f"{owner}/{name}" in self.existing

    def create_private_repo(self, owner, name, description):
        path = self.base / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
        self.created.append((owner, name, description))
        self.existing.add(f"{owner}/{name}")
        return str(path)

    def open_pull_request(self, owner, name, head, base, title, body):
        self.prs.append({"repo": f"{owner}/{name}", "head": head, "base": base, "title": title, "body": body})
        return f"https://example.test/{owner}/{name}/pull/{len(self.prs)}"

    def pull_request_state(self, url):
        return self.state

    def merge_on_host(self, name, head):
        """What IT does in the web UI: fast-forward the remote main to the pull request's branch."""
        bare = self.base / f"{name}.git"
        subprocess.run(["git", "update-ref", "refs/heads/main", f"refs/heads/{head}"], cwd=bare, check=True)
        self.state = "MERGED"


@pytest.fixture
def hosted(foreman, tmp_path):
    foreman.host = FakeHost(tmp_path / "remotes")
    (tmp_path / "remotes").mkdir()
    return foreman


def migration(foreman, tmp_path):
    app = shipped_app(foreman)
    foreman.radar, _ = radar_with(tmp_path, {"pydantic": "hold"})
    foreman.runner = ChangeAgent()
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Drop pydantic", "remove it", "migration"))
    return app, foreman.approve(foreman.approve(ch, "owner"), "it", by="bob")


# ------------------------------------------------------------------ a new app becomes a private repo


def test_publish_is_off_unless_a_host_is_configured(foreman):
    app = shipped_app(foreman)
    with pytest.raises(FactoryError, match="delivery is off"):
        foreman.publish(app, "it")


def test_only_it_publishes_and_only_an_approved_item(hosted):
    item = hosted.run(hosted.intake("Orders", "an api", "poc"))
    with pytest.raises(FactoryError, match="not approved yet"):
        hosted.publish(item, "it")
    app = shipped_app(hosted)
    for role in ("business", "owner"):
        with pytest.raises(FactoryError, match="only IT publishes"):
            hosted.publish(app, role)
    assert hosted.host.created == []


def test_publishing_a_new_app_creates_a_private_repo_and_pushes_head(hosted):
    app = shipped_app(hosted)
    app = hosted.publish(app, "it", "bob")
    owner, name, description = hosted.host.created[0]
    assert (owner, name) == ("acme", f"app-{app.slug}") and "maturity poc" in description
    assert app.repo == f"acme/app-{app.slug}" and app.repo_url
    folder = hosted.cfg.apps_dir / app.slug
    remote = hosted.host.base / f"app-{app.slug}.git"
    assert git(remote, "rev-parse", "main") == git(folder, "rev-parse", "HEAD")  # the remote has exactly HEAD
    assert hosted.store.load(app.slug).repo == app.repo  # persisted
    assert any(h["event"] == "published" for h in app.history)


def test_publishing_twice_reuses_the_repo_and_pushes_again(hosted):
    app = hosted.publish(shipped_app(hosted), "it")
    again = hosted.publish(app, "it")
    assert len(hosted.host.created) == 1 and again.repo_url == app.repo_url


def test_it_never_pushes_into_a_repo_it_did_not_create(hosted):
    app = shipped_app(hosted)
    hosted.host.existing.add(f"acme/app-{app.slug}")  # somebody else's repository with that name
    with pytest.raises(FactoryError, match="already exists and was not created by this factory"):
        hosted.publish(app, "it")
    assert hosted.host.created == [] and not app.repo_url


def test_the_prefix_and_owner_come_from_factory_toml(hosted):
    hosted.cfg.repo_prefix, hosted.cfg.delivery_owner = "svc-", "my-org"
    app = hosted.publish(shipped_app(hosted), "it")
    assert app.repo == f"my-org/svc-{app.slug}"


def test_a_remote_that_points_elsewhere_is_refused_not_overwritten(hosted, tmp_path):
    app = shipped_app(hosted)
    folder = hosted.cfg.apps_dir / app.slug
    subprocess.run(["git", "init", "-q"], cwd=folder, check=False)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.test/other.git"], cwd=folder, check=True
    )
    with pytest.raises(FactoryError, match="already points to"):
        hosted.publish(app, "it")
    assert git(folder, "remote", "get-url", "origin") == "https://example.test/other.git"


# ------------------------------------------------------------------ a change becomes a pull request


def test_a_change_needs_its_app_published_first(hosted, tmp_path):
    _, ch = migration(hosted, tmp_path)
    with pytest.raises(FactoryError, match="publish the app first"):
        hosted.publish(ch, "it")


def test_publishing_a_change_pushes_its_branch_and_opens_a_pull_request(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    ch = hosted.publish(ch, "it", "bob")
    pr = hosted.host.prs[0]
    assert (
        pr["repo"] == f"acme/app-{app.slug}" and pr["head"] == f"factory/{ch.slug}" and pr["base"] == "main"
    )
    assert pr["title"] == "[factory] Drop pydantic"
    assert "Approvals" in pr["body"] and "never merges" in pr["body"]
    assert pr["body"].count("Intent") == 1  # the spec's own section heading is not repeated under ours
    assert ch.pr_url.endswith("/pull/1") and ch.pr_state == "OPEN"
    remote = hosted.host.base / f"app-{app.slug}.git"
    assert git(remote, "rev-parse", f"factory/{ch.slug}")  # the branch is there
    assert git(remote, "rev-parse", "main") != git(
        remote, "rev-parse", f"factory/{ch.slug}"
    )  # main untouched


def test_republishing_a_change_updates_the_branch_without_a_second_pull_request(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    first = hosted.publish(ch, "it")
    hosted.publish(first, "it")
    assert len(hosted.host.prs) == 1


def test_local_merge_is_refused_once_a_pull_request_exists(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    ch = hosted.publish(ch, "it")
    with pytest.raises(FactoryError, match="factory sync"):
        hosted.merge(ch, "it", "bob")


# ------------------------------------------------------------------ sync after IT merged on the host


def test_sync_waits_while_the_pull_request_is_open(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    ch = hosted.sync(hosted.publish(ch, "it"))
    assert (ch.pr_state, ch.merged) == ("OPEN", False)
    assert current_branch(hosted.cfg.apps_dir / app.slug) == f"factory/{ch.slug}"


def test_sync_after_the_merge_fast_forwards_the_app_and_cures_the_drift(hosted, tmp_path):
    from factory.drift import scan_drift

    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    ch = hosted.publish(ch, "it")
    hosted.host.merge_on_host(f"app-{app.slug}", f"factory/{ch.slug}")
    ch = hosted.sync(ch)
    folder = hosted.cfg.apps_dir / app.slug
    assert ch.merged and ch.pr_state == "MERGED" and not ch.change_open
    assert current_branch(folder) == "main" and git(folder, "branch", "--list", f"factory/{ch.slug}") == ""
    assert "pydantic" not in (folder / "pyproject.toml").read_text(encoding="utf-8")
    drifted, _ = scan_drift(hosted.store.all(), hosted.radar, hosted.cfg.apps_dir)
    assert drifted == []
    assert hosted.intake_change(app.slug, "Next", "another").change_open  # the app is free again


def test_sync_reports_a_closed_pull_request_and_keeps_the_change_open(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    ch = hosted.publish(ch, "it")
    hosted.host.state = "CLOSED"
    ch = hosted.sync(ch)
    assert ch.pr_state == "CLOSED" and not ch.merged and ch.change_open


def test_sync_needs_a_pull_request(hosted):
    with pytest.raises(FactoryError, match="no pull request"):
        hosted.sync(shipped_app(hosted))


def test_sync_refuses_a_diverged_local_base(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    ch = hosted.publish(ch, "it")
    hosted.host.merge_on_host(f"app-{app.slug}", f"factory/{ch.slug}")
    folder = hosted.cfg.apps_dir / app.slug
    git(folder, "switch", "-q", "main")
    (folder / "local.txt").write_text("x", encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "local only")
    with pytest.raises(FactoryError, match="diverged"):
        hosted.sync(ch)


# ------------------------------------------------------------------ the GitHub adapter never goes public


@pytest.fixture
def gh_calls(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append({"argv": argv, "input": kw.get("input")})
        out = (
            '{"state": "MERGED"}' if "view" in argv and "--json" in argv else "https://github.com/x/y/pull/3"
        )
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    return calls


def test_gh_creates_repositories_private_and_has_no_way_to_make_them_public(gh_calls):
    url = GhCli().create_private_repo("acme", "app-x", "an app")
    argv = gh_calls[0]["argv"]
    assert argv[:3] == ["gh", "repo", "create"] and "--private" in argv
    assert "--public" not in argv and "--internal" not in argv
    assert url == "https://github.com/acme/app-x.git"
    import inspect

    assert "public" not in inspect.signature(GhCli.create_private_repo).parameters  # no visibility knob


def test_gh_opens_a_pull_request_with_the_body_on_stdin_and_never_merges(gh_calls):
    url = GhCli().open_pull_request("acme", "app-x", "factory/c", "main", "[factory] T", "BODY")
    argv = gh_calls[0]["argv"]
    assert argv[1:3] == ["pr", "create"] and gh_calls[0]["input"] == "BODY" and url.endswith("/pull/3")
    assert not any("merge" in a for a in argv)


def test_gh_reads_the_pull_request_state(gh_calls):
    assert GhCli().pull_request_state("https://github.com/acme/app-x/pull/3") == "MERGED"


def test_gh_errors_become_delivery_errors(monkeypatch):
    monkeypatch.setattr(
        delivery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom")
    )
    with pytest.raises(DeliveryError, match="boom"):
        GhCli().owner()


def test_a_missing_gh_is_a_clear_error():
    with pytest.raises(DeliveryError, match="not installed"):
        GhCli("definitely-not-a-real-gh-binary").owner()


def test_repo_exists_distinguishes_missing_from_a_real_failure(monkeypatch):
    def run(msg):
        monkeypatch.setattr(
            delivery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=msg)
        )

    run("GraphQL: Could not resolve to a Repository with the name 'acme/x'.")
    assert GhCli().repo_exists("acme", "x") is False
    run("HTTP 401: bad credentials")
    with pytest.raises(DeliveryError, match="401"):
        GhCli().repo_exists("acme", "x")


# ------------------------------------------------------------------ config


def test_delivery_defaults_to_off_and_rejects_unknown_providers(factory_root):
    assert load_config(factory_root).delivery_provider == "none"
    toml = factory_root / "factory.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace('provider = "none"', 'provider = "ftp"'), encoding="utf-8"
    )
    from factory.config import ConfigError

    with pytest.raises(ConfigError, match="expected 'none' or 'github'"):
        load_config(factory_root)


# ------------------------------------------------------------------ only the approved commit leaves (C5)


def commit(folder, name="extra.txt", text="x\n", message="more work"):
    (folder / name).write_text(text, encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", message)


def test_publish_refuses_uncommitted_work_and_pushes_nothing(hosted):
    app = shipped_app(hosted)
    folder = hosted.cfg.apps_dir / app.slug
    (folder / "extra.txt").write_text("never gated\n", encoding="utf-8")
    with pytest.raises(FactoryError, match="uncommitted changes never gated or approved: extra.txt"):
        hosted.publish(app, "it")
    assert hosted.host.created == [] and not app.repo_url


def test_publish_refuses_a_head_that_moved_after_the_approval(hosted):
    app = shipped_app(hosted)
    folder = hosted.cfg.apps_dir / app.slug
    commit(folder)
    with pytest.raises(FactoryError, match="the code moved after the approval"):
        hosted.publish(app, "it")
    assert hosted.host.created == []


def test_a_secret_committed_then_deleted_still_blocks_the_publish(hosted):
    app = shipped_app(hosted)
    folder = hosted.cfg.apps_dir / app.slug
    key = "AKIA" + "ABCDEFGHIJKLMNOP"  # assembled at runtime: no token-shaped literal in the repository
    commit(folder, "config.py", f'KEY = "{key}"\n', "add config")
    leaked = git(folder, "rev-parse", "--short=8", "HEAD")
    (folder / "config.py").unlink()
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "remove the key")
    app.approved_head = git(folder, "rev-parse", "HEAD")  # as if IT had approved this exact commit
    hosted.store.save(app)
    with pytest.raises(
        FactoryError, match=rf"secrets found in the commits to publish:\s+{leaked}:config.py: AWS"
    ):
        hosted.publish(app, "it")
    assert hosted.host.created == []


def test_a_legacy_item_without_a_recorded_approval_needs_accept_unverified(hosted):
    app = shipped_app(hosted)
    app.approved_head = ""  # approved before approvals were tied to a commit
    with pytest.raises(FactoryError, match="--accept-unverified"):
        hosted.publish(app, "it")
    published = hosted.publish(app, "it", "bob", accept_unverified=True)
    assert published.repo and any(h["event"] == "unverified" for h in published.history)


def test_an_unverified_legacy_publish_takes_the_tree_as_it_is(hosted):
    app = shipped_app(hosted)
    app.approved_head = ""
    folder = hosted.cfg.apps_dir / app.slug
    (folder / "extra.txt").write_text("legacy work\n", encoding="utf-8")
    hosted.publish(app, "it", accept_unverified=True)
    remote = hosted.host.base / f"app-{app.slug}.git"
    assert git(remote, "rev-parse", "main") == git(folder, "rev-parse", "HEAD")


def test_a_change_must_also_be_the_commit_it_approved(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    folder = hosted.cfg.apps_dir / app.slug
    commit(folder)  # a commit on factory/<slug> after IT approved
    with pytest.raises(FactoryError, match="the code moved after the approval"):
        hosted.publish(ch, "it")
    assert hosted.host.prs == []


def test_a_merged_change_moves_the_apps_approved_head_so_it_can_be_published_again(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.publish(hosted.store.load(app.slug), "it")
    ch = hosted.publish(ch, "it")
    hosted.host.merge_on_host(f"app-{app.slug}", f"factory/{ch.slug}")
    hosted.sync(ch)
    folder = hosted.cfg.apps_dir / app.slug
    assert hosted.store.load(app.slug).approved_head == git(folder, "rev-parse", "HEAD")
    again = hosted.publish(hosted.store.load(app.slug), "it")  # the new base is the approved one
    assert again.repo_url


def test_a_local_merge_also_moves_the_apps_approved_head(hosted, tmp_path):
    app, ch = migration(hosted, tmp_path)
    hosted.merge(ch, "it", "bob")
    folder = hosted.cfg.apps_dir / app.slug
    assert hosted.store.load(app.slug).approved_head == git(folder, "rev-parse", "HEAD")


# ------------------------------------------------------------------ IT approves what the gates judged


def test_approval_is_refused_if_the_app_moved_after_the_gates(foreman):
    item = foreman.run(foreman.intake("Z", "an api", "poc"))
    item = foreman.approve(item, "business")
    item = foreman.approve(item, "owner")  # builds and gates; waits for IT
    assert item.stage == "ship_review" and item.gated_sha == ""  # offline POC: not a git project yet
    item.gated_sha = "0" * 40  # pretend the gates judged another commit
    prepare = foreman.app_dir(item)
    from factory.project import prepare_project

    prepare_project(prepare)
    with pytest.raises(FactoryError, match="the app changed after the gates ran"):
        foreman.approve(item, "it")


def test_approval_commits_an_app_that_was_never_in_git_and_records_its_head(foreman):
    item = foreman.run(foreman.intake("Z", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    folder = foreman.app_dir(item)
    assert not (folder / ".git").exists()
    done = foreman.approve(item, "it")
    assert done.approved_head == git(folder, "rev-parse", "HEAD") and (folder / ".git").exists()
