# VP Demo Pitch Brief

## Core Message
This project reduces the effort of creating HLD and MDD documents by turning requirements and code intelligence into repeatable, reviewable design outputs.

## Problem
HLD and MDD creation is usually manual, slow, and dependent on tribal knowledge. Teams must read Confluence, inspect large monolith code paths, identify impacted modules, write diagrams, and keep documents consistent with SOP expectations. That creates delays, missed impact areas, and inconsistent architecture handoffs.

## Solution
MDD_NEW automates the design-document pipeline. It combines:
- Confluence requirements as product evidence.
- `graph.json` as codebase intelligence.
- Feature contract JSON as scoped ticket context.
- LLM generation for structured HLD and MDD content.
- Release-scoped artifacts for traceability and reuse.

The result is a code-aware HLD plus selected-module MDDs that can be previewed and downloaded.

## Demo Storyline
1. Show the demo UI and explain the inputs: Confluence page, product/release, ticket, and contract JSON.
2. Start or reuse Confluence ingestion artifacts.
3. Generate structured requirements from the ingested evidence.
4. Analyze code impact through `graph.json` and the feature contract.
5. Generate the HLD and show the architecture/diagram output.
6. Show the module catalog derived from HLD, requirements, and code graph.
7. Select modules and generate MDD DOCX files.
8. Close with the downloaded artifacts as the tangible output.

## Business Value
- Faster documentation cycle from requirements to design artifacts.
- More consistent HLD/MDD structure across teams and releases.
- Better traceability because every stage writes inspectable artifacts.
- Lower dependency on manual codebase discovery.
- Stronger handoff between product, architecture, and development teams.
- Reusable product/release context for demos, reviews, and future iterations.

## What Is Unique
This is not only document summarization. The key differentiator is code-aware generation: Confluence requirements are combined with Graphify code graph context and a ticket-level contract, so the generated design reflects both business intent and impacted implementation areas.

## Demo Success Criteria
- Requirements are generated from Confluence evidence.
- Codebase analysis identifies scoped impact from `graph.json` and contract JSON.
- HLD output is visible with architecture content and diagrams.
- Logical modules are available for selection.
- Selected MDD documents are generated and downloadable as DOCX.

## Conditions To Mention Upfront
- The demo needs valid Confluence access and configured LLM credentials.
- The monolith graph export must exist as `graph.json`.
- Each feature needs a contract JSON with ticket scope and seed symbols.
- Product and release values control artifact reuse.
- The current app is demo-oriented: open CORS and no app-level authentication should be hardened before production rollout.

## Likely VP Questions
- Why is this valuable? It shortens the path from feature requirements to architecture and module design documents.
- Why should we trust the output? The pipeline uses stored requirement artifacts, code graph artifacts, and scoped contracts instead of relying on an unconstrained prompt.
- Can teams inspect intermediate results? Yes. Requirements, code graph, HLD, MDD plans, and manifests are persisted as artifacts.
- Can it work across releases? Yes. Artifacts are organized by product and release.
- What happens when inputs are missing? The APIs return explicit errors and the missing stage can be rerun.
- Is it production-ready? It is demo-ready and architecturally extensible; production rollout should add app auth, restricted CORS, centralized secrets, audit logs, and deployment hardening.

## Closing Line
The pitch is simple: we are moving design documentation from manual reconstruction to an evidence-driven, code-aware, repeatable generation pipeline.
