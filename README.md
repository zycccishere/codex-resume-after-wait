# codex-resume-after-wait

Codex skill for handing off long blocking waits to a detached watcher and resuming the same session when the watched job exits.

## Install

```text
$skill-installer install https://github.com/zycccishere/codex-resume-after-wait/tree/main/skills/blocking-wait-handoff
```

Restart Codex after installation so the skill is discovered.

## What It Does

- hands a blocking wait off to an external watcher after a preflight window
- watches a local or remote PID, or a unique process pattern
- resumes the same Codex session with `codex exec resume`
- allows one active handoff per Codex session
- serves a local dashboard for recent handoffs, logs, prompts, answers, and task JSON
- optionally records an observed application log with `--observed-log`

## Notes

- Local `--pattern` mode expects `pgrep`.
- Remote monitoring expects `ssh`, `ps`, and `pgrep` on the remote host.
- Remote observed logs are read by the dashboard through short-lived SSH tail requests.
- Local PID watching is most reliable when Codex has full access or another mode that can inspect arbitrary local processes.
- In restricted sandboxes, local process inspection may fail even if the scheduler itself launches correctly.
- The resume path depends on the `codex` CLI being present on the same machine and supporting `codex exec resume`.
- For long, stable runs, use a longer `--max-wait-seconds` resume cadence such as 7200 seconds.
  Early in a run, a shorter 1800 second window is still useful while validating stability and metrics.

## Dashboard

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py serve \
  --state-dir /path/to/tmp/codex-wait-handoff
```

The dashboard shows recent handoff status, the scheduler note, continuation prompt,
watch log, resume log, optional observed log, task JSON, and the last resumed answer.
It also exposes single-task `Kill Handoff` and `Open File` controls while keeping
the mutation boundary narrow.

If this skill is installed under `~/.codex/skills/blocking-wait-handoff`, the
included shortcut can be placed on `PATH`:

```bash
ln -sf ~/.codex/skills/blocking-wait-handoff/scripts/handoff-ui ~/.local/bin/handoff-ui
handoff-ui
```

## Local smoke checks

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py --help
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --help
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py serve --help
python3 -m py_compile \
  skills/blocking-wait-handoff/scripts/codex_wait_handoff.py \
  skills/blocking-wait-handoff/scripts/wait_handoff_dashboard.py
```
