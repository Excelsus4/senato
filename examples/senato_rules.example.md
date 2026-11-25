# Project-Specific Senato Rules (Example)

Place a customized copy of this file at `<repo>/.agents/senato_rules.md` (or `.agents/council_rules.md`) to override Senato's dynamic seat selection with project-specific stakeholder lenses, mandatory reading lists, invariants, and decision-log paths.

Omit any section you want the orchestrator to handle dynamically.

---

## 1. Optional Fixed Stakeholder Seats & Reading Lists

| Seat | Optimization Axis | Mandatory Context (Pick Relevant Docs) |
|---|---|---|
| **Seat A (Architecture)** | System integrity, latency/memory budgets, reversibility | `docs/architecture.md`, `src/core/`, `tests/` |
| **Seat B (Product & Market)** | User value, onboarding clarity, competitive positioning | `README.md`, `docs/market_notes.md`, `docs/roadmap.md` |
| **Seat C (Operations & Scope)** | Maintenance cost, dependency hygiene, shipping velocity | `pyproject.toml`, `.github/workflows/`, `docs/decisions.md` |

---

## 2. Project Non-Negotiable Invariants (Injected Into Shared Block A)

1. Zero third-party runtime dependencies in the core package.
2. Backwards compatibility for all public CLI flags across minor versions.
3. No production code edits or commits during a read-only deliberation session.

---

## 3. Artifact Paths & Decision-Log Integration

- **Deliberation Directory:** `docs/senato/<YYYY-MM-DD>_<topic>/`
- **Decision Log:** `docs/decisions.md` (sequential `ADR-xxx` entries with What / Why / Rejected Alternatives & Preserved Dissent / Reversal Criteria & Impact Scope)
- **Approval Boundary:** Record Senato outcomes as `STATUS: PROPOSED` until the project owner explicitly approves them.
