"""The secrets gate fails closed (H5: audit A54-A58, A101). Token-shaped strings are built at runtime."""

import subprocess

import pytest

from factory.gates import secrets_gate, secrets_in_history
from factory.project import commit_all, prepare_project

AWS = "AKIA" + "QWERTYUIOPASDFGH"
OPENAI = "sk-" + "proj-" + "Ab3" * 15
SERVICE = "sk-" + "svcacct-" + "Zx9" * 14
ANTHROPIC = "sk-" + "ant-" + "api03-" + "k" * 30


def scan(tmp_path, name, content, *, binary=False):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if binary else content.encode("utf-8"))
    return secrets_gate(tmp_path)


@pytest.mark.parametrize(
    ("name", "content", "label"),
    [
        ("settings.py", 'DB_PASSWORD = "s3cr3t-pass"\n', "hardcoded credential"),
        ("settings.py", "SECRET_KEY = 'django-insecure-abcdefgh'\n", "hardcoded credential"),
        ("config.json", '{"password": "hunter2hunter2"}\n', "hardcoded credential"),
        ("deploy.yml", 'client_secret: "abcdef123456"\n', "hardcoded credential"),
        (".env", "DB_PASSWORD=s3cr3tpass\n", "hardcoded credential"),
        ("prod.env", "export API_TOKEN=abcdefgh12\n", "hardcoded credential"),
        ("app.properties", "db.password_key = plainvalue99\n", "hardcoded credential"),
        ("client.mjs", f"const k = '{AWS}';\n", "AWS access key"),
        ("credentials", f"aws_access_key_id = {AWS}\n", "AWS access key"),
        ("src/App.vue", f"const k = '{OPENAI}'\n", "generic API key"),
        ("worker.go", f'key := "{SERVICE}"\n', "generic API key"),
    ],
)
def test_common_secret_forms_are_caught(tmp_path, name, content, label):
    result = scan(tmp_path, name, content)
    assert not result.ok and f"{name}:1: {label}" in result.detail


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("compose.yml", 'password: "${DB_PASSWORD}"\n'),
        ("ci.yml", "token: ${{ secrets.GITHUB_TOKEN }}\n"),
        ("app.py", 'API_KEY_HEADER = "X-Api-Key-Header"\n'),
        ("app.py", "password = get_password_from_vault()\n"),
        (".env.example", "DB_PASSWORD=${DB_PASSWORD}\n"),
        (".env.example", "DB_PASSWORD=<set-me>\n"),
        ("app.py", 'token = "short"\n'),
    ],
)
def test_references_and_code_are_not_secrets(tmp_path, name, content):
    assert scan(tmp_path, name, content).ok


def test_a_non_utf8_file_is_scanned_not_skipped(tmp_path):
    result = scan(tmp_path, "notes.txt", "caf\xe9 ".encode("latin-1") + AWS.encode(), binary=True)
    assert not result.ok and "notes.txt:1: AWS access key" in result.detail


def test_a_binary_file_is_skipped(tmp_path):
    assert scan(tmp_path, "blob.bin", b"\0\0\0" + AWS.encode(), binary=True).ok


def test_an_anthropic_key_is_reported_once_under_its_own_label(tmp_path):
    result = scan(tmp_path, "a.py", f"x = {ANTHROPIC}\n")
    assert result.detail.splitlines() == ["a.py:1: Anthropic API key"]


def test_tracked_vendored_code_is_scanned_because_it_is_published(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.py").write_text(f"K = '{AWS}'\n", encoding="utf-8")
    prepare_project(tmp_path)  # tracked
    result = secrets_gate(tmp_path)
    assert not result.ok and "vendor/lib.py:1: AWS access key" in result.detail


def test_an_unreadable_file_is_skipped(tmp_path, monkeypatch):
    from factory import gates

    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(gates.Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("locked")))
    assert gates.read_text_file(tmp_path / "a.txt") is None


def test_the_history_scan_reads_unquoted_env_values(tmp_path):
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    prepare_project(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / ".env").write_text("DB_PASSWORD=s3cr3tpass\n", encoding="utf-8")
    commit_all(tmp_path, "add env")
    (tmp_path / ".env").unlink()
    commit_all(tmp_path, "remove it")
    hits = secrets_in_history(tmp_path, f"{base}..HEAD")
    assert any(h.endswith(".env: hardcoded credential") for h in hits)
