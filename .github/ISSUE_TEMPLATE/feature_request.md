---
name: ✨ Feature request
about: Want to extend the template — describe the use case first
title: "[FEATURE] "
labels: ["enhancement", "needs-design"]
---

## Problem

What real-world problem are you trying to solve? Example: "I want to monitor
job postings from 5 specific companies via RSS, not generic news."

## Use case

Who is the user, what do they do, why does it matter. Skip hand-wavy "would be
nice" — concrete scenarios only.

## Proposed shape

How would you imagine the feature hooking into the existing pipeline? Which
component (`rss_fetcher`, `rewrite_and_score`, `image_pipeline`, `publisher`,
`tg_dispatch`, etc.) would change?

Sketch the API surface if you have a concrete idea:

```python
# Example: customs scorer
def score_one(candidate: dict, source_reliability: float) -> float:
    return candidate["base_score"] * source_reliability * 1.2
```

## Alternatives considered

What else did you consider before landing on this shape? Why is the proposed
shape better?

## Impact

- Which existing tests would need updating?
- Which docs sections need updating?
- Does this require a migration?
- Does this break the public API (env vars, cron contract, state machine)?

## Willingness to contribute

- [ ] I can submit a PR with the design discussion unblocked
- [ ] I would review but not implement
- [ ] I just want the feature, no time to contribute

## Checklist

- [ ] I have searched [existing issues](https://github.com/DimbikeY/media-fabrique-template/issues) for duplicates
- [ ] I have read the [Roadmap](README.md#roadmap) — this isn't already in progress
- [ ] I have checked the [extension points](README.md#customize-where-to-fork-for-your-topic) — maybe the template already supports this
