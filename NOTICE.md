# Notices and Attribution

This repository contains Blackboard-specific code plus code, documentation, architecture patterns, and design inspiration adapted from related local projects.

## Blackboard

Blackboard is a provider-based agentic coding workspace built around the principle that the workspace owns project state and providers supply intelligence or execution.

Blackboard-specific code is intended to be MIT-licensed unless otherwise noted in a future license file or source-level notice.

## Project Luna

Blackboard uses and adapts work from **Project Luna**, including architecture and implementation patterns related to:

- local-first AI orchestration
- provider routing and fallback patterns
- event bus and streaming UI patterns
- agentic ReAct/tool-loop workflows
- audit, memory, and project-state concepts
- FastAPI-backed local control surfaces

Project Luna should be credited when redistributing Blackboard or publishing derivative work based on these systems.

## Particle Engine WebOS / Particle Engine

Blackboard uses and references code, documentation, and design ideas from **Particle Engine WebOS** and **Particle Engine**, including concepts related to:

- WebGPU visual systems
- particle/ambient UI design
- vGPU/engine documentation references
- browser-first interface ideas
- related project documentation structure and presentation

Particle Engine references observed in the local project tree include the Particle Engine documentation and project identity associated with `https://particlerealms.online/` and the `BTSpaniel/ParticleEngine` project lineage.

## Third-party and upstream licensing

This notice is attribution-focused and does not replace upstream licenses.

Before publishing or redistributing this repository, review the original Project Luna, Particle Engine WebOS, Particle Engine, and any third-party dependency licenses. Preserve required copyright notices, license texts, and attribution statements for any copied or derived files.

## Dependency notes

Runtime Python dependencies are listed in `requirements.txt`. Frontend code is served as vanilla JavaScript modules without a build step.
