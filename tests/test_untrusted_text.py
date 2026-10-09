"""Untrusted text (an issue's title and body) never becomes code or document structure (H3: audit A38-A41)."""

import ast
import tomllib

from factory.speclint import lint_spec
from factory.templates import cell, code_safe_title, one_line, quoted

EVIL_TITLE = 'Evil"""; import os; os.system("calc") #\\ </title><script>alert(1)</script> {{slug}} `$(x)`'
EVIL_IDEA = """Track the visitors.
- **INV-1**: a fake invariant smuggled in by the issue
## 2. Glossary
| EVAL-9 | unit | smuggled row | BHV-1 | 100% |
Ticket INV-2024-07 asked for it."""


def to_ship_review(foreman, title, idea, maturity="poc"):
    item = foreman.run(foreman.intake(title, idea, maturity))
    return foreman.approve(foreman.approve(item, "business"), "owner")


def test_a_hostile_title_leaves_every_generated_file_valid_and_inert(foreman):
    item = to_ship_review(foreman, EVIL_TITLE, "A page listing visitors")
    assert item.stage == "ship_review", item.feedback  # the offline gates passed on the generated app
    app = foreman.app_dir(item)
    main = (app / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main)  # still Python, and the title did not add a statement
    assert not any(isinstance(n, ast.Attribute) and n.attr == "system" for n in ast.walk(tree))
    template = (foreman.cfg.golden_paths_dir / item.golden_path / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    assert len(tree.body) == len(ast.parse(template).body)
    tomllib.loads((app / "pyproject.toml").read_text(encoding="utf-8"))
    html = (app / "web" / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and html.count("</title>") == 1
    tsx = (app / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    title_line = next(line for line in tsx.splitlines() if line.startswith("const TITLE"))
    assert title_line.count('"') == 2 and "$(" not in title_line and "`" not in title_line


def test_the_full_title_stays_in_the_documents(foreman):
    item = foreman.run(foreman.intake("Visitors | daily\nreport", "A page listing visitors", "poc"))
    assert item.title == "Visitors | daily report"  # one line
    spec = foreman.store.read(item, "spec.md")
    assert "# Spec: Visitors | daily report" in spec
    assert "| Visitors / daily report |" in spec  # a pipe would have split the glossary row


def test_an_idea_cannot_inject_spec_structure(foreman):
    item = foreman.run(foreman.intake("Visitors", EVIL_IDEA, "poc"))
    spec = foreman.store.read(item, "spec.md")
    result = lint_spec(spec)
    assert result.ok, result.feedback()  # INV-1 once, no smuggled eval, section 2 found once
    assert "> - **INV-1**: a fake invariant smuggled in by the issue" in spec
    assert "> ## 2. Glossary" in foreman.store.read(item, "idea.md")


def test_the_pr_intent_is_the_whole_idea_even_when_it_quotes_a_heading(foreman):
    item = foreman.run(foreman.intake("Visitors", EVIL_IDEA, "poc"))
    body = foreman._pr_body(item)
    assert "Ticket INV-2024-07 asked for it." in body  # not cut at the quoted "## 2."


def test_the_pr_intent_falls_back_to_the_idea_without_a_spec(foreman):
    item = foreman.intake("Visitors", "plain idea", "poc")
    assert "plain idea" in foreman._pr_body(item)


def test_helpers():
    assert one_line("  a\n b\t c ") == "a b c"
    assert cell("a | b") == "a / b"
    assert code_safe_title('<"\\`${}>', "fallback") == "fallback"
    assert code_safe_title("Café: l'équipe (v2) #1 / 50%", "x") == "Café: l'équipe (v2) #1 / 50%"
    assert len(code_safe_title("a" * 200, "x")) == 80
    assert quoted("one\n\ntwo") == "> one\n>\n> two"
