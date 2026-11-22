---
name: tesse-archive
description: Use when the user explicitly asks to archive an active Tesse Forge strategy proposal, including abandoned or rejected work. Not for reviewing a run, archiving an OpenSpec change, or promoting.
---

# Arquivar uma proposta no Tesse Forge

**Preserve the package; do not certify its result.** Read the archive section of `docs/forge.md` first.

1. Verify the explicitly requested proposal ID and selected workspace (`forge/` by default), where active definitions live under `projects/<id>/`. Ask if the ID or intent is unclear. A rejection, a passing verdict, a review request or a lack of runs is not itself a request to archive.
2. For the named proposal and explicit request, use the internal Python helper `tesse.forge.archive.archive_proposal(root, proposal_id, confirmed=True)` in the Tesse environment. There is no archive CLI. Do not substitute `mv`, copy/delete, lock-file removal, or a process check followed by a move. The helper acquires the same nonblocking proposal lease as `run`; it checks the definition versions and IDs, verifies every published run, rejects collisions, moves the entire package (including iterations and recorded study state), and compares hashes before and after while holding that lease.
3. If the helper reports a busy proposal, an invalid artifact, or an existing destination, stop and report the reason. Do not move or overwrite data, repair evidence, kill an execution, or automatically retry. If the helper reports a verification error after the move, report the destination and error, preserve the package, and stop; do not claim it remained active or rerun the archive. On verified success, report `archive/<YYYY-MM-DD>-<id>/`, the unchanged proposal and run IDs, and that the proposal is no longer discovered by `validate --all`.

An abandoned proposal with no runs and a rejected proposal with intact evidence are both eligible. Archived iterations cannot resume; use `tesse-evidence` to read an exact archived run and `tesse-iterate` for a resume request on an active iteration. Archival does not resolve checkpoints or erase data exposures, write a `knowledge/` note, rerun, promote, or touch operational state.
