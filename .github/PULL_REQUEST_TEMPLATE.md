## What this PR does

One-paragraph summary. Example: "Adds a `tg_dispatch.expired_clean` cron tick
to remove expired TG previews older than 24 hours, addressing Issue #42."

## Why

Link to the issue this PR addresses (if any). Example: "Fixes #42"

## How to test

Step-by-step manual test plan:

1. Run `python main.py tick=generate_for_tg`
2. Wait 25 hours (or set `expires_at` in the past for one draft)
3. Run `python main.py tick=janitor`
4. Verify the expired draft is gone from `tg_dispatch`

Automated test plan:

- [ ] New smoke test added: `test_<feature>_smoke.py`
- [ ] Existing smoke tests still pass: `for f in test_*_smoke.py; do python "$f"; done`
- [ ] No regression in `test_sprint_y_e2e_smoke.py` (19/19)

## Checklist

- [ ] I have read [CONTRIBUTING.md](README.md#contributing) (the section in README)
- [ ] I have a corresponding issue (or this is a trivial fix)
- [ ] My code follows the existing style (typing, docstrings, naming)
- [ ] I have updated docs in `docs/` if the public API changed
- [ ] I have updated `.env.example` if new env vars were added
- [ ] I have added a smoke test in `test_*_smoke.py`
- [ ] I have manually tested on a clean clone (if not trivial)

## Breaking changes

If yes, describe migration steps:

- Which env vars renamed?
- Which DB migrations needed?
- Which cron schedule changed?

## Notes

Anything else the reviewer should know: special test data, performance
characteristics, security implications, known limitations.
