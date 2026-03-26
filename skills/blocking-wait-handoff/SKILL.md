---
name: blocking-wait-handoff
description: Hand off a genuinely blocking local or remote wait to an external watcher that resumes the same Codex session after the job exits. Use only when the launched job is expected to take more than five minutes, you cannot make progress without its result, and you have already verified that it survives an initial 20-second preflight without failing immediately.
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
3. resumes the same Codex session with `codex exec resume` after the watched process exits

The current session should stop after the handoff succeeds.
Each Codex session may have only one active wait handoff at a time.

## Default Script

Use:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule ...
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

## Guardrails

- Do not use this for sub-5-minute waits.
- Only use `--allow-short-test` for deliberate mechanism testing.
- Do not use this for non-blocking jobs.
- Do not use vague patterns such as `python`, `train`, or `node`.
- Do not schedule a second handoff for the same session until the first one clears.
- Prefer putting any detailed continuation instructions into a prompt file under `tmp/`.
