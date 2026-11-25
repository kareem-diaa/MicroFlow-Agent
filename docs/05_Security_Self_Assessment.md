---
title: "MicroFlow AI — Security Self-Assessment Report"
subtitle: "2026 Shenzhen International FinTech Competition · International Track · Topic E: SME Finance Copilot"
---

# 1. Scope and method

**System assessed:** MicroFlow AI prototype (FastAPI backend, Streamlit dashboard, SQLite/PostgreSQL store, sandbox financing provider), commit submitted with this report.

**Method:** threat modelling with STRIDE (Section 5b) over the data flow (upload → parse → encrypt → store → decrypt → AI → agent → approval → provider), code review of every endpoint, and automated tests. Evidence: `tests/` (22 tests) and `evidence/security_tests.json` (12/12 negative tests passed against the live API).

**Assets:** SME bank transactions and balances, invoice amounts, buyer names and tax ids, business registration ids, credit dossiers, financing applications, audit trail integrity, user credentials.

# 2. Permission-tier implementation

## 2.1 Roles and permissions

Permissions are defined once in `backend/security.py` (`ROLE_PERMISSIONS`) and enforced per endpoint with a FastAPI dependency `require("<permission>")`.

| Permission | SME owner | Advisor | Bank auditor |
|---|:-:|:-:|:-:|
| `data:upload` — upload statements / invoices | ✔ | | |
| `data:read` — view decrypted invoices | ✔ | ✔ | |
| `credit:score` — run scoring | ✔ | ✔ | |
| `credit:read` — view credit dossier | ✔ | ✔ | ✔ (amounts banded) |
| `forecast:run` — cash-flow forecast | ✔ | ✔ | |
| `agent:run` / `agent:ask` — run agent, ask copilot | ✔ | ✔ | |
| `financing:read` — view offers and applications | ✔ | ✔ | ✔ (amounts banded) |
| `financing:approve` — approve or reject an application | ✔ | | |
| `audit:read_own` — audit entries for own businesses | ✔ | ✔ | |
| `audit:read_all` / `audit:verify` — full log, chain verification | | | ✔ |

Design choices:

- **Separation of duties.** The agent can only *draft*. Only the business owner can approve, so neither the AI nor an advisor can commit the business to a financing contract. The auditor can read and verify but cannot change data or trigger AI actions.
- **Least privilege and data minimisation.** Auditors receive amount bands (`<10k`, `10k–100k`, `100k–1M`, `>1M`) instead of exact figures.

## 2.2 Tenant isolation

Every business-bound endpoint calls `resolve_business(user, business_id)`:

- owners are bound to their own `business_id`;
- advisors and auditors are bound to an explicit `assigned_business_ids` list;
- any other business id returns **403**. Audit-log reads are filtered to the caller's businesses unless they hold `audit:read_all`.

Financing approval re-checks that the application's business is in the caller's scope; otherwise it returns 404 so the existence of other tenants' records is not revealed.

## 2.3 Authentication

- Passwords: PBKDF2-HMAC-SHA256, 600,000 iterations (OWASP Password Storage Cheat Sheet recommendation), 16-byte random salt per user; constant-time comparison.
- Sessions: JWT HS256, 60-minute expiry, required `exp`/`sub`/`iss` claims, issuer pinned; on every request the user is reloaded and the token's role must still match the stored role (a demoted user's old token stops working).
- Brute force: 5 consecutive failures lock the account for 15 minutes (HTTP 423); failures and lockouts are audited.

## 2.4 Verified behaviour (from `evidence/security_tests.json`)

| Test | Expected | Result |
|---|---|---|
| No token | 401 | ✔ |
| Forged `alg=none` token | 401 | ✔ |
| Advisor uploads data | 403 | ✔ |
| Advisor approves financing | 403 | ✔ |
| Auditor runs credit scoring | 403 | ✔ |
| Other tenant reads invoices | 403 | ✔ |
| Other tenant runs agent | 403 | ✔ |
| XML with external entity (XXE) | 422 | ✔ |
| Upload > 5 MB | 413 | ✔ |
| Approving the same application twice | 409 | ✔ |
| Edited credit-score entry in audit log (raw SQL) | chain invalid at that entry | ✔ |
| Correct password after 5 failures | 423 locked | ✔ |

# 3. Data protection

## 3.0 Cryptographic standards in use

| Purpose | Algorithm / parameters | Standard / guidance |
|---|---|---|
| Field encryption at rest | AES-256-GCM, 96-bit random nonce, 128-bit tag, column context as AAD | NIST SP 800-38D (GCM) |
| Key hierarchy | per-value 256-bit DEK wrapped by 256-bit KEK (AES-256-GCM) | envelope pattern used by cloud KMS services |
| Password storage | PBKDF2-HMAC-SHA256, 600,000 iterations, 16-byte salt, constant-time compare | NIST SP 800-132; OWASP Password Storage Cheat Sheet |
| Session tokens | JWT HS256, 60-min expiry, `iss` pinned, `exp/sub/iss` required | RFC 7519 |
| Pseudonymous buyer ids | HMAC-SHA256 keyed with the KEK | RFC 2104 |
| Audit integrity | SHA-256 hash chain over canonical JSON | FIPS 180-4 |
| Source traceability | SHA-256 of every uploaded file | FIPS 180-4 |

## 3.1 Encryption at rest — AES-256-GCM envelope encryption

For each sensitive value (invoice amount, buyer name, buyer tax id, transaction description, amount and balance, business registration and tax ids, credit dossier, financing payload):

1. generate a fresh 256-bit data-encryption key (DEK) and 96-bit nonce;
2. encrypt the value with AES-256-GCM using the DEK; the column context (e.g. `invoice.amount`) is the additional authenticated data (AAD);
3. wrap the DEK with the master key (KEK) using AES-256-GCM (AAD `dek-wrap:<context>`);
4. store only `{version, wrapped DEK, nonces, ciphertext, context}`.

Properties: identical values produce different ciphertexts; moving a ciphertext to another column fails authentication; any bit flip raises an authentication error (tested). Because only DEKs are wrapped, the KEK can live in a KMS/HSM and be rotated by re-wrapping keys without re-encrypting data. The dashboard's *Audit Vault* tab displays the exact stored envelope to demonstrate this.

**Pseudonymous grouping:** buyers are linked by `HMAC-SHA256(KEK, tax_id)`, so analytics can group by buyer without the plaintext tax id.

## 3.2 In transit

The prototype runs HTTP on localhost. Deployment target (Cloud Run) terminates TLS 1.2+ at the edge; the dashboard calls the API over the internal network. CORS is restricted to the dashboard origin; responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.

## 3.3 Secrets

The master key and JWT secret are read from environment variables (`MICROFLOW_MASTER_KEY`, `MICROFLOW_JWT_SECRET`); docker-compose refuses to start without them. Local demo mode generates a key file (`.dev_master_key`, mode 0600, git-ignored) and prints a warning.

# 4. Integrity and accountability — hash-chained audit log

Each entry stores `prev_hash` and `entry_hash = SHA-256(prev_hash ‖ canonical_json(ts, actor, role, action, business_id, details))`, starting from a zero genesis hash. Logged events include logins (success, failure, lockout), every ingestion (with the source file's SHA-256), rejected uploads, every credit score (score, grade, model version, SHA-256 of the full dossier), every forecast, every agent run (SHA-256 of the full decision), every drafted, approved or rejected application (with provider reference), dossier views and chain verifications.

- The ORM rejects updates and deletes on `audit_logs`.
- `GET /api/audit-logs/verify` recomputes the chain and returns the first broken entry. The test suite edits a row with raw SQL (bypassing the ORM, as a DB administrator could) and confirms detection, then restores it and confirms the chain is valid again.

Questions logged by the copilot are stored as SHA-256 hashes, not text, to avoid collecting free-text personal data in the audit trail.

# 5. Input and AI-specific controls

| Threat | Control |
|---|---|
| XXE / billion-laughs in XML invoices | any `<!DOCTYPE` or `<!ENTITY>` rejected before parsing |
| Disguised file types | magic-byte detection before extension |
| Resource exhaustion | 5 MB upload cap, 50-page PDF cap, bounded query limits |
| Malformed or fraudulent records | schema validation (positive amounts, date order, required fields); duplicate invoice numbers skipped; duplicate statement files detected by hash |
| Double financing of the same invoice | approved invoices set to `financed` and excluded from future matching |
| Autonomous agent overreach | agent can draft only; owner approval required; scores < 60 also flagged for advisor review; no auto-decline |
| Unexplainable or opaque decisions | transparent scorecard with factor-level contributions; model version and dossier hash recorded |
| Prompt injection through documents | no LLM executes on document text; the copilot answers from structured results only |
| SQL injection | SQLAlchemy ORM with bound parameters throughout |

# 5b. Threat analysis (STRIDE)

| STRIDE category | Threat in MicroFlow | Control implemented | Evidence |
|---|---|---|---|
| **S**poofing | Attacker logs in as an owner; forged or `alg=none` JWT | PBKDF2 passwords, lockout after 5 failures, JWT signature + issuer + expiry checks, role re-validated per request | security_tests.json: forged token 401, brute force 423 |
| **T**ampering | Edit an invoice amount or a credit score in the database; alter the audit trail | AES-GCM authentication tag per field; AAD binds ciphertext to its column; hash-chained audit log; ORM blocks audit updates/deletes | tests: tamper raises InvalidTag; chain breaks at edited entry |
| **R**epudiation | Owner denies approving a financing request; advisor denies running a score | Every action audited with actor, role, time and decision hash; approvals recorded with user id | audit_chain.json |
| **I**nformation disclosure | DB dump or backup leaks SME financials; one tenant reads another's data; auditor sees exact figures | Field-level encryption; HMAC buyer refs; tenant scoping on every endpoint; banded amounts for auditors; no-store headers | db_dump_raw.json shows ciphertext; other tenant 403 |
| **D**enial of service | Huge uploads, PDF bombs, entity-expansion XML, login lockout abuse | 5 MB and 50-page caps; DTD/entity rejection; bounded query limits; lockout is time-limited (R6 below) | oversized upload 413; XXE 422 |
| **E**levation of privilege | Advisor approves financing; auditor runs AI; agent submits on its own | Permission checks per endpoint; approval restricted to owners; agent can only draft | advisor approve 403; auditor score 403 |

# 6. Known-risk list

| # | Risk | Severity | Status / mitigation plan |
|---|---|---|---|
| R1 | Seeded demo accounts share one known password | High (prototype only) | Disable with `MICROFLOW_SEED_DEMO=0`; production uses invited accounts with enforced password policy and MFA |
| R2 | Master key held in an environment variable / local key file, not a KMS/HSM | High | Envelope format is KMS-ready: move KEK to GCP KMS / AWS KMS, unwrap DEKs via KMS calls, add key rotation |
| R3 | JWTs are stateless; no server-side revocation before expiry (60 min) | Medium | Role is re-checked per request; add a Redis deny-list keyed by `jti` and short-lived access + refresh tokens |
| R4 | Audit chain is tamper-evident, not tamper-proof: a DB administrator could rewrite the whole chain | Medium | Periodically anchor the head hash externally (WORM storage or a signed timestamp); restrict DB roles to INSERT-only on `audit_logs` |
| R5 | No TLS in local mode | Medium | TLS terminated by the cloud platform in deployment; HSTS at the edge |
| R6 | Login lockout can be abused to lock out a known user (DoS) | Low–Medium | Add IP-based rate limiting and CAPTCHA after failures; notify the owner |
| R7 | Decrypted data exists in process memory and in the 5-minute analysis cache | Medium | In-memory cache by default; if Redis is used, keep it on a private network with AUTH and TLS, and shorten TTL |
| R8 | Pickle serialisation for the optional Redis cache | Low | Cache is private to the service; replace with JSON serialisation before production |
| R9 | Statement parser trusts document content (a forged PDF statement would be accepted) | Medium | Future: bank-API or open-banking feeds, statement signature checks, anomaly checks against instant-payment records |
| R10 | Credit model trained on synthetic data; weights expert-set | Medium (model risk) | Validate on real pilot data, fairness checks across sectors and regions, monitoring for drift; human review stays mandatory below threshold |
| R11 | No API-level rate limiting on non-auth endpoints | Low | Add gateway rate limits (Cloud Armor / API Gateway) |
| R12 | Dependency vulnerabilities | Low | Pin versions, run `pip-audit` in CI, Dependabot alerts |

# 7. Compliance alignment (design intent)

- **Egypt Personal Data Protection Law (Law No. 151 of 2020):** the design follows its core principles of purpose limitation, minimisation, security of processing and consent for disclosure to third parties; a formal legal review is still required before handling real customer data.
- **Data minimisation and purpose limitation:** only fields needed for cash-flow and credit analysis are stored; auditors see banded amounts; question text is not stored.
- **Explainability and human oversight** for credit-related outputs: factor-level reasons, owner approval for every application, advisor review below the threshold, no automated decline.
- **Traceability:** each AI output is tied to source-file hashes, model version and a decision hash in the audit chain.
- **Consent model (planned):** financing applications are shared with a provider only after explicit owner approval, which is logged.

This prototype has not been reviewed against any specific regulator's rules; the policy parameters in `config.POLICY` are illustrative and designed to be configured per jurisdiction.
