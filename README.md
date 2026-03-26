# codex-resume-after-wait

Codex skill for handing off long blocking waits to a detached watcher and resuming the same session when the watched job exits.

## Install

```text
$skill-installer install https://github.com/<owner>/codex-resume-after-wait/tree/main/skills/blocking-wait-handoff
```

Restart Codex after installation so the skill is discovered.

## What It Does

- hands a blocking wait off to an external watcher after a preflight window
- watches a local or remote PID, or a unique process pattern
- resumes the same Codex session with `codex exec resume`
- allows one active handoff per Codex session

## Current Limits

- Not intended to be macOS-only, but the current implementation is tested on macOS with Unix-like process tools.
- Local `--pattern` mode expects `pgrep`.
- Remote monitoring expects `ssh`, `ps`, and `pgrep` on the remote host.
- Windows has not been validated.
- Local PID watching is most reliable when Codex has full access or another mode that can inspect arbitrary local processes.
- In restricted sandboxes, local process inspection may fail even if the scheduler itself launches correctly.
- Remote host monitoring is often the safer path when local sandbox limits are tight.
- The resume path depends on the `codex` CLI being present on the same machine and supporting `codex exec resume`.
- The current implementation supports one active wait handoff per Codex session.

## Repository Layout

- `skills/blocking-wait-handoff/SKILL.md`: skill contract and invocation rules
- `skills/blocking-wait-handoff/agents/openai.yaml`: Codex UI metadata
- `skills/blocking-wait-handoff/scripts/codex_wait_handoff.py`: scheduler and watcher implementation

## Local smoke checks

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py --help
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --help
```
