# Task 1 report: deterministic review issue classification

## Outcome

Implemented deterministic review issue normalization and transition policy in `apex/analyze.py`, and wired it into the analysis review node.

The implementation accepts reviewer string or mapping issues, normalizes messages/severity/blocking metadata, retries one material issue as `revise`, abstains on a repeated material issue, and preserves technical review failures as blocking abstentions. Legacy marker handling retains material conflict/contradiction safety while treating a bare `结论方向` phrase as minor.

## Tests

- RED: `pytest -q tests/test_analysis_graph.py -k 'review_transition'` failed with the expected missing `_review_transition` attribute.
- GREEN: focused review-transition tests: 4 passed.
- Full suite: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py`: 37 passed.
- `git diff --check`: passed.

## Self-review

- New transition output remains string messages for existing graph state and event consumers.
- Explicit business abstentions remain abstentions; technical failures remain abstentions with their failure message.
- The existing `_review_requires_revision` helper was kept compatible and updated so the bare `结论方向` phrase is not a material marker.
- No unrelated untracked files were modified or staged.

