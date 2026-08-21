# Copilot Instructions

Follow the repository root `AGENTS.md` and the nearest scoped `AGENTS.md`.

Implement the numbered Stories under `.github/story/` in order. The current Story is the
lowest-numbered Story with unfinished checklist items. Work only on that Story unless the user
explicitly changes the scope, and mark an item complete only after its implementation is finished.

When customer APIs or business rules are unavailable, continue with the Canonical contract and
Mock MES. Record temporary assumptions clearly and do not present them as confirmed customer
behavior. Never invent permission, tenant-isolation, or sensitive-data rules.

Stories are simple human-reviewed checklists, not machine-evaluated work items. Do not add status
fields, dependency graphs, risk scores, acceptance sections, or completion ratings. Run the
engineering checks relevant to the code changed, then summarize the completed work and remaining
assumptions for the user's review.