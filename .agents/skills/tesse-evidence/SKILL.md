---
name: tesse-evidence
description: Use when the user asks to read a Tesse Forge run or diagnostic, explain its verdict, draft a round review, or assess or publish a reusable research finding. Not for reviewing repository code, registering a round review, or advancing the Forge workflow.
---

# Revisar evidência do Tesse Forge

**Inspecione e redija sem alterar o estado do estudo.** Para registrar uma revisão (gerenciada ou avulsa) ou avançar o Forge, use `tesse-iterate`. Revisão de código, testes ou specs do repositório é com `tesse-review`.

## Ler a evidência, sem redespachar

1. Identify the workspace, proposal and exact run or owning iteration. Read `docs/forge.md` for the evidence, review and knowledge contracts. Do not fall back to another proposal or run.
2. Use the CLI in the project Python environment. An iteration ID is not a run ID; inspect `evidence`, `next_action` and `state_revision` rather than guessing. Use the report's `diagnosis` for a managed iteration or the exact run's report directly:

   ```sh
   tesse forge iterate "$PROPOSAL" --root "$WORKSPACE" --iteration "$ITERATION" --json
   tesse forge iterate "$PROPOSAL" --root "$WORKSPACE" --iteration "$ITERATION" --diagnose --json
   tesse forge diagnose "$PROPOSAL" --root "$WORKSPACE" --run "$RUN" --json
   tesse forge review "$PROPOSAL" --root "$WORKSPACE" --run "$RUN"
   ```

   `diagnose` gives the measures; `review` as shown verifies the receipt. For a verified owner or deeper audit, use `tesse.forge.review.open_verified_run` with the exact proposal and run; read `study.binding.managed_owner` from its receipt when needed. A sound legacy run can lack diagnostics; report the limitation. For an explicitly selected archived or copied package, use `review_run(run_directory, expected_proposal=..., expected_run=...)` from that module for integrity and scope, or `tesse.forge.diagnose.diagnose_run` with the same verified identity for diagnostics. Report `legacy_run` or `confirmation_run` when diagnostics are unavailable. Stop on missing, corrupt or mismatched evidence. Do not query unverified SQLite, rerun or reconstruct evidence.
3. Report **technical completion**, **criterion verdict**, and **scope/strength of the conclusion** separately. Cite the block, measure and denominator; `null` is unavailable, not zero. Hypothetical outcomes are not official trades. A completed `failed` run is not a technical error; `not_evaluated` forecasts are not a comparison. A passing development run proves neither independent confirmation nor promotion approval.

## Redigir a revisão de uma rodada de estudo

1. Bind the v1 review to `run_id`, `evidence_sha256` and `declaration_hash` from that verified report. For each declared check, cite a concrete scalar using `references: [{block_id: ..., measure: ...}]`; e.g., `settlement.candidate.winrate` for a block with that declared check. Cite missing support when relevant. Answer the predeclared criteria with `supported`, `contradicted` or `inconclusive`, a rationale, and a separate decision: `stop`, `investigate`, or `test_adjustment`. An inconclusive review cannot release an adjustment. A follow-up keeps the frozen blocks and goes through the owning workflow; use the review fields documented in `docs/forge.md`.
2. Use `tesse-propose` if the review needs a new `next_study` declaration. A recommendation, including `test_adjustment`, is not approval. If the user only asked for evidence or a draft, deliver it without registering. If the review is part of an explicitly delegated offline workflow that includes registration, hand it off immediately to `tesse-iterate` without asking for per-review approval. Otherwise, hand off for registration only when the user approves the exact review. Carry the run ID, hashes and owning iteration if any; this skill never writes the review record itself.

## Publicar conhecimento reutilizável (aprovação separada)

1. Draft a note only if all four admission criteria in `docs/forge.md` are met: a verifiable claim; an auditable source with versioned contract and test or proposal/run IDs, evidence hashes and the specific verifying query; scope and revalidation conditions; a concrete decision another proposal can reuse without replay. A scoped negative result can qualify. Do not publish a provisional, unverifiable, or inconclusive result in `knowledge/`.
2. Show the proposed filename and exact Markdown draft and wait for the user's explicit approval of that draft. A passing verdict, recorded round review or general request to review is not approval. Only then use `tesse.forge.review.publish_knowledge_note(root, concept_id, note, approved=True)` with the approved `KnowledgeNote` fields; do not overwrite an existing note. No archival, promotion, or operational changes.
