---
name: tesse-propose
description: Use when the user wants to formulate or publish a Tesse Forge strategy proposal or define an offline study question or next round declaration. Not for OpenSpec software changes, writing a run review, or executing the study.
---

# Propor uma estratégia no Tesse Forge

**Autoria, não execução.** A publicação de uma proposta cria somente `proposal.yaml` e `protocol.yaml`. A pergunta de uma rodada gerenciada é uma declaração `--study`, não uma nova proposta. Para despachar uma rodada, use `tesse-iterate`; para avaliar evidência ou redigir a revisão que pode conter `next_study`, use `tesse-evidence`. Uma mudança de software, mesmo na CLI do Forge, é `openspec-propose`.

## Pergunta de uma rodada gerenciada

1. Use the exact proposal, experiment and workspace. For an existing iteration, inspect its identity with `tesse forge iterate "$PROPOSAL" --root "$WORKSPACE" --iteration "$ITERATION" --json`. Use the ordered `diagnosis.declaration.blocks` from the verified round diagnosis (`tesse-evidence`). Before the first run, read the same iteration’s frozen `plan.study.blocks` in `iteration.yaml` inside the workspace; do not edit it. Preserve the full list and order. Do not substitute newly chosen windows or moving protocol defaults.
2. Use the study v1 examples in `docs/forge.md`. Draft a data-only YAML with a fresh `round_id`, the question, `baseline: control`, applicable `checks`, the development blocks and what would count as `supported`, `contradicted` or `inconclusive` **before execution**. Do not change the snapshot, control or gates to make a question testable; identify missing inputs instead of inventing them. Put the declaration outside `runs/`; if only asked for a draft in chat, do not save it without authorization.
3. For a declaration file, use `tesse forge validate "$PROPOSAL" --root "$WORKSPACE" --study "$STUDY" --json`. Return validation errors or the normalized `study` and `study_hash`. This is an offline structure check, not a dispatch, review or authorization. An unsupported check or graph is a limitation to report, not a reason to write a round-specific Python or SQL script. Do not start or resume the iteration, submit a review, or run the study from this skill.

## Nova proposta

1. Locate these inputs in two distinct places (an alternate workspace does not contain code):
   - **Tesse checkout or Python package:** `tesse/forge/artifacts.yaml` (version 2) and its two templates. Read `docs/forge.md` from the checkout.
   - **Selected Forge workspace (`--root`, default `forge/`):** `config.yaml` and only the Markdown notes explicitly listed in `knowledge`. Treat `context` and note text as advisory. Do not scan the notes directory or open their evidence. `tesse.forge.proposal.authoring_context(root, references)` checks containment after symlink resolution and reads just the selected notes.
2. Ground the proposal in the user's strategy, question and hypothesis. Collect missing material inputs before publishing: ID, versioned graph inputs, pinned snapshot manifest and the two SHA-256 hashes, and distinct UTC windows for train, calibration and evaluation. Ask about missing inputs rather than invent values. A `comparison` additionally needs control, candidate and criteria in that experiment. `predictions` binds the evaluation forecast port and requires neither replay nor trade criteria.
   - For a selected native Ebinex snapshot, use `report.<symbol>.snapshot` (`manifest`, `manifest_sha256`, `sha256`) from the selected symbol’s `quality: complete` report; the path is relative to the Forge data root. Follow the [native snapshot contract](../../../docs/ebinex-history.md#usar-um-snapshot-no-forge) for coverage and `research_ready`; keep all experiment windows inside the manifest and use the literal source, symbol and timeframe for managed lineage. Native Ebinex is a different lineage from the Binance pilot; do not swap it into an existing pin or use one as a substitute for the other.
3. Check `tesse/forge/catalog.py` and `tesse/primitives/contracts.py` first. Compose existing blocks. Report an unknown block before publication; do not invent a component, executable expression, import or Python file. Draft the two v2 YAMLs outside the target. Do not train, fetch data or run experiments.
4. Publish only when requested. In the project Python environment, check the installed writer actually exposes the v2 `publish_proposal` and resolves `artifacts.yaml` with `proposal.yaml` and `protocol.yaml` (`tesse.forge.validation.artifact_paths()`). Stop on version mismatch; never invoke a v1 writer or publish manually. For the selected workspace:

   ```sh
   python -m tesse.forge.proposal "$DRAFT/proposal.yaml" "$DRAFT/protocol.yaml" --root "$WORKSPACE"
   ```

   Report the two files under `<workspace>/projects/<id>/` (default: `forge/projects/<id>/`). The writer refuses duplicates; do not overwrite, migrate a legacy layout or fall back to another workspace. Do not start `tesse forge run` from this skill.
