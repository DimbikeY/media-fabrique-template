---
name: 🐛 Bug report
about: Something is broken — paste logs + reproduction steps
title: "[BUG] "
labels: ["bug", "needs-triage"]
---

## What happened

A clear, concise description of the bug. Example: "When I run `tick=publish`, the pipeline logs `KeyError: 'wp_post_id'` after the LLM rewrite."

## What you expected

What you expected to happen instead.

## Reproduction steps

Minimal steps to reproduce the behavior:

1. Run `python main.py tick=fetch`
2. Run `python main.py tick=rewrite` on a specific candidate
3. Observe `KeyError: 'wp_post_id'` in `logs/rewrite.log`

## Environment

- **OS**: (Ubuntu 24.04, Debian 12, macOS 14, etc.)
- **Python**: (`python3 --version`)
- **Branch / commit**: (`git rev-parse HEAD`)
- **Deploy method**: (local dev / VPS / Docker)

## Logs

Paste the relevant log lines from `logs/*.log` (not the whole file unless it's
small). Use a code block with ` ```log` fence.

```log
2026-07-26 10:00:00 | INFO | tick=rewrite started
2026-07-26 10:00:01 | ERROR | KeyError: 'wp_post_id'
  File "/opt/<deploy-user>/media-fabrique-template/rewrite_and_score.py", line 142, in process_one
    row["wp_post_id"]
```

## Customizations

Anything you changed from the upstream template: RSS sources, prompts,
categories, scoring weights, cron schedule, etc. The more you changed, the
harder it is to reproduce — small isolated repros are easier to fix.

## Checklist

- [ ] I have searched [existing issues](https://github.com/DimbikeY/media-fabrique-template/issues) for duplicates
- [ ] I am running the latest commit on the `public-release` branch
- [ ] I have run the smoke tests — `python test_*_smoke.py` — and they pass
- [ ] I have read the [operational playbook](docs/architecture/08-operational-playbook.md) for known gotchas
