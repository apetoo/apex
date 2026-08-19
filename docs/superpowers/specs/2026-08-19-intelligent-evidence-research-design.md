# Intelligent Evidence Research Design

## Goal

Replace mandatory category searches with a stateful, quality-gated research loop. Structured market data remains primary; every analysis performs one narrow authoritative material-event scan, then the model chooses additional tools only when evidence gaps remain. A conclusion is delivered only after evidence convergence and an independent review; otherwise the run is persisted as `insufficient_evidence` without a verdict or position action.

## Architecture

LangGraph owns orchestration only. Existing data tools, trading guards, journal, trace, calibration, repeat-analysis limits, and position lifecycle remain business authorities.

The graph is:

`prepare -> safety_scan -> reason -> tools -> assess -> draft -> review`

Conditional routes from `assess` continue research, draft a conclusion, or abstain. Review may pass, request one targeted rework, or abstain. The legacy loop remains available behind configuration during migration.

The graph state contains messages, evidence, research gaps, authoritative scan status, budget counters, draft output, review outcome, and final status. The initial implementation uses process-local state; journal and trace remain the durable record.

## Evidence quality

Search queries include stock name, short code, and exchange-qualified code. Results are normalized before the model sees them: URL hostname parsing, entity match, category relevance, publication-date validation, duplicate removal, and source-tier assignment.

- Tier 1: exchanges, CNInfo, CSRC/government, and company disclosures. May directly support a conclusion.
- Tier 2: established financial media and structured Eastmoney content. Material facts require a second independent source or Tier 1 confirmation.
- Tier 3: self-media, forums, and stock communities. Leads only; cannot independently change a verdict or action.

Every accepted item is represented by `EvidenceItem`: `id`, `fact`, `inference`, `evidence_type`, `tool_name`, `source_name`, `source_url`, `published_at`, `source_tier`, `entity_matched`, and `freshness_status`.

Search errors, empty responses, and fully filtered responses do not count as coverage. A clean material-event scan is valid only after an authoritative query succeeds; transport failure leaves the status `unknown`.

## Budget and termination

Defaults are 300 seconds, 12 external tool calls including the safety scan, three evidence rounds, two consecutive no-progress rounds, and one review-requested rework. Evidence convergence ends early.

Critical unknowns include ambiguous entity identity, stale/unavailable core price data, an unverified suspected material event, a material claim supported only by Tier 3, and unverified grounds for `trim` or `exit`. A critical unknown blocks final submission.

## Results and compatibility

Successful records use `analysis_status=completed`. Abstentions use `analysis_status=insufficient_evidence`, retain evidence, unknowns, attempted tools, failures, and a research summary, while leaving verdict, confidence, price advice, and position action null. Abstentions never enter calibration, backtests, candidate promotion, notifications, or trading actions.

Legacy journal records with a verdict and no status are interpreted as completed. CLI and frontend show an amber “证据不足，暂不判断” state with unknowns and no action controls. Full orchestration remains in trace; hidden chain-of-thought is not persisted or displayed.

## Acceptance

Fixtures based on the observed `002050.SZ` failure must reject unrelated Tianzhun/Changyingtong and local-enforcement results, rank sources by URL hostname, and deduplicate reposts. Tests cover clean no-extra-search completion, gap-triggered research, budgets, no-progress termination, review pass/rework/abstain, position-action blocking, journal isolation, and legacy compatibility.
