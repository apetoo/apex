# Position-action effective-state final fix report

## Scope and outcome

This final fix wave closes the final-review findings against `f564c8f` without
touching unrelated user-owned untracked files. The changes make the frozen
position baseline authoritative at save time, ensure all downstream consumers
receive a canonical effective plan, and move proposal-shape validation before
independent review.

## Findings addressed

1. **Critical — save-time race guard**
   - Added `position_trading_fingerprint()` for exactly the ruled fields:
     existence/target code, `position_size_shares`, `stop_loss`, `target`, and
     the complete canonical `plan.scale_plan`. Name and other metadata are not
     part of the fingerprint.
   - `run()` now supplies its frozen baseline to `_finalize_position_action`.
   - Added `watchlist.apply_position_action_if_unchanged()`: one load,
     fingerprint comparison, and complete plan/stop/target replacement. A
     mismatch or closed position returns without saving the watchlist.
   - Finalization performs that operation before `journal.write_entry()` or
     trace writing. A mismatch returns
     `analysis_status=insufficient_evidence` and
     `outcome_reason=position_changed_during_analysis`, with zero journal or
     watchlist writes.
   - Replaced the closure regression that expected a journal entry. Added
     closed-position, trading-stop-change, and non-trading-name-change cases.

2. **Important — canonical effective plan**
   - `materialize_effective_position_plan()` now explicitly constructs
     `EffectivePositionPlan`; proposal-only `new_stop`, `new_target`, raw
     `scale_plan`, and compatibility metadata do not leak into effective state.
   - Moved full-exit normalization into
     `normalize_effective_scale_plan()`: `trim`, `pct=1.0`, finite nonpositive
     `new_stop` becomes `new_stop=None` before review, report validation,
     journaling, and watchlist persistence.
   - Deleted report-only ladder normalization. Equality regressions prove the
     canonical full-exit ladder is identical in effective state, journal, and
     watchlist.

3. **Important — stop/target validation and null compatibility**
   - Explicit `null` preserves the baseline stop/target and is recorded as
     `preserved` in `change_summary`.
   - Explicit booleans, non-numeric values, non-finite values, zero, and
     negative stop/target values are rejected before review. Positive finite
     values replace baseline values.

4. **Important — complete replacement-ladder validation**
   - `replace` now validates every submitted level: object type, `add|trim`,
     finite positive trigger, exactly one valid positive integer `shares` or
     finite `pct in (0,1]`, and a valid optional stop.
   - The sole exception is the canonical full-exit stop sentinel, which is
     normalized to `None` by the materializer.
   - The `record_position_action` schema now expresses required action/trigger
     fields, exactly-one shares/pct, positive numeric bounds, nonempty replace
     plans, and the conditional `replace -> scale_plan required` rule. The
     domain validator remains authoritative.

5. **Important — run-boundary authority**
   - Added a `run()` spy regression proving separate proposal, effective state,
     and a deep-copied frozen baseline reach finalization in the intended
     positions. Existing graph coverage continues to prove revise/preserve and
     report authority behavior.

6. **Minor — legacy compatibility warning**
   - Legacy missing or empty `scale_plan` adapts to `preserve` and adds the
     additive `compatibility_warnings` proposal field. Explicit-intent payloads
     receive no warning. The final journal retains this proposal metadata.

7. **Minor — exit report semantics**
   - Exit effective state retains stop/target for audit, but report-candidate
     construction and deterministic validation omit/ignore current-effective
     stop/target labels for `action=exit`.

8. **Minor — shared type documentation**
   - Updated `apex/schemas.py` to document `ladder_intent`, proposal versus
     effective fields, compatibility warnings, canonical full-exit stops, and
     whole-effective-plan persistence. Removed the obsolete “evolve, not
     replace” wording.

## TDD evidence

### RED

1. Added regressions for compatibility warnings, null preservation, invalid
   numeric values, complete ladder validation, full-exit normalization,
   save-time closure/change races, schema constraints, exit report semantics,
   and the `run()` finalization boundary.
2. Ran:

   ```text
   .venv/bin/python -m pytest -q tests/test_position_action_state.py \
     tests/test_position_action_finalize.py tests/test_analysis_graph.py \
     -k 'legacy_empty or explicit_null or explicit_stop_and_target or \
     materializer_normalizes or validates_every_ladder or closed_position_returns \
     or changed_trading_state or ignores_non_trading or empty_position_ladder_prompt \
     or exit_report or passes_distinct'
   ```

   Result: **16 failed, 1 passed**. Failures were the expected missing warning,
   null-overwrites-baseline behavior, absent validation/normalization/schema,
   absent finalization baseline parameter, and required exit labels.

3. Added the explicit-effective-state construction regression and ran:

   ```text
   .venv/bin/python -m pytest -q \
     tests/test_position_action_state.py::test_effective_state_excludes_proposal_only_patch_fields
   ```

   Result: **1 failed**, because the old materializer copied proposal-only
   fields into effective state.

### GREEN

1. Domain suite:

   ```text
   .venv/bin/python -m pytest -q tests/test_position_action_state.py
   ```

   Result: **18 passed**.

2. Required focused suites:

   ```text
   .venv/bin/python -m pytest -q tests/test_position_action_state.py \
     tests/test_position_action_dispatch.py tests/test_position_action_finalize.py \
     tests/test_analysis_graph.py tests/test_ladder_path_sim.py \
     tests/test_adherence.py tests/test_position_lifecycle.py
   ```

   Result: **180 passed in 1.54s**.

3. Complete suite, sandbox run:

   ```text
   .venv/bin/python -m pytest -q
   ```

   Result: **699 passed, 2 failed, 9 errors**. The nine errors are sandbox-only
   `PermissionError: [Errno 1] Operation not permitted` failures while binding
   the local `ThreadingHTTPServer` fixture in:

   - `test_process_path_exports_detail_and_first_level_comments`
   - `test_process_path_classifies_block_and_schema[/blocked-blocked-None]`
   - `test_process_path_classifies_block_and_schema[/captcha-blocked-None]`
   - `test_process_path_classifies_block_and_schema[/schema-schema_changed-override2]`
   - `test_process_path_enforces_total_request_quota`
   - `test_process_comment_pagination_stops_at_fifty`
   - `test_duplicate_url_jobs_preserve_all_sector_mappings`
   - `test_shared_url_failure_marks_every_mapping_and_retry_consumes_quota`
   - `test_completed_batch_rerun_is_idempotent`

4. The socket-dependent suite was rerun outside the sandbox:

   ```text
   .venv/bin/python -m pytest -q tests/test_eastmoney_guba.py
   ```

   Result: exit code **0** (all test dots printed).

5. Hygiene checks:

   ```text
   git diff --check
   rg -n '\\[DEBUG-' apex tests
   ```

   Result: `git diff --check` exit **0**; `rg` exit **1** with no output,
   confirming no temporary debug markers.

## Pre-existing failures deliberately left unchanged

The two remaining full-suite failures are the privacy-redaction assertions the
brief explicitly called out; no assertion was weakened and no sector-sentiment
code was modified:

- `tests/test_sector_sentiment_hardening.py::test_creator_metrics_use_all_unique_content_duplicate_is_time_stable_and_evidence_is_redacted`
- `tests/test_sector_sentiment_hardening.py::test_legacy_persisted_evidence_is_minimized_and_redacted_on_upgrade`

Both expose unredacted `@finance_guy` / `财经小王` handles in the existing
sector-sentiment persistence path.

## Files changed

- `apex/position_action_state.py`
- `apex/watchlist.py`
- `apex/analyze.py`
- `apex/schemas.py`
- `tests/test_position_action_state.py`
- `tests/test_position_action_finalize.py`
- `tests/test_analysis_graph.py`
- `.superpowers/sdd/2026-08-27-position-action-effective-state/final-fix-report.md`

## Self-review

- Verified finalization receives proposal and effective state separately and
  never reconstructs effective state after review/reporting.
- Verified all save-time writes route through the one compare-and-apply path;
  the journal and trace occur only after that operation succeeds.
- Verified the fingerprint excludes non-trading metadata while including the
  ruled trading fields and canonical full-exit ladder representation.
- Verified `preserve` cannot clear a ladder, `clear` removes only the ladder,
  explicit null preserves risk values, and exit reports do not demand current
  stop/target labels.
- Confirmed no privacy-redaction assertion, unrelated frontend code, or
  user-owned untracked file was changed.

## Remaining concerns

- Local full-suite socket fixtures require an unsandboxed runner; the dedicated
  Eastmoney suite passed there.
- The two pre-existing sector-sentiment privacy-redaction failures remain and
  should be handled as a separate, security-focused task rather than weakened
  here.
