# Sector Sentiment Policy Migration Design

## Goal

Allow an operator to deliberately start a new sector-sentiment retrieval cohort before the existing 60-trading-date freeze completes, without deleting or rewriting historical runs.

## Configuration

`sector_sentiment.policy_override_reason` is optional. An omitted or empty-string value has no effect. A non-empty string of at most 500 characters authorizes one audited transition from the latest cohort's policy hash to the currently configured policy hash. Whitespace-only and non-string values are invalid.

The reason is operational audit data. It must not be included in the retrieval policy hash, and changing only the wording must not create another policy.

## Rules

- The normal 60-distinct-trading-date freeze remains the default.
- An override may start a new cohort only on a date later than every previously recorded or reserved policy date.
- A date already reserved by another policy remains immutable, including after a crash.
- An override is invalid when the requested policy hash equals the current policy hash.
- A normalized reason may authorize only one migration and cannot be reused for another policy change.
- The transition records the effective trade date, prior cohort and hash, new cohort and hash, reason, and timestamp in the sentiment SQLite database before collection begins.
- Re-running the same date with the same policy is idempotent and reuses its reservation; it does not create duplicate migration audit rows.
- Historical collection runs, scores, shadow progress, stale reports, and prior cohorts are never deleted or rewritten.
- API projections must not expose the free-form override reason unless a later privacy-reviewed requirement explicitly permits it.

## Configuration Example

```yaml
sector_sentiment:
  mediacrawler_commit: "0358d53a4387eb283099a7937b9ff3fd32eb5135"
  policy_override_reason: "升级 MediaCrawler 并降低 B站/抖音采集配额"
  retrieval:
    max_queries_per_sector: 3
    max_contents_per_query: 8
    max_comments_per_content: 20
```

After the first successful reservation under the new policy, the override reason may be removed. The new cohort remains frozen under the normal rules.

## Verification

Tests must prove the default freeze still rejects a premature change, an audited override creates a new cohort without altering old rows, historical/same-day overrides remain rejected, retries are idempotent, and the reason is persisted but not included in the policy hash or public API output.
