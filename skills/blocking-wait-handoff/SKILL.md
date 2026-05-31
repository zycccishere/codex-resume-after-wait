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

If you find contradictory, duplicated, stale, or obsolete chains, stop only the chains that you are 100% sure belong to the same session, same task lineage, or a directly related cleanup target:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py stop --task-id <task_id> --json
```

If ownership is not certain, do not stop or cancel the chain. Record the concern and leave it running for its owning session to handle.

If you also need to terminate the watched remote or local target, do not kill it manually first. Use the same safe interface so the script stops local watch/resume before it touches the target:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py stop --task-id <task_id> --also-stop-target --json
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
- Treat `--max-wait-seconds` as the Codex resume cadence, not the low-level
  process probe cadence. The watcher still probes the target using
  `--poll-seconds` while Codex is away.
- For relatively long-running jobs, such as multi-hour training or evaluation
  runs expected to take more than about 5 hours, prefer a longer maximum wait
  once the run is understood to be stable. A `2h` window is often more sensible
  than resuming every `30m` just to rediscover that the run is still progressing.
- This is a guideline, not a hard rule. Early in a run, when stability,
  correctness, data flow, or first metrics still need confirmation, a shorter
  maximum wait such as `1800` seconds (`30m`) is appropriate. After the run has
  passed those checks and the observed log/metrics look healthy, schedule the
  next handoff with a looser window such as `7200` seconds (`2h`).
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
- This is the recommended mode for almost all real blocking resumes, especially when the resumed agent may need SSH, `ps`, process cleanup, cross-directory inspection, or other non-trivial local/remote debugging actions.
- In practice, sandboxed resumes often break the very recovery flow they are supposed to continue.
- Use `--resume-preserve-approvals-and-sandbox` only when you explicitly need a restricted resume and you are confident the resumed work does not depend on those blocked capabilities.

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

With an observed process log for the dashboard:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 1800 \
  --host <remote_host> \
  --pid 12345 \
  --observed-log /absolute/path/to/run.log \
  --observed-log-label "Training Log"
```

Use `--observed-log` when the watched process already writes a useful
incremental log, or when creating one is cheap and clarifies progress beyond
simple process liveness. Do not force an observed log for every handoff. For a
remote watched target, `--observed-log` defaults to the same `--host`; use
`--observed-log-host` only when the log lives on a different host. Remote
observed-log paths must be absolute.

Override the default 2-hour maximum wait:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 21600 \
  --max-wait-seconds 14400 \
  --host <remote_host> \
  --pid 12345
```

Preserve the normal approval and sandbox settings on resume.
This is not the recommended default:

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

## Resume Retry

After the watched target exits or the max wait is reached, the watcher runs
`codex exec resume`. If that resume command exits nonzero and the new resume
log contains a retryable network-disconnect signature such as
`ERROR: Reconnecting...`, `stream disconnected before completion`, or
`backend-api/codex/responses`, the watcher does not mark the task failed
immediately. It waits 20 minutes and retries the same resume prompt.

Defaults:

- `--resume-retry-delay-seconds 1200`
- `--resume-retry-max-attempts 12`

Use `--resume-retry-max-attempts 0` for unlimited retry attempts. Non-network
resume failures still fail fast.

## Inspecting State

Start the local dashboard:

```bash
python3 /Users/zhangyc/.codex/skills/blocking-wait-handoff/scripts/codex_wait_handoff.py serve \
  --state-dir /Users/zhangyc/Desktop/WorkHub/tmp/codex-wait-handoff
```

The dashboard serves on `http://127.0.0.1:8765/` by default, or the next open
port if that one is busy. It auto-refreshes recent task JSON, watcher/resume
process presence, watch logs, resume logs, continuation prompts, and
`last-message` answers. It can open the selected task/log/prompt/answer file in
the local OS, and it can kill one active/live handoff by calling the safe
`stop --task-id <task_id>` path. The dashboard kill action does not pass
`--all-active` and does not pass `--also-stop-target`; stopping a watched target
still requires the explicit CLI command after applying the task-ownership gate.
When a task includes `observed_log`, the dashboard shows it in a dedicated
`Observed Log` tab. Local logs are read directly. Remote logs are read through
short-lived SSH tail requests from the dashboard server, so closing the browser
does not leave a persistent SSH/watch process behind.

If the shortcut wrapper is installed, open the dashboard quickly with:

```bash
handoff-ui
```

Check one task:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --task-id <task_id> --json
```

Check the active task for the current session:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --session-id "$CODEX_THREAD_ID" --json
```

Cancel a stale handoff only when you are 100% sure it belongs to your current session, your current task lineage, or an explicitly user-authorized cleanup target:

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

Stop every currently active local watch/resume chain in one shot:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py stop --all-active --json
```

Use `stop --all-active` only for explicit skill-mechanism tests or explicit user-authorized global cleanup. It is not allowed merely because another unrelated handoff is occupying the scheduler.

Stop a chain and then also stop the watched target safely:

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py stop --task-id <task_id> --also-stop-target --json
```

`cancel` is the local-only emergency path.
`stop` is the preferred safe interface.
When remote target shutdown is desired, always use `stop --also-stop-target` rather than manually killing the remote process first.
`stop` protects the calling process and its parent chain, so it will not kill the current Codex/resume session out from under itself.
If the current session is itself the stale resume chain you want to kill, run `stop` from a different session.

## Task Ownership Before Stopping

Stopping or cancelling a handoff is a task-ownership decision, not just local process cleanup.

- Never stop, cancel, or replace a handoff that belongs to another session, another user request, or another topic unless the user explicitly authorized that exact cross-task cleanup.
- "Another handoff is active" is not sufficient evidence. First prove ownership from task metadata, session id, parent thread, resume prompt, target PID/tmux, note, and the surrounding user instruction.
- Unless you have 100% confidence that the handoff belongs to you or is directly related to your current task, leave it running and report the conflict.
- If you discover that an unrelated task wrongly stopped or cancelled a handoff, treat it as an abnormal interruption and resume or restore the interrupted task unless newer explicit user instructions supersede it.
- When in doubt, prefer a blocked scheduling report over touching another task's watcher or remote target.

## Guardrails

- Do not use this for sub-5-minute waits.
- Only use `--allow-short-test` for deliberate mechanism testing.
- Do not use this for non-blocking jobs.
- Do not use vague patterns such as `python`, `train`, or `node`.
- Do not schedule a second handoff for the same session until the first one clears.
- Prefer putting any detailed continuation instructions into a prompt file under `tmp/`.
- Before every new `schedule`, run `active` and inspect the currently live watch/resume chains.
- If you find contradictory, duplicated, or obsolete chains, stop only chains that are proven to belong to your current session or directly related task lineage. Do not clean up unrelated tasks.
- If you need to terminate the watched job itself, never kill the remote target before stopping local watch/resume. Use `stop --also-stop-target` so the tool enforces the safe order.
- `stop` will not kill the process that invoked it or that process's parent chain. This is intentional self-protection.
- The default resume path now assumes the machine or container is already trusted and externally sandboxed.
- Treat `--resume-preserve-approvals-and-sandbox` as an exception path, not a best practice.
- If a resumed session is expected to use SSH, inspect remote state, stop stale waiters, or clean up processes, do not preserve sandbox.
