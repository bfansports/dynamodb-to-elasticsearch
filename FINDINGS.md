# AI Audit: dynamodb-to-elasticsearch

**Date:** 2026-02-17  
**Auditor:** Backend Developer Agent (AI)  
**Repo:** bfansports/dynamodb-to-elasticsearch  
**Branch:** master  
**Scope:** Data integrity, error handling, mapping correctness, ES injection, OpenSearch migration status

---

## Critical

### C1 — Still Using `elasticsearch` Client Library (Not Migrated to OpenSearch)

**File:** `requirements.txt`, `src/DynamoToES/index.py`  
**Impact:** The entire codebase still uses `elasticsearch==7.17.9` and `from elasticsearch import Elasticsearch`. AWS OpenSearch Service has diverged from Elasticsearch since the fork. The `elasticsearch` Python client versions >=7.14 include license checks that may reject connections to OpenSearch clusters. This is a ticking time bomb.

**Evidence:** The companion `ImportToOpensearch` Lambda in `docker-environments/cloudformation/Lambdas/src/ImportToOpensearch/index.py` has already migrated to `opensearch-py` and `OpenSearch` client. The stream sync Lambda has not.

**Recommendation:**
1. Replace `elasticsearch==7.17.9` with `opensearch-py>=2.4.0` in `requirements.txt`
2. Change imports from `from elasticsearch import Elasticsearch, RequestsHttpConnection` to `from opensearchpy import OpenSearch, RequestsHttpConnection`
3. Replace `Elasticsearch()` constructor with `OpenSearch()`
4. Update `awsauth` service name from `'es'` to `'es'` (stays the same for AWS OpenSearch Service)
5. Remove deprecated `doc_type` parameter from all `es.index()` and `es.delete()` calls (see H1)

### C2 — Silent Data Loss on Exceptions

**File:** `src/DynamoToES/index.py`, lines 63-67  
**Impact:** When any record fails to process, the error is printed and the loop continues with `continue`. The Lambda returns successfully. Because the Lambda reports success, DynamoDB Streams considers the batch processed and advances the iterator. **Failed records are permanently lost** — they will never be retried.

```python
except Exception as e:
    print("Failed to process:")
    print(json.dumps(record))
    print("ERROR: " + repr(e))
    continue  # <-- swallows the error, batch advances
```

**Recommendation:** Re-raise the exception after logging, or collect failures and raise at the end. DynamoDB Streams provides at-least-once delivery — if the Lambda fails, the batch will be retried. The current pattern defeats this guarantee.

```python
# Option A: fail-fast (simplest, relies on DynamoDB Streams retry)
except Exception as e:
    print(f"Failed to process: {json.dumps(record)}")
    print(f"ERROR: {repr(e)}")
    raise  # Let Lambda fail; Streams will retry the batch

# Option B: partial batch failure (requires bisect-on-error config)
# Use Lambda's ReportBatchItemFailures feature
```

### C3 — `doc_type` Parameter Will Break on OpenSearch 2.x

**File:** `src/DynamoToES/index.py`, lines 85-89, 101-104, 129-133  
**Impact:** Every `es.index()` and `es.delete()` call passes `doc_type=table`. The `doc_type` parameter was deprecated in Elasticsearch 7.x and removed in OpenSearch 2.x. If the cluster is OpenSearch 2.x (which bFAN uses per the CloudFormation template `opensearch2.template`), these calls will fail with an unrecognized parameter error.

**Recommendation:** Remove `doc_type` from all calls:
```python
es.index(index=table, body=doc, id=docId, refresh=True)
es.delete(index=table, id=docId, refresh=True)
```

---

## High

### H1 — `refresh=True` on Every Operation Kills Cluster Performance

**File:** `src/DynamoToES/index.py`, lines 89, 104, 133  
**Impact:** Every INSERT, MODIFY, and REMOVE forces an index refresh. For high-throughput tables, this creates massive I/O pressure on the OpenSearch cluster. Each refresh flushes the in-memory buffer to a new Lucene segment, which then needs to be merged. Under load this causes:
- Increased latency for all search queries
- Higher CPU and disk usage
- Potential cluster instability

**Recommendation:** Remove `refresh=True`. OpenSearch's default refresh interval (1 second) is sufficient for near-realtime search. If specific use cases need it, make it configurable via environment variable.

### H2 — No Bulk Indexing — One HTTP Call Per Record

**File:** `src/DynamoToES/index.py`, lines 56-67  
**Impact:** Each DynamoDB Stream record triggers an individual HTTP request to OpenSearch. A batch of 100 records = 100 HTTP round trips. The `ImportToOpensearch` Lambda in the infra repo already uses bulk API (line 259: `opensearch_client.bulk(data)`). This Lambda should too.

**Recommendation:** Accumulate records and use `es.bulk()` for the entire batch. DynamoDB Streams batches records (default batch size: 100), so one bulk call per Lambda invocation.

### H3 — Race Condition in Index Auto-Creation

**File:** `src/DynamoToES/index.py`, lines 114-121  
**Impact:** On INSERT, the code checks `es.indices.exists(table)` and creates the index if missing. With multiple Lambda invocations processing the same table concurrently (one per shard), two invocations could both see `exists=False` and both try to create the index. The second `create` call will fail with `resource_already_exists_exception`.

**Recommendation:** Use `ignore=400` on the create call to silently ignore "already exists" errors:
```python
es.indices.create(table, body='{"settings": { "index.mapping.coerce": true } }', ignore=400)
```

### H4 — Credentials Refreshed on Every Invocation (Unnecessary)

**File:** `src/DynamoToES/index.py`, lines 31-47  
**Impact:** A new `boto3.session.Session()`, `AWS4Auth`, and `Elasticsearch` client are created on every Lambda invocation. This adds latency to every call. The session and client should be initialized outside the handler for connection reuse across warm invocations.

**Recommendation:** Move client initialization to module level:
```python
# Module level — reused across warm invocations
session = boto3.session.Session()
credentials = session.get_credentials()
awsauth = AWS4Auth(credentials.access_key, credentials.secret_key,
                   session.region_name, 'es',
                   session_token=credentials.token)
es = OpenSearch([env.ES_ENDPOINT], http_auth=awsauth, ...)

def lambda_handler(event, context):
    for record in event['Records']:
        ...
```

**Note:** AWS4Auth with `session_token` handles credential rotation for Lambda execution roles.

### H5 — `update_mapping.py` Regex Too Restrictive

**File:** `update_mapping.py`, line 32  
**Impact:** The regex `[a-zA-Z]+` for table name extraction misses tables with numbers, underscores, or hyphens. The main `index.py` correctly uses `[0-9a-zA-Z_-]+`. Any DynamoDB table with a number in its name (common in bFAN: table names like `GameStats2024`) would be silently skipped during mapping generation, causing key order issues.

**Recommendation:** Update regex to match `index.py`:
```python
re.search(".+:table/([0-9a-zA-Z_-]+)/.+", event_source_arn)
```

---

## Medium

### M1 — `BOOL` Type Returned As-Is (Not Converted to Python bool)

**File:** `src/DynamoToES/index.py`, line 181  
**Impact:** DynamoDB `BOOL` type values are passed through as-is alongside `S` (string) types. DynamoDB encodes booleans as `{"BOOL": true}` where the value is already a Python bool, so this works by accident. But the code treats `S` and `BOOL` identically (`return value`), which makes the intent unclear and could mask bugs if DynamoDB SDK behavior changes.

**Recommendation:** Handle `BOOL` explicitly for clarity:
```python
if key == "BOOL":
    return bool(value)
if key == "S":
    return str(value)
```

### M2 — Binary (`B`, `BS`) Types Not Properly Handled

**File:** `src/DynamoToES/index.py`, line 193  
**Impact:** `BS` (Binary Set) is handled alongside `L` (List) on line 193, but single `B` (Binary) type has no handler. Binary values will fall through `unmarshalValue` returning `None` implicitly, causing silent data loss. The README acknowledges "Binary NOT tested."

**Recommendation:** Either:
1. Add explicit `B` handler that base64-encodes the value for ES
2. Or raise a clear error so the issue is visible in logs

### M3 — `unmarshalValue` Returns `None` for Unknown Types

**File:** `src/DynamoToES/index.py`, lines 176-210  
**Impact:** If DynamoDB introduces a new type (or if a type is misidentified), `unmarshalValue` returns `None` implicitly (no `else` clause, no `return` at end of function). This causes silent data corruption — fields are set to `null` in OpenSearch instead of raising an error.

**Recommendation:** Add a catch-all:
```python
raise ValueError(f"Unknown DynamoDB type: {key}")
```

### M4 — Verbose Logging in Production (PII Risk)

**File:** `src/DynamoToES/index.py`, lines 49-50, 82-83, 125-126  
**Impact:** The Lambda prints the full ES cluster info, full document bodies, and all record data to CloudWatch Logs. For a fan engagement platform, DynamoDB records likely contain PII (names, emails, device IDs). This creates a compliance risk (GDPR, etc.) and inflates CloudWatch costs.

**Recommendation:** 
1. Remove `print(es.info())` — exposes cluster metadata
2. Remove `print(doc)` — exposes full document bodies
3. Log only document IDs and operation types at INFO level
4. Use structured logging (JSON format) for easier parsing

### M5 — Duplicate `import json` Statement

**File:** `src/DynamoToES/index.py`, lines 3 and 9  
**Impact:** Minor — `import json` appears twice. No functional impact but indicates code was modified without cleanup.

### M6 — `update_mapping.py` Stores Full `describe_table` Response

**File:** `update_mapping.py`, lines 35-38  
**Impact:** The mapping script stores the entire `describe_table` response in `table_mapping.json`, but `index.py` only reads `PrimaryKey` and `SortKey`. The JSON file contains unnecessary metadata (table size, ARN, creation date, provisioned throughput, etc.), making it larger than needed and potentially exposing infrastructure details if the file is leaked.

**Recommendation:** Store only the key schema:
```python
table_mapping = {}
for table_name, desc in raw_mapping.items():
    entry = {"PrimaryKey": primary_key}
    if sort_key:
        entry["SortKey"] = sort_key
    table_mapping[table_name] = entry
```

### M7 — No Timeout on OpenSearch Client

**File:** `src/DynamoToES/index.py`, lines 41-47  
**Impact:** The Elasticsearch client is created without a `timeout` parameter. Default is 10 seconds. If OpenSearch is under load, requests could hang for the full Lambda timeout (10 seconds per Makefile). The `ImportToOpensearch` Lambda correctly sets `timeout=120`.

**Recommendation:** Set an explicit timeout:
```python
es = OpenSearch([env.ES_ENDPOINT], ..., timeout=30)
```

---

## Low

### L1 — Typo: "Successly" in Log Messages

**File:** `src/DynamoToES/index.py`, lines 91, 106, 135  
**Impact:** Cosmetic. "Successly" should be "Successfully".

### L2 — Inconsistent ID Generation When Mapping Is Missing

**File:** `src/DynamoToES/index.py`, lines 158-167  
**Impact:** When no table mapping exists, the fallback iterates `keys.items()` and concatenates values with `|`. Python dict ordering is insertion-ordered (Python 3.7+), but the insertion order of keys from DynamoDB Streams is not guaranteed. This is the exact problem the mapping was created to solve. Without the mapping, IDs could be generated inconsistently (e.g., `hashval|rangeVal` vs `rangeVal|hashVal`), causing duplicate documents in OpenSearch.

**Recommendation:** Always generate the mapping. Make it a required deployment step, not optional.

### L3 — Hardcoded Function Name in `update_mapping.py`

**File:** `update_mapping.py`, line 18  
**Impact:** `FunctionName='DynamoToES'` is hardcoded. The README mentions this but does not provide a way to override it. If the function is deployed with a different name (e.g., per-environment naming), the script silently finds zero mappings.

**Recommendation:** Accept function name as CLI argument or environment variable.

### L4 — GitHub Backup Workflow Triggers on `develop` but Default Branch is `master`

**File:** `.github/workflows/github-backup.yml`, line 5  
**Impact:** The S3 backup workflow triggers on push to `develop`, but the repo's default branch is `master`. The backup workflow never runs unless someone pushes to a `develop` branch.

**Recommendation:** Change trigger to `master`:
```yaml
on:
  push:
    branches:
      - master
```

### L5 — Makefile Uses `AWSENV_NAME` but README Says `ENV`

**File:** `Makefile` line 33 vs `README.md` line 29  
**Impact:** Documentation mismatch. The Makefile checks `AWSENV_NAME` for the environment variable, but the README instructs users to set `ENV`. Users following the README will hit a confusing error.

### L6 — Reserved Fields Escape is One-Way

**File:** `src/DynamoToES/index.py`, lines 13, 189-190  
**Impact:** Fields like `_id`, `_type`, etc. are escaped by replacing the first `_` with `__` (e.g., `_id` becomes `__id`). But there is no corresponding unescape when reading from OpenSearch. If any application queries OpenSearch and expects the original field name, it will not find it. This is documented nowhere.

---

## Agent Skill Improvements

### S1 — CLAUDE.md Needs Stream Architecture Details
The existing CLAUDE.md is solid but lacks the stream data flow diagram and the relationship to `ImportToOpensearch` (the bulk backfill companion). Added in the improved CLAUDE.md delivered with this audit.

### S2 — Missing `<!-- Ask -->` Answers
The CLAUDE.md has four `<!-- Ask -->` placeholders. Based on code analysis:
- No unit tests exist (confirmed by absence of test files)
- Not migrated to OpenSearch client (confirmed: still uses `elasticsearch`)
- No performance benchmarks found in repo
- No batching implemented (confirmed: one HTTP call per record)

### S3 — Cross-Repo Context Missing
The `ImportToOpensearch` Lambda in `docker-environments` is the backfill companion to this stream sync. This relationship should be documented in both repos.

---

## Positive Observations

### P1 — DynamoDB JSON Unmarshaling is Correct
The `unmarshalJson` / `unmarshalValue` functions correctly handle the nested DynamoDB type system (S, N, M, L, SS, NS, NULL). The recursive approach is clean and handles nested maps and lists properly.

### P2 — Reserved Field Handling
The code correctly identifies Elasticsearch reserved fields (`_id`, `_type`, `_source`, etc.) and escapes them to prevent conflicts. This is a non-obvious requirement that many DynamoDB-to-ES implementations miss.

### P3 — Table Mapping Solves a Real Problem
The key ordering issue with DynamoDB Streams is a known, subtle bug. The mapping solution with `update_mapping.py` is a pragmatic fix that ensures deterministic document IDs.

### P4 — Makefile Build Pipeline is Well-Structured
The build pipeline (download config from S3, install deps, ZIP, upload, create/update Lambda) is clean and follows the standard bFAN Lambda deployment pattern (`aws-lambda-python-local`).

### P5 — IAM Auth via AWS4Auth
Using `AWS4Auth` with Lambda execution role credentials (instead of hardcoded keys) is the correct approach for OpenSearch access from Lambda.

### P6 — Coerce Setting on Index Creation
`index.mapping.coerce: true` on index creation prevents type mismatch errors when numeric strings arrive as numbers or vice versa. Good defensive setting.
