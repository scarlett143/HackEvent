# HackEvents — Document Set

Hackathon discovery and reminder platform for professors and college management.
Version 0.1 · 2026-11-13 · Status: design approved, pre-build.

| # | Document | Purpose |
|---|---|---|
| 01 | [PRD](01-PRD.md) | What and why: users, scope, metrics, risks, phases |
| 02 | [TRD](02-TRD.md) | How: architecture, stack, services, pipelines, NFRs, config |
| 03 | [App Flow](03-App-Flow.md) | Screens, navigation, every user flow, edge states |
| 04 | [UI/UX Design Brief](04-UI-UX-Design-Brief.md) | Visual system, key screens, interaction, accessibility |
| 05 | [Backend Schema](05-Backend-Schema.md) | Postgres tables, keys, indexes, migrations |
| 06 | [API Integrations & Specs](06-API-Integrations-and-Specifications.md) | Internal REST API + external providers |
| 07 | [Security & Access](07-Security-and-Access.md) | Roles, auth, secrets, network, data protection |
| 08 | [AI Guardrails & Rules](08-AI-Guardrails-and-Rules.md) | LLM extraction rules + rules for coding agents |
| 09 | [Feature Ticket List](09-Feature-Ticket-List.md) | Epics, tickets, priority, acceptance criteria |
| 10 | [Deployment & Testing Runbook](10-Deployment-and-Testing-Runbook.md) | Provisioning, deploy, backups, tests, incidents |

## Open decisions

None. Q19 decided 2026-11-13: **DuckDNS** subdomain for the app, **Gmail API** for email. `APP_DOMAIN` is the single config value; Runbook B6–B7 has setup, Part I has the path to a college subdomain later.

## Unverified items

Items marked **[verify]** were not confirmed against official sources and must be checked before relying on them: Oracle limits and PAYG behaviour, Google OAuth production-mode token lifetime, provider prices and quotas, source endpoints.
