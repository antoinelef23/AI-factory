# AI Software Factory × Claudo — 3 plans

*Status: proposal, 2026-10-07. Engine: Claudo (`AI-Workflow-gates/_build`, github antoinelef23/Claudo).*

## The brief

Help companies turn **business ideas** into **production-grade apps** (from POV / POC / MVP) shipped **securely**, in a way that **respects the IT department's way of working**.

| Actor | Owns | Never does |
|---|---|---|
| **Business** | Value: the idea, the outcome, the acceptance criteria | Pick technologies |
| **IT department** | The **tech radar** (allowed technologies), security policies, golden paths, the final merge | Write each app |
| **AI factory** | Shipping: spec → design → code → evals → PR, *within* the radar | Merge, or use tech the radar forbids |

The factory must **read the company tech radar and adapt itself** to it.

## Market scan: what others do, and the gap

| Product | What it does | What it does *not* show |
|---|---|---|
| [Warp Factories](https://docs.warp.dev/factories/) | Foreman agent + triage/spec/implement/review agents; intake from Slack/GitHub/Linear/Jira; human spec & code checkpoints; "the factory never merges". Early Access. | How company standards are enforced |
| [Factory.ai Software Factory](https://docs.factory.com/software-factory/overview) | 24/7 agents across Triage → Code-gen → Validate → Release → Document → Monitor; coverage map per repo. Private Preview. | No documented org-standard / tech-policy enforcement |
| [softwarefactory.ai](https://www.softwarefactory.ai/) | Claims expert-system + GenAI full-stack generation and automated tech-stack selection | No concrete security or governance detail |
| [Backstage](https://developers.redhat.com/index%2ephp/articles/2024/06/03/backstage-cncf-project) (CNCF) | Catalog, golden-path software templates, tech-radar plugin: IT's paved road | Not an agentic factory |
| [Devin + Cognizant](https://www.nasdaq.com/press-release/cognizant-and-cognition-partner-scale-autonomous-software-engineering-and-deliver) | Autonomous engineer sold through a large integrator | Delivery model, not governance |
| [PRODYNA spec-driven SDLC](https://www.prodyna.com/insights/spec-driven-development-ai-software-engineering) | Prototype → specs → enterprise software | Methodology, not a product |

**The gap is our edge:** nobody documents a factory that is *governed by the customer's own tech radar*. Claudo already covers the hard half: spec-first triplet, anchored design, signed human checkpoints, eval gate, anti-empty-gate, trajectory guard, model evals.

## Common core: radar-as-code (all 3 plans)

Built on patterns Claudo already has:

1. **`radar.toml`**, maintained by IT through PRs. It mirrors the data-driven style of `lab/models/registry.toml`. One entry per technology:
   `name, quadrant (language|framework|platform|tool), ring (adopt|trial|assess|hold), owner, golden_path (template repo), policies (security/compliance refs), allowed_maturity`.
2. **Radar → design compiler.** It pre-fills `design.md` §2 (internal reference repos = golden paths) and §3 Stack (`Justified by` = radar entry). This is the existing "Anchored design" rule, made concrete per company.
3. **`radar_guard.py`**, a deterministic gate modelled on `brand_guard.py`, wired into `gate`/`gate-ci`, plan-lint and pre-commit.
   - It fails on any tech in **hold** or absent from the radar, whether it appears in design.md, tasks.md, pyproject/package manifests or Dockerfiles.
   - **assess** tech is allowed only up to POC.
   - **trial** tech is allowed up to MVP with an ADR.
   - **adopt** tech is allowed everywhere.
4. **Maturity ladder: POV → POC → MVP → Production.** Each rung is a profile that selects gates.

   | Rung | Gates |
   |---|---|
   | POV | spec + radar_guard |
   | POC | + evals |
   | MVP | + reviewer PASS, security scan, lock check |
   | Production | + full `gate-ci`, trajectory guard, IT sign-off checkpoint, human merge |

   **Promotion between rungs is a signed checkpoint** (existing HMAC approvals).
5. **Radar drift handling.** When IT moves a tech to *hold*, the factory lists affected apps and opens migration work items. The radar becomes a living control, not a PDF.

## Plan A: Radar-as-code inside Claudo (prove the core)

**What:** add the common core to Claudo, plus minimal intake.
- Business idea → GitHub issue (template) → `vibe-workshop` skill produces `spec.md` → radar compiler drafts `design.md` → Claudo orchestrates → PR. IT merges.
- Local queue of work items (`factory/queue/`); one item at a time per repo.

**Deliverables:** `radar.toml` schema + example radar; `radar_guard.py` + tests; maturity profiles; radar→design compiler; issue intake via `gh`; 1 golden eval proving a *hold* tech is rejected end-to-end.

| Effort | Pros | Cons / risks |
|---|---|---|
| ~4–6 weeks, 1 engineer | Lowest risk; reuses every Claudo guarantee; no vendor dependency; demonstrable differentiator | Single-tenant, CLI only, no dashboard: a strong demo, not yet a product |

## Plan B: Factory control plane (own product)

**What:** Plan A, plus a **foreman service** and three surfaces.
- **Business portal:** submit ideas, follow value / status, approve spec (checkpoint).
- **IT console:** edit radar & policies, approve promotions (POC→MVP→Prod), see radar drift and impacted apps.
- **Coverage dashboard:** per repo/app: rung, gates on, first-pass rate, cost (from `run_report.py`).
- Foreman runs **many Claudo runs in parallel**, one per repo/client, via the existing `run-project` mode; per-tenant radar, models and secrets; `SandboxRunner` (Docker) for isolation.
- Intake connectors later: Slack/Teams, Jira, Linear.
- `eval_models.py` + `EVOLUTION.md` drive model choice per role on measured proof.

| Effort | Pros | Cons / risks |
|---|---|---|
| ~4–6 months, small team | Strongest product and ESN story: one factory, many client codebases; full control of the value chain | Most build; enterprise must-haves (SSO, audit export, data residency, on-prem) before first sale |

## Plan C: Integrate into existing ecosystems (fastest to market)

**What:** don't build intake or UI. Plug Claudo in as the **governance and validation layer**.
- **Backstage** supplies catalog + golden-path templates + tech-radar plugin; the factory *reads* the radar from it, so IT keeps its tools.
- Coding done by **swappable runners** behind Claudo's `AgentRunner` interface: Claude Code (today), and later Warp / Factory.ai / Devin agents.
- Claudo gates every output (radar_guard, evals, trajectory guard, signed checkpoints) before a PR exists.

| Effort | Pros | Cons / risks |
|---|---|---|
| ~6–10 weeks to a pilot | IT already knows Backstage; vendor-neutral "trust layer" pitch; low UI cost | Warp/Factory.ai are early-access/preview: custom-runner hooks unverified; their roadmaps may absorb governance |

## Recommendation

**A → C → B.**
1. **A** proves the one thing nobody documents: radar-governed shipping.
2. **C** makes it sellable inside existing IT tooling.
3. **B** turns it into a product once a pilot client exists.

## Prerequisites / open risks

- **Windows blocker:** Claudo's `orchestrate.py` imports `fcntl` (POSIX-only), so `just gate-ci` is red natively on Windows (lint passes, test collection fails). Fix by running in WSL (Ubuntu present) or porting the lock. Do this before Plan A.
- No license on the Claudo repo yet; pick one before any company pilot.
- Verify Warp / Factory.ai extension points before committing to Plan C.

## Next step

Pick a plan. For A, the first work item is `work/radar-guard/spec.md` written with the `vibe-workshop` skill.
