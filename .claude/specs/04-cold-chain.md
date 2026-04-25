# Module 4 — Cold Chain Compliance
**Status:** Spec Approved | **Build Status:** Not Started

## Problem Statement
Vaccines, insulin, and biologics require 2-8°C storage.
Depots maintain paper temperature logs that are frequently faked —
same temperature written daily regardless of actual readings.
Manufacturers and inspectors have no tamper-proof evidence.
A cold chain excursion can spoil an entire fridge worth of product
(₹50,000-5,00,000) and invalidate the batch for dispensing.

## Functional Requirements
FR1: Staff photographs thermometer twice daily (9 AM + 3 PM minimum)
FR2: OCR extracts temperature reading from photo automatically
FR3: GPS + timestamp + user_id + device_id captured on every log
FR4: System checks reading against product-specific thresholds (not global)
FR5: Excursion (reading outside threshold): WhatsApp + Firebase alert fires immediately
FR6: AI fraud detection: flags identical photos submitted on different days
FR7: Behavioral anomaly: flags staff submitting logs without being at depot location
FR8: Cold chain compliance PDF auto-generated per batch on request
FR9: GDP-compliant temperature log report exportable for inspectors
FR10 (IoT): ESP8266 + DS18B20 reads every 10 min, sends via MQTT
FR11 (IoT): Continuous sensor data stored alongside manual photo logs

## API Contracts
POST /temperature/photo-log
  Request: multipart/form-data {
    photo: file, depot_id, batch_id (optional),
    fridge_id, logged_by: user_id
  }
  Response: {
    log_id, extracted_temp: float, confidence: float,
    is_excursion: bool, threshold_min, threshold_max,
    alert_fired: bool
  }
  Side effects: temperature_logs (INSERT only), alert if excursion

GET /temperature/excursions/{depot_id}
  Query params: from_date, to_date
  Response: [{ log_id, timestamp, reading, threshold_breached,
               photo_url, logged_by, fridge_label, batch_id }]

GET /temperature/compliance-report/{depot_id}
  Query params: month, year
  Response: PDF download
  Contents: daily log table, excursion summary, compliance score 0-100,
            AI flags (suspicious entries), signature field

GET /temperature/cold-chain-certificate/{batch_id}
  Response: PDF with batch temp history, min/max/avg, excursion count,
            compliance status (PASSED/FAILED), route info

## Temperature Thresholds (per product, from products table)
Cold chain (vaccines, insulin): 2°C - 8°C
General cool storage: 15°C - 25°C
Deep freeze (some biologics): -20°C
Values stored in products.storage_temp_min / storage_temp_max
Excursion detector reads per-product thresholds, never global

## Fraud Detection (3 layers)
Layer 1 — Duplicate image: hash photo, compare against last 30 entries for same fridge
Layer 2 — Spatial check: GPS must be within 500m of depot coordinates
Layer 3 — Behavioral: same temperature (±0.1°C) for 4+ consecutive logs → flag

## IoT Path (MQTT subscriber — separate Docker container)
ESP8266 → MQTT topic: flowsync/{depot_id}/temperature
Payload: { device_id, depot_id, temp_celsius, timestamp }
Subscriber: validates → checks threshold → writes temperature_logs → alerts if excursion
IoT logs: reading_type = 'SENSOR', photo_url = null
Photo logs: reading_type = 'PHOTO', device_id = null

## Compliance Score Formula
score = 100
- 10 per missing log session (expected: 2/day)
- 15 per excursion not investigated within 24h
- 20 per AI-flagged suspicious entry confirmed by manager
- 5 per log submitted > 2 hours late

## Edge Cases
- Analog thermometer photo: PaddleOCR digit recognition, confidence < 0.7 → manual entry required
- Digital display photo (LCD): standard digit OCR, high confidence expected
- Staff submits log from home: GPS check fails → photo rejected, reminder sent
- Excursion detected at 2 AM via IoT: WhatsApp fires immediately, not queued
- Batch with excursion history: cold chain certificate shows FAILED, batch flagged for review
- IoT device offline > 35 min: device offline alert to manager (Celery task)
- Multiple fridges same depot: each fridge has separate iot_device record and threshold set

## Acceptance Criteria
- [ ] temperature_logs INSERT-only enforced (trigger raises exception on UPDATE/DELETE)
- [ ] Photo OCR extracts temperature value with confidence score
- [ ] Excursion triggers WhatsApp within 60 seconds of log submission
- [ ] Duplicate photo detection catches same image submitted twice
- [ ] GPS check rejects log from location > 500m from depot
- [ ] Cold chain certificate PDF generated correctly for batch with no excursions
- [ ] Cold chain certificate shows FAILED for batch with ≥ 1 confirmed excursion
- [ ] IoT subscriber writes to temperature_logs using same schema as photo path
- [ ] Compliance score computed correctly for month with 2 missing sessions