# Copilot Instructions

Follow the repository root `AGENTS.md` and the nearest scoped `AGENTS.md`.

Implement the numbered Stories under `.github/story/` in order. The current Story is the
lowest-numbered Story with unfinished checklist items. Work only on that Story unless the user
explicitly changes the scope, and mark an item complete only after its implementation is finished.

When customer APIs or business rules are unavailable, continue with the Canonical contract and
Mock MES. Record temporary assumptions clearly and do not present them as confirmed customer
behavior. Never invent permission, tenant-isolation, or sensitive-data rules.

Stories use human-reviewed checklists and may include nested implementation items, ADO-style state,
dependencies, acceptance criteria, risks/open decisions, Technology Notes, and Release Notes. These
sections support implementation and review; they are not machine-evaluated gates and never override
checklist evidence. Mark a child item complete only after its implementation is finished and a parent
item complete only after all of its children are finished. Run the engineering checks relevant to the
code changed, then summarize the completed work and remaining assumptions for the user's review.