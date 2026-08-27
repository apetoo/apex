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

## Review fix round 1

- RED: added regressions for raw `rework` preservation and the 000977 minor-issue-after-revision path; the raw rework integration initially failed because the transition seam converted it to `revise`, and the existing business-abstention regression exposed the need to retain material abstentions.
- GREEN: `_review_transition` now preserves `rework` for `controller.record_review`, retains material business abstentions, and allows minor issues to pass after one revision without a caller-side override.
- Added integration coverage proving raw rework emits a rework review and re-enters the researching/reason path, plus 000977 minor issue recovery.
- Focused tests: 7 passed. Full `tests/test_analysis_graph.py`: 40 passed. `git diff --check`: passed.
