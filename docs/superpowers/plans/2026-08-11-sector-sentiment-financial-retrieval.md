# Sector Sentiment Financial Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ambiguous bare sector searches with quota-controlled financial queries and an audited finance-creator pool, while exposing retrieval quality and moderation in the existing shadow-alert UI.

**Architecture:** Keep MediaCrawler outside Apex and deepen the existing adapter boundary: Apex creates typed search/creator jobs and consumes canonical JSONL. Split retrieval policy, financial relevance, and creator-pool persistence into focused modules; `run_configured()` orchestrates them and passes only accepted, deduplicated records into the existing semantic scoring path.

**Tech Stack:** Python 3, SQLite, FastAPI/Pydantic, MediaCrawler subprocess adapter, React/TypeScript, TanStack Query, Vitest, pytest.

## Global Constraints

- Personal research and shadow mode only; no selection, position, or automatic-trading side effects.
- MediaCrawler remains outside this repository at the configured pinned commit; credentials and login state never enter Apex storage or logs.
- Search queries always contain a financial anchor; generic entities such as `机器人` are never sent as bare queries.
- `candidate` creators require manual approval; no score, follower count, or LLM result can automatically set `approved`.
- Creator-pool collection improves recall only and never increases sentiment weight.
- Filtered records remain auditable but never enter sentiment, author-count, or alert aggregation.
- Dictionary, query-template, relevance-prompt, and creator-rule versions remain frozen during the 60-trading-day validation window.
- Existing immutable raw files, `(platform, content_id, comment_id)` idempotency, cross-platform repost weighting, and partial-platform failure behavior remain intact.

---

## File Structure

- Create `apex/sector_retrieval.py`: versioned query generation, quotas, financial-relevance decisions, and candidate thresholds.
- Modify `apex/sector_sentiment.py`: schema migration, creator/audit/relevance persistence, typed MediaCrawler jobs, and daily orchestration.
- Modify `backend/routers/sector_sentiment.py`: creator list and moderation endpoints plus funnel metadata.
- Modify `frontend/src/api/sector-sentiment.ts`: creator, moderation, and funnel contracts.
- Modify `frontend/src/routes/sector-sentiment/SectorSentimentPage.tsx`: alert/creator tabs, moderation actions, and retrieval funnel.
- Modify `config.example.yaml`: exact retrieval templates, quotas, relevance dictionary, engagement thresholds, and frozen versions.
- Modify `tests/test_sector_sentiment.py`, `tests/test_sector_sentiment_api.py`, and `frontend/src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx`: domain, adapter, API, and UI acceptance coverage.

### Task 1: Versioned Financial Query Planning

**Files:**
- Create: `apex/sector_retrieval.py`
- Modify: `config.example.yaml:114-140`
- Test: `tests/test_sector_sentiment.py`

**Interfaces:**
- Consumes: taxonomy entries shaped as `{sector_id, sector_name, taxonomy, aliases}` and retrieval settings.
- Produces: `RetrievalJob(platform: str, mode: Literal["search", "creator"], value: str, sector_ids: tuple[str, ...], source_id: str)` and `build_search_jobs(taxonomy: list[dict], platforms: list[str], settings: dict) -> list[RetrievalJob]`.

- [ ] **Step 1: Write failing query-planning tests**

```python
def test_financial_search_jobs_never_emit_bare_sector_terms():
    jobs = retrieval.build_search_jobs(
        [{"sector_id": "concept:robot", "sector_name": "机器人", "taxonomy": "concept",
          "aliases": ["机器人", "具身智能"]}],
        ["bili", "dy"],
        {"query_templates": ["{term} 股票", "{term} ETF"], "max_queries_per_sector": 3},
    )
    assert len(jobs) == 6
    assert {job.value for job in jobs} <= {"机器人 股票", "机器人 ETF", "具身智能 股票"}
    assert all(job.value not in {"机器人", "具身智能"} for job in jobs)
    assert all(job.mode == "search" for job in jobs)

def test_query_planning_is_stable_and_deduplicated():
    taxonomy = [{"sector_id": "concept:robot", "sector_name": "机器人",
                 "taxonomy": "concept", "aliases": ["机器人", "机器人"]}]
    first = retrieval.build_search_jobs(taxonomy, ["bili"], {
        "query_templates": ["{term} 股票", "{term} 股票"], "max_queries_per_sector": 8})
    second = retrieval.build_search_jobs(taxonomy, ["bili"], {
        "query_templates": ["{term} 股票", "{term} 股票"], "max_queries_per_sector": 8})
    assert first == second
    assert [job.value for job in first] == ["机器人 股票"]
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_sector_sentiment.py -k 'financial_search_jobs or query_planning' -v`

Expected: FAIL because `apex.sector_retrieval` and `build_search_jobs` do not exist.

- [ ] **Step 3: Implement immutable retrieval jobs and deterministic quotas**

```python
from dataclasses import dataclass
from typing import Literal

QUERY_VERSION = "sector-finance-query-v1"
DEFAULT_QUERY_TEMPLATES = (
    "{term} 股票", "{term} A股", "{term} 板块", "{term} 概念股",
    "{term} ETF", "{term} 龙头", "{term} 投资", "{term} 行情",
)

@dataclass(frozen=True)
class RetrievalJob:
    platform: str
    mode: Literal["search", "creator"]
    value: str
    sector_ids: tuple[str, ...]
    source_id: str

def build_search_jobs(taxonomy: list[dict], platforms: list[str], settings: dict) -> list[RetrievalJob]:
    templates = tuple(settings.get("query_templates") or DEFAULT_QUERY_TEMPLATES)
    limit = int(settings.get("max_queries_per_sector", 8))
    jobs: list[RetrievalJob] = []
    seen: set[tuple[str, str]] = set()
    for sector in taxonomy:
        terms = dict.fromkeys([sector["sector_name"], *sector.get("aliases", [])])
        queries = list(dict.fromkeys(template.format(term=term).strip()
                                     for term in terms for template in templates))[:limit]
        for platform in platforms:
            for query in queries:
                key = (platform, query)
                if key not in seen:
                    seen.add(key)
                    jobs.append(RetrievalJob(platform, "search", query,
                                             (sector["sector_id"],), f"query:{query}"))
    return jobs
```

- [ ] **Step 4: Add explicit frozen retrieval configuration**

```yaml
  retrieval:
    query_version: "sector-finance-query-v1"
    query_templates: ["{term} 股票", "{term} A股", "{term} 板块", "{term} 概念股", "{term} ETF", "{term} 龙头", "{term} 投资", "{term} 行情"]
    max_queries_per_sector: 8
    max_contents_per_query: 20
    max_comments_per_content: 50
```

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/test_sector_sentiment.py -k 'financial_search_jobs or query_planning' -v`

Expected: PASS.

```bash
git add apex/sector_retrieval.py config.example.yaml tests/test_sector_sentiment.py
git commit -m "feat: plan finance-anchored sector searches"
```

### Task 2: Auditable Financial-Relevance Gate

**Files:**
- Modify: `apex/sector_retrieval.py`
- Modify: `apex/sector_sentiment.py:35-170`
- Test: `tests/test_sector_sentiment.py`

**Interfaces:**
- Consumes: canonical records with `title`, `description`, `tags`, `text`, `author_id`, and optional approved-author status.
- Produces: `RelevanceDecision(score: float, decision: Literal["accepted", "review", "filtered_non_financial"], reasons: tuple[str, ...], version: str)` and persisted relevance fields on `content`.

- [ ] **Step 1: Write failing rule and persistence tests**

```python
@pytest.mark.parametrize("text", ["机器人产品测评", "机械臂安装教程", "机器人编程比赛"])
def test_physical_robot_content_is_filtered(text):
    decision = retrieval.classify_financial_relevance({"title": text, "text": text}, ["机器人"], False)
    assert decision.decision == "filtered_non_financial"
    assert decision.reasons

def test_financial_robot_content_is_accepted():
    decision = retrieval.classify_financial_relevance(
        {"title": "机器人板块资金流入", "text": "继续看多，准备加仓"}, ["机器人"], False)
    assert decision.decision == "accepted"
    assert decision.score >= 0.7

def test_filtered_record_is_retained_but_not_eligible(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.ingest([_item("bili", "physical", "机械臂安装教程")])
    store.save_relevance("bili", "physical", "", retrieval.RelevanceDecision(
        0.05, "filtered_non_financial", ("exclude:教程",), retrieval.RELEVANCE_VERSION))
    row = store.list_content("2026-08-10")[0]
    assert row["relevance_decision"] == "filtered_non_financial"
    assert store.list_eligible_content("2026-08-10") == []
```

- [ ] **Step 2: Verify the tests fail**

Run: `pytest tests/test_sector_sentiment.py -k 'physical_robot or financial_robot or filtered_record' -v`

Expected: FAIL on missing relevance classifier/schema methods.

- [ ] **Step 3: Implement weighted, explainable relevance classification**

```python
@dataclass(frozen=True)
class RelevanceDecision:
    score: float
    decision: Literal["accepted", "review", "filtered_non_financial"]
    reasons: tuple[str, ...]
    version: str

RELEVANCE_VERSION = "sector-finance-relevance-v1"
FINANCE_TERMS = ("股票", "A股", "板块", "概念股", "ETF", "行情", "主力", "资金", "涨停", "估值", "持仓", "加仓", "减仓")
EXCLUDE_TERMS = ("教程", "编程", "机械臂安装", "产品测评", "比赛", "玩具")

def classify_financial_relevance(record: dict, sector_terms: list[str], approved_author: bool) -> RelevanceDecision:
    headline = " ".join(str(record.get(key) or "") for key in ("title", "description", "tags"))
    body = str(record.get("text") or "")
    entity = any(term in headline or term in body for term in sector_terms)
    finance_head = [term for term in FINANCE_TERMS if term in headline]
    finance_body = [term for term in FINANCE_TERMS if term in body]
    excludes = [term for term in EXCLUDE_TERMS if term in headline or term in body]
    score = min(1.0, 0.35 * entity + 0.35 * bool(finance_head) +
                0.20 * bool(finance_body) + 0.15 * (approved_author and bool(finance_body)) -
                0.35 * bool(excludes))
    reasons = tuple([*(f"finance:{x}" for x in finance_head + finance_body),
                     *(f"exclude:{x}" for x in excludes)])
    decision = "accepted" if score >= 0.7 else "review" if score >= 0.4 else "filtered_non_financial"
    return RelevanceDecision(max(0.0, score), decision, reasons, RELEVANCE_VERSION)
```

- [ ] **Step 4: Migrate storage and expose only eligible rows to scoring**

Add columns with safe defaults and matching `ALTER TABLE` guards:

```sql
ALTER TABLE content ADD COLUMN relevance_score REAL;
ALTER TABLE content ADD COLUMN relevance_decision TEXT NOT NULL DEFAULT 'unclassified';
ALTER TABLE content ADD COLUMN relevance_reasons_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE content ADD COLUMN relevance_version TEXT;
ALTER TABLE content ADD COLUMN retrieval_source TEXT NOT NULL DEFAULT 'search';
ALTER TABLE content ADD COLUMN author_id TEXT;
ALTER TABLE content ADD COLUMN author_name TEXT;
```

Implement `save_relevance(...)` as an exact-key update and `list_eligible_content(trade_date)` with `WHERE relevance_decision='accepted'`. Preserve `list_content()` as the audit view.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/test_sector_sentiment.py -k 'relevance or filtered_record or ingest' -v`

Expected: PASS, including existing idempotency tests.

```bash
git add apex/sector_retrieval.py apex/sector_sentiment.py tests/test_sector_sentiment.py
git commit -m "feat: gate sentiment input by financial relevance"
```

### Task 3: Creator Pool, Candidate Discovery, and Audit Events

**Files:**
- Modify: `apex/sector_retrieval.py`
- Modify: `apex/sector_sentiment.py:35-260`
- Test: `tests/test_sector_sentiment.py`

**Interfaces:**
- Consumes: accepted content keyed by `(platform, author_id, content_id)` and platform engagement thresholds.
- Produces: `list_creators(status: str | None) -> list[dict]`, `moderate_creator(platform: str, creator_id: str, action: str, actor: str = "local-user") -> dict`, `discover_creator_candidates(records: list[dict], settings: dict) -> dict[str, int]`, and `approved_creators(platform: str | None = None) -> list[dict]`.

- [ ] **Step 1: Write failing candidate/idempotency/moderation tests**

```python
def test_creator_becomes_candidate_once_after_three_unique_financial_contents(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    records = [{**_item("bili", f"v{i}", "机器人板块资金流入"),
                "author_id": "up-1", "author_name": "财经小王",
                "relevance_score": 0.9, "sector_ids": ["concept:robot"]} for i in range(3)]
    assert store.discover_creator_candidates(records, {"candidate_min_contents": 3}) == {"created": 1, "updated": 0}
    assert store.discover_creator_candidates(records, {"candidate_min_contents": 3}) == {"created": 0, "updated": 1}
    creators = store.list_creators("candidate")
    assert len(creators) == 1
    assert creators[0]["valid_content_count"] == 3

def test_candidate_requires_manual_approval_and_writes_audit_event(tmp_path):
    store = seeded_candidate_store(tmp_path)
    assert store.approved_creators() == []
    creator = store.moderate_creator("bili", "up-1", "approve")
    assert creator["status"] == "approved"
    assert store.creator_events("bili", "up-1")[0]["action"] == "approve"
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_sector_sentiment.py -k 'creator_becomes or manual_approval' -v`

Expected: FAIL because creator tables and store methods are absent.

- [ ] **Step 3: Add creator and immutable moderation-event tables**

```sql
CREATE TABLE IF NOT EXISTS creators (
  platform TEXT NOT NULL, creator_id TEXT NOT NULL, display_name TEXT,
  status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected')),
  first_discovered_at TEXT NOT NULL, last_discovered_at TEXT NOT NULL,
  valid_content_count INTEGER NOT NULL DEFAULT 0,
  total_content_count INTEGER NOT NULL DEFAULT 0,
  financial_ratio REAL NOT NULL DEFAULT 0,
  sector_ids_json TEXT NOT NULL DEFAULT '[]', evidence_json TEXT NOT NULL DEFAULT '[]',
  reviewed_at TEXT, last_collection_error TEXT,
  PRIMARY KEY(platform, creator_id)
);
CREATE TABLE IF NOT EXISTS creator_content (
  platform TEXT NOT NULL, creator_id TEXT NOT NULL, content_id TEXT NOT NULL,
  relevance_score REAL NOT NULL, sector_ids_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(platform, creator_id, content_id)
);
CREATE TABLE IF NOT EXISTS creator_events (
  event_id TEXT PRIMARY KEY, platform TEXT NOT NULL, creator_id TEXT NOT NULL,
  action TEXT NOT NULL, previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
  actor TEXT NOT NULL, created_at TEXT NOT NULL
);
```

- [ ] **Step 4: Implement threshold evaluation without automatic approval**

Candidate creation is allowed when any condition is true: three unique accepted contents; one record at or above `very_high_relevance` and the per-platform `high_engagement_threshold`; or accepted hits across two sector IDs. Upsert content evidence first, recompute counts from `creator_content`, and never change an existing `approved` or `rejected` status during discovery.

Moderation transitions are exact: `approve -> approved`, `reject -> rejected`, `restore -> candidate`; reject unknown actions and missing creators with a domain `ValueError`/`KeyError`. Insert one audit event in the same SQLite transaction as the status update.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/test_sector_sentiment.py -k 'creator' -v`

Expected: PASS for duplicate discovery, manual approval, rejection, restoration, and immutable audit tests.

```bash
git add apex/sector_retrieval.py apex/sector_sentiment.py tests/test_sector_sentiment.py
git commit -m "feat: add audited finance creator pool"
```

### Task 4: Search and Approved-Creator Collection Pipeline

**Files:**
- Modify: `apex/sector_sentiment.py:550-760`
- Modify: `config.example.yaml:114-155`
- Test: `tests/test_sector_sentiment.py`

**Interfaces:**
- Consumes: `RetrievalJob`, `approved_creators()`, canonical JSONL, and `runner(job, destination, timeout, limits)`.
- Produces: collection report with separate `search_coverage`, `creator_coverage`, per-job status, and a daily funnel; only accepted rows reach `_semantic_evidence`/LLM classification.

- [ ] **Step 1: Write failing adapter and partial-failure pipeline tests**

```python
def test_mediacrawler_runner_uses_search_and_creator_modes(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(ss.subprocess, "run", fake_mediacrawler(commands, tmp_path))
    runner = ss.default_mediacrawler_runner(tmp_path / "MediaCrawler")
    runner(retrieval.RetrievalJob("bili", "search", "机器人 股票", ("concept:robot",), "q1"),
           tmp_path / "search.jsonl", 10, {"max_contents": 20, "max_comments": 50})
    runner(retrieval.RetrievalJob("bili", "creator", "up-1", (), "creator:up-1"),
           tmp_path / "creator.jsonl", 10, {"max_contents": 20, "max_comments": 50})
    assert "--type search" in " ".join(commands[0])
    assert "--keywords 机器人 股票" in " ".join(commands[0])
    assert "--type creator" in " ".join(commands[1])

def test_creator_failure_does_not_discard_successful_search_results(tmp_path):
    result = ss.run_configured(financial_config(tmp_path), runner=runner_failing_only_creator,
                               trade_date="2026-08-10")
    assert result["collection"]["search_coverage"] == 1.0
    assert result["collection"]["creator_coverage"] < 1.0
    assert result["funnel"]["financial_relevant"] > 0
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_sector_sentiment.py -k 'search_and_creator_modes or creator_failure' -v`

Expected: FAIL because the runner accepts platform/keywords only and the report has one coverage value.

- [ ] **Step 3: Change the MediaCrawler boundary to one typed job per invocation**

For search jobs emit `--platform <platform> --type search --keywords <value>`. For creator jobs use the pinned MediaCrawler CLI's creator identifier argument configured as `creator_id_argument` (default `--creator_id`) and emit `--type creator <argument> <value>`. Keep commit verification, immutable UUID destinations, timeout, and normalization. Stamp every normalized row with `retrieval_source`, `retrieval_source_id`, public `author_id`, and public `author_name` when available.

- [ ] **Step 4: Refactor `run_configured()` into the approved order**

```python
search_jobs = build_search_jobs(taxonomy, platforms, retrieval_settings)
search_report = collect_jobs(search_jobs, runner, raw_dir, trade_date, limits)
ingest_and_classify_relevance(search_report, store, taxonomy, relevance_settings)
store.discover_creator_candidates(store.accepted_search_records(trade_date), creator_settings)
creator_jobs = build_creator_jobs(store.approved_creators(), platforms)
creator_report = collect_jobs(creator_jobs, runner, raw_dir, trade_date, limits)
ingest_and_classify_relevance(creator_report, store, taxonomy, relevance_settings)
eligible_records = store.list_eligible_content(trade_date)
```

Use `eligible_records`—not raw `all_records`—for sector mapping, LLM semantics, author counts, scoring, and alert state. Catch failures per job, retain successful jobs, and aggregate search/creator coverage independently.

- [ ] **Step 5: Add exact quota and creator configuration**

```yaml
    creator_id_argument: "--creator_id"
    candidate_min_contents: 3
    candidate_min_sectors: 2
    very_high_relevance: 0.9
    high_engagement_thresholds: {bili: 1000, dy: 1000}
    relevance_version: "sector-finance-relevance-v1"
    creator_rule_version: "sector-finance-creator-v1"
```

- [ ] **Step 6: Run regression tests and commit**

Run: `pytest tests/test_sector_sentiment.py -v`

Expected: PASS, including previous collector degradation, idempotency, scoring, state, and validation cases.

```bash
git add apex/sector_sentiment.py config.example.yaml tests/test_sector_sentiment.py
git commit -m "feat: collect finance searches and approved creators"
```

### Task 5: Creator Moderation API and Retrieval Funnel Contract

**Files:**
- Modify: `backend/routers/sector_sentiment.py`
- Modify: `tests/test_sector_sentiment_api.py`
- Modify: `frontend/src/api/sector-sentiment.ts`

**Interfaces:**
- Consumes: store creator/relevance methods from Tasks 2–4.
- Produces: `GET /api/sector-sentiment/creators?status=`, three moderation POST endpoints, and `retrieval_funnel` in overview/detail metadata.

- [ ] **Step 1: Write failing API tests**

```python
def test_creator_endpoints_list_and_moderate(tmp_path):
    store = seeded_candidate_store(tmp_path)
    client = _client(store)
    listed = client.get("/api/sector-sentiment/creators?status=candidate").json()
    assert listed["creators"][0]["creator_id"] == "up-1"
    approved = client.post("/api/sector-sentiment/creators/bili/up-1/approve").json()
    assert approved["creator"]["status"] == "approved"
    assert client.get("/api/sector-sentiment/creators?status=approved").json()["creators"]

def test_detail_exposes_retrieval_funnel(tmp_path):
    store = seeded_score_and_funnel_store(tmp_path)
    payload = _client(store).get("/api/sector-sentiment/concept:robot").json()
    assert payload["retrieval_funnel"] == {
        "raw_recalled": 10, "financial_relevant": 4, "filtered": 6,
        "search_sources": 3, "creator_sources": 1,
    }
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_sector_sentiment_api.py -k 'creator or funnel' -v`

Expected: FAIL with 404 or missing payload fields.

- [ ] **Step 3: Add validated endpoints and error mappings**

```python
@router.get("/creators")
def creators(status: Literal["candidate", "approved", "rejected"] | None = Query(None)):
    store = _store()
    return {**_quality_meta(store, store.latest_date()), "creators": store.list_creators(status)}

def _moderate(platform: str, creator_id: str, action: str):
    try:
        return {**_quality_meta(_store(), _store().latest_date()),
                "creator": _store().moderate_creator(platform, creator_id, action)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="creator not found") from exc
```

Register `/creators` routes before `/{sector_id}` so `creators` is never captured as a sector ID. Add `approve`, `reject`, and `restore` POST handlers. Return 422 for invalid status filters through the `Literal` annotation.

- [ ] **Step 4: Add TypeScript contracts and client calls**

```typescript
export type CreatorStatus = "candidate" | "approved" | "rejected";
export interface FinanceCreator {
  platform: string; creator_id: string; display_name: string | null;
  status: CreatorStatus; financial_ratio: number; valid_content_count: number;
  sector_ids: string[]; last_discovered_at: string; evidence: Array<{ text: string }>;
  last_collection_error: string | null;
}
export interface RetrievalFunnel {
  raw_recalled: number; financial_relevant: number; filtered: number;
  search_sources: number; creator_sources: number;
}
export const getSectorSentimentCreators = (status?: CreatorStatus) =>
  api.get<{ creators: FinanceCreator[] }>(`/sector-sentiment/creators${status ? `?status=${status}` : ""}`);
export const moderateSectorSentimentCreator = (platform: string, id: string, action: "approve" | "reject" | "restore") =>
  api.post(`/sector-sentiment/creators/${encodeURIComponent(platform)}/${encodeURIComponent(id)}/${action}`);
```

- [ ] **Step 5: Run API tests and frontend type-check, then commit**

Run: `pytest tests/test_sector_sentiment_api.py -v`

Run: `cd frontend && npm run build`

Expected: both PASS.

```bash
git add backend/routers/sector_sentiment.py tests/test_sector_sentiment_api.py frontend/src/api/sector-sentiment.ts
git commit -m "feat: expose sentiment creator moderation api"
```

### Task 6: Author Pool UI and Sector Retrieval Funnel

**Files:**
- Modify: `frontend/src/routes/sector-sentiment/SectorSentimentPage.tsx`
- Modify: `frontend/src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx`

**Interfaces:**
- Consumes: `getSectorSentimentCreators`, `moderateSectorSentimentCreator`, and `RetrievalFunnel` from Task 5.
- Produces: accessible alert/creator tabs, status tabs, moderation controls, empty/error states, and a funnel in expanded sector detail.

- [ ] **Step 1: Write failing interaction tests**

```typescript
test("approves a candidate and refreshes the creator pool", async () => {
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: "作者池" }));
  expect(await screen.findByText("财经小王")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "批准财经小王" }));
  await waitFor(() => expect(moderateSectorSentimentCreator).toHaveBeenCalledWith("bili", "up-1", "approve"));
});

test("shows retrieval funnel in sector detail", async () => {
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "查看机器人证据" }));
  expect(await screen.findByText("原始召回 10")).toBeInTheDocument();
  expect(screen.getByText("金融相关 4")).toBeInTheDocument();
  expect(screen.getByText("已过滤 6")).toBeInTheDocument();
});
```

Also test: empty candidate list; rejected creator restoration; mutation error message; and `last_collection_error` rendering for an invalid creator ID.

- [ ] **Step 2: Verify UI tests fail**

Run: `cd frontend && npm test -- --run src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx`

Expected: FAIL because the author-pool tab and funnel do not exist.

- [ ] **Step 3: Implement page-level tabs and creator status filters**

Add `view: "alerts" | "creators"` and `creatorStatus` state. Load creators only when the creator view is active. Render platform, public display name, finance ratio, accepted-content count, sector IDs, last discovery, minimal evidence, and collection error. Do not render author IDs as the primary label or expose any non-public author field.

Use `useMutation` for moderation. On success invalidate `['sector-sentiment','creators']`; on failure keep the row and show an inline `role="alert"`. Controls are:

- candidate: `批准`, `拒绝`
- approved: `拒绝`
- rejected: `恢复为候选`

- [ ] **Step 4: Render the retrieval funnel in expanded details**

Show five exact values: raw recalled, financial relevant, filtered, search sources, and creator sources. If no funnel exists for a historical row, render `暂无检索漏斗数据` rather than zeroes, so missing telemetry is not mistaken for a clean retrieval day.

- [ ] **Step 5: Run UI tests, build, and commit**

Run: `cd frontend && npm test -- --run src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx`

Run: `cd frontend && npm run build`

Expected: PASS.

```bash
git add frontend/src/routes/sector-sentiment/SectorSentimentPage.tsx frontend/src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx
git commit -m "feat: add sentiment finance author pool ui"
```

### Task 7: Full Replay, Failure, and Regression Verification

**Files:**
- Modify: `tests/test_sector_sentiment.py`
- Modify: `tests/test_sector_sentiment_api.py`
- Modify: `frontend/src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx`

**Interfaces:**
- Consumes: all production interfaces from Tasks 1–6.
- Produces: one end-to-end acceptance test proving retrieval pollution cannot affect alert scoring and a repeatable verification record.

- [ ] **Step 1: Write the end-to-end replay test**

```python
def test_financial_retrieval_replay_excludes_pollution_and_is_idempotent(tmp_path):
    cfg = financial_config(tmp_path)
    first = ss.run_configured(cfg, runner=mixed_robot_runner, trade_date="2026-08-10")
    second = ss.run_configured(cfg, runner=mixed_robot_runner, trade_date="2026-08-10")
    store = ss.open_store(tmp_path)
    raw = store.list_content("2026-08-10")
    eligible = store.list_eligible_content("2026-08-10")
    assert any("机械臂安装教程" in row["text"] for row in raw)
    assert all("机械臂安装教程" not in row["text"] for row in eligible)
    assert first["funnel"]["filtered"] > 0
    assert second["ingest"]["inserted"] == 0
    assert len(store.scores_for_date("2026-08-10")) == 1
    assert len(store.list_events()) == len({event["event_key"] for event in store.list_events()})
```

- [ ] **Step 2: Verify the acceptance test fails before final wiring fixes**

Run: `pytest tests/test_sector_sentiment.py::test_financial_retrieval_replay_excludes_pollution_and_is_idempotent -v`

Expected: FAIL if any raw record bypasses relevance, repeated sources double-count creator evidence, or repeated runs duplicate scores/events.

- [ ] **Step 3: Make only the integration corrections exposed by the test**

Keep corrections within the defined boundaries: canonical normalization, exact-key idempotency, relevance eligibility query, creator-content uniqueness, funnel persistence, or `run_configured()` ordering. Do not change alert thresholds or give creator-sourced records extra weight.

- [ ] **Step 4: Run all backend and frontend verification**

Run: `pytest -q`

Run: `cd frontend && npm test -- --run`

Run: `cd frontend && npm run build`

Run: `cd frontend && npm run lint`

Expected: all tests and builds PASS; lint reports no new errors. Existing unrelated warnings may remain only if they were present before this feature branch.

- [ ] **Step 5: Confirm frozen versions and secret hygiene**

Run: `git diff --check`

Run: `rg -n "cookie|token|password|login_state" apex backend frontend config.example.yaml`

Expected: `git diff --check` has no output; the secret scan contains no newly persisted MediaCrawler credential/login-state fields. Confirm API payloads expose only public display data and minimal evidence spans.

- [ ] **Step 6: Commit the acceptance suite**

```bash
git add tests/test_sector_sentiment.py tests/test_sector_sentiment_api.py frontend/src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx
git commit -m "test: verify financial sentiment retrieval workflow"
```

## Self-Review Record

- Spec coverage: financial anchors and quotas are Task 1; relevance and audit retention Task 2; candidate rules/manual moderation Task 3; search/creator collection and partial failure Task 4; APIs/funnel Task 5; creator UI/error states Task 6; idempotent replay and full regression Task 7.
- Boundary coverage: creator pool changes recall only; Task 4 explicitly feeds accepted records into the existing equal-platform/repost-weight scoring without boost.
- Version coverage: query, relevance, and creator-rule versions are concrete configuration values and stored alongside decisions/runs; existing prompt/dictionary/mapping versions remain unchanged and frozen.
- Type consistency: all jobs use `RetrievalJob`; creator statuses are exactly `candidate | approved | rejected`; moderation actions are exactly `approve | reject | restore`; funnel fields are identical across SQLite/API/TypeScript/UI.
- Placeholder scan: every production change names concrete files, interfaces, behavior, commands, and expected results; no deferred implementation item remains.
