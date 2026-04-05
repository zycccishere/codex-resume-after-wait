---
name: blocking-wait-handoff
description: Hand off a genuinely blocking local or remote wait to an external watcher that resumes the same Codex session after the job exits or after a maximum wait window elapses. Use only when the launched job is expected to take more than five minutes, you cannot make progress without its result, and you have already verified that it survives an initial 20-second preflight without failing immediately.
---

# Blocking Wait Handoff

Use this skill only for a long wait that is both:

- longer than about 5 minutes
- on the critical path

If the job is shorter than that, use `sleep` directly.
If the job can run in the background while you do something else, do not use this skill.

For deliberate rapid iteration on the skill itself, you may bypass the 5-minute floor with:

- `--allow-short-test`

Use that only for explicit testing of the handoff mechanism, not for normal workflow.

## What This Skill Does

It schedules a detached watcher that:

1. verifies the target survives a 20-second preflight
2. keeps polling outside the active Codex session
3. resumes the same Codex session with `codex exec resume` after the watched process exits or after the maximum wait time is reached

The current session should stop after the handoff succeeds.
Each Codex session may have only one active wait handoff at a time.

## Default Script

Use:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule ...
```

Before scheduling a new handoff, always inspect the currently live watch/resume chains first:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py active --json
```

If you find contradictory, duplicated, stale, or obsolete chains, stop them before scheduling a new one:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py stop --task-id <task_id> --json
```

## Required Decision Gate

Before you schedule a handoff, confirm all of these:

- expected runtime is over 300 seconds
- the result is blocking the next step
- the command has already started
- the watched target is precise enough:
  - prefer `--pid`
  - use `--pattern` only when it is unique

For skill testing only, you may replace the first condition by adding `--allow-short-test`.

## Preflight Rule

The script already enforces a 20-second preflight by default.

- If the process dies during preflight, do not hand it off.
- Inspect the failure immediately in the current session.
- Only terminate the session after `schedule` returns `status: scheduled`.

## Max Wait

- The watcher has a maximum wait limit.
- Default: `7200` seconds (`2h`).
- When the limit is reached, the watcher resumes Codex even if the process is still alive.
- The resumed prompt explicitly says this was a timeout-style resume, not a success signal.
- After a timeout-style resume, first confirm whether the run is healthy and progressing.
- If it is healthy, schedule another blocking wait on the same precise target.
- If it is unhealthy or stuck, diagnose, fix, relaunch if needed, and only then schedule a new blocking wait.

## Current Limits

- This is not designed as a macOS-only skill.
- The current implementation is tested on macOS with Unix-like process tools.
- Local `--pattern` mode expects `pgrep`.
- Remote monitoring expects `ssh`, `ps`, and `pgrep` on the remote host.
- Windows has not been validated.
- Local process watching is most reliable when Codex is running with full access or another mode that can inspect arbitrary local PIDs.
- In tighter sandboxes, Codex may be unable to inspect or signal unrelated local processes.
- Remote host monitoring can still be a better fit when local sandbox restrictions are tight.
- The resume path depends on the `codex` CLI being available on the same machine and supporting `codex exec resume`.
- The current implementation allows only one active handoff per Codex session.
- By default, resumed sessions now use full permission and no sandbox via `codex exec resume --dangerously-bypass-approvals-and-sandbox`.
- Use `--resume-preserve-approvals-and-sandbox` only when you explicitly want the resumed session to keep the normal Codex approval and sandbox settings.

## Common Invocations

Local PID:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 1800 \
  --pid 12345 \
  --note "When resumed, collect the run outputs and continue the blocked task."
```

Remote PID:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 2400 \
  --host <remote_host> \
  --pid 12345 \
  --note "When resumed, inspect the remote outputs and update the experiment artifact."
```

Remote unique pattern:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 2400 \
  --host <remote_host> \
  --pattern "src.context_delta.cli --config configs/nqswap/qwen3_4b_to_14b.yaml"
```

With a richer continuation prompt:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 1800 \
  --pid 12345 \
  --resume-prompt-file tmp/wait-resume-prompt.md
```

Override the default 2-hour maximum wait:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 21600 \
  --max-wait-seconds 14400 \
  --host <remote_host> \
  --pid 12345
```

Preserve the normal approval and sandbox settings on resume:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 1800 \
  --pid 12345 \
  --resume-preserve-approvals-and-sandbox
```

Short test run for iteration:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --allow-short-test \
  --expected-seconds 30 \
  --pid 12345
```

## After Scheduling

Once the script reports `status: scheduled`:

1. note the `task_id` and `task_file`
2. stop the current Codex session cleanly
3. let the watcher resume the same session later

Do not keep polling manually after the handoff.

## Inspecting State

Check one task:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --task-id <task_id> --json
```

Check the active task for the current session:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --session-id "$CODEX_THREAD_ID" --json
```

Cancel a stale handoff:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py cancel --task-id <task_id>
```

List only live watch/resume processes:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py active --json
```

Stop a specific live chain cleanly:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py stop --task-id <task_id> --json
```

## Guardrails

- Do not use this for sub-5-minute waits.
- Only use `--allow-short-test` for deliberate mechanism testing.
- Do not use this for non-blocking jobs.
- Do not use vague patterns such as `python`, `train`, or `node`.
- Do not schedule a second handoff for the same session until the first one clears.
- Prefer putting any detailed continuation instructions into a prompt file under `tmp/`.
- Before every new `schedule`, run `active` and inspect the currently live watch/resume chains.
- If you find contradictory, duplicated, or obsolete chains, stop them first. Do not let them pile up.
- The default resume path now assumes the machine or container is already trusted and externally sandboxed.
- Use `--resume-preserve-approvals-and-sandbox` when that assumption does not hold.
