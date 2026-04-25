# Project-Level Claude Instructions

## What Claude Should Always Do
- Read the relevant spec in `.claude/specs/` before building any module
- Check `ml/shared/config_loader.py` before writing any threshold value
- Write docstrings on every function explaining inputs, outputs, side effects
- After editing any `ml/` file, confirm the import chain still works

## What Claude Should Never Do
- Hardcode thresholds (0.85, 0.60, 2.5, 0.6) anywhere except compliance_config seed
- Change COORDINATOR_PRIORITY order in `ml/agents/base.py`
- Change stage order in `ml/inference/orchestrator.py`
- Remove `class_weight='balanced'` from expiry risk model
- Remove `CalibratedClassifierCV` from expiry risk model
- Add UPDATE or DELETE permissions to audit_trail or temperature_logs tables
- Duplicate feature engineering code between training and inference

## SDD Flow — Always Follow This
Every feature must go through:
Spec (.claude/specs/) → Design → Tasks → Build → Validate (tests pass)
Never build without a spec. If spec is missing, write it first.

## Current Build Status
- [x] ml/shared/config_loader.py
- [x] ml/features/ (all three files)
- [x] ml/registry/model_store.py
- [x] ml/models/ (notebooks — demand + expiry)
- [x] ml/inference/ (orchestrator + all infer files)
- [x] ml/agents/ (base + all agents + coordinator)
- [x] ml/pipeline/auto_trainer.py
- [x] ml/drift/psi_monitor.py
- [x] database/schema.sql + triggers + seeds
- [x] backend/tasks.py (Celery)
- [ ] backend/main.py
- [ ] backend/config.py
- [ ] backend/database.py
- [ ] backend/models/ (ORM)
- [ ] backend/schemas/ (Pydantic)
- [ ] backend/middleware/
- [ ] backend/services/
- [ ] backend/routers/
- [ ] iot/
- [ ] tests/

## Pricing (Never Suggest Changing This)
Starter ₹2,499 · Professional ₹4,999 · Enterprise ₹9,999 · IoT ₹1,299/fridge