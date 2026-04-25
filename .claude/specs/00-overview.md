# FlowSync Health — Master Spec
**Version:** 1.0 | **Status:** Approved | **Owner:** Arghyadeep

## Problem Statement
Indian pharmaceutical depots lose ₹6–11 lakhs per year per depot from:
- Expiry losses (₹3–6L) — batches expire undetected on shelves
- Billing chaos (₹1–2L) — ghost stock, invoice mismatches, duplicate entries
- Cold chain failures — fridge excursions, fake temperature logs
- Reconciliation delays — payments unmatched to invoices for months
- Compliance gaps — GDP, Rule 65, Schedule M violations during audits

No existing software solves all five together with AI prediction.

## System Modules
| # | Module | Core Problem | Status |
|---|---|---|---|
| 1 | AI Billing + OCR | Manual invoice typing errors | Spec: 01-ocr-billing.md |
| 2 | Inventory + Stock Ledger | Ghost stock, ledger mismatch | Spec: 02-inventory.md |
| 3 | Expiry + FEFO Engine | Expiry losses | Spec: 03-expiry-fefo.md |
| 4 | Cold Chain Compliance | Fake logs, excursions | Spec: 04-cold-chain.md |
| 5 | Financial Reconciliation | Payment-invoice mismatches | Spec: 05-reconciliation.md |
| 6 | Returns + Credit Notes | Return disputes, credit delays | Spec: 06-returns.md |
| 7 | Compliance + Audit | GDP, Rule 65 violations | Spec: 07-compliance.md |
| 8 | Analytics + Forecasting | Reactive management | Spec: 08-analytics.md |

## Non-Functional Requirements
- API response time: < 200ms for all pre-computed reads
- OCR pipeline: < 5 seconds per invoice
- Nightly ML pipeline: < 10 minutes for 50 depots
- Uptime: 99.5% (< 4h downtime/month)
- Data retention: 6 years (GST Act requirement)
- Multi-tenancy: RLS at DB level, zero cross-tenant data leak

## Users and Roles
| Role | Access | Primary Device |
|---|---|---|
| ADMIN | All depots, system config | Web |
| MANAGER | Own depot, all modules, approve agent actions | Web + Mobile |
| STAFF | Stock-in, temperature logs, batch scan | Mobile |
| FIELD_REP | Payment collection, returns | Mobile |

## Global Constraints
- All thresholds read from compliance_config table, never hardcoded
- audit_trail is INSERT-only (IT Act 2000)
- temperature_logs is INSERT-only (WHO GDP)
- FEFO is legally required under Schedule M of Drugs & Cosmetics Rules
- Recall tracing must complete in < 10 seconds
- All invoices must have unique invoice_number per depot (GST requirement)