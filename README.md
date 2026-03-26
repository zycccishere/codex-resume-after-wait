# codex-resume-after-wait

Resume the same Codex session after long-running jobs finish.

Stop polling. Keep session continuity.

GitHub description:

> Resume the same Codex session after a long-running job or blocking process finishes. A scheduler and watcher for background jobs, process waits, and agent session continuity.

## What is in this repo

- `skills/blocking-wait-handoff/`
  - `SKILL.md`: the skill contract and invocation rules
  - `agents/openai.yaml`: UI-facing metadata for Codex skill lists
  - `scripts/codex_wait_handoff.py`: the scheduler and watcher implementation

## Why the layout looks like this

The skill itself stays in the standard Codex skill shape. Repo-level packaging lives outside the skill folder so the skill body stays concise and installation-friendly.

## Search Keywords

People looking for this kind of tool will usually search for terms like:

- `codex resume session`
- `resume codex after command finishes`
- `codex long running job`
- `codex background job`
- `codex wait for process`
- `ai agent scheduler`
- `agent resume after task completes`

This repository is intentionally named and described to match those queries.

## Suggested GitHub Topics

`codex`, `openai-codex`, `ai-agent`, `agentic-coding`, `job-scheduler`, `process-monitor`, `background-jobs`, `automation`, `developer-tools`, `long-running-tasks`

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

## Local smoke checks

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py --help
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --help
```

## Publishing checklist

1. Replace any placeholder repo URL in your release docs.
2. Restart Codex after installation so the new skill is discovered.

## Installing from a GitHub repo

Once published, install the skill from the GitHub directory URL or by copying `skills/blocking-wait-handoff/` into your Codex skills directory.

Example:

```text
$skill-installer install https://github.com/<owner>/codex-resume-after-wait/tree/main/skills/blocking-wait-handoff
```
