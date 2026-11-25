# PGuard — Handover v10 (Phase 5 frontend)

Date: 2026-09-08. Status: Phase 5 DONE. All planned phases complete;
remaining work is data-blocked (see v9 §6 / Phase 4 report wishlist).

Base records unchanged: v8 (project context, Phases 0–2.5, bag record,
Project B), v9 (Phase 3), `Phase4_Evaluation_Report.md`. This file covers
Phase 5 only.

## 1. What was built (all frontend except two backend endpoints)

- **`GET /api/ml-status`** (cached, mode-independent, fail-soft): serves
  model facts from `ml/artifacts/metadata.json`. Verified live over HTTP,
  including the NaN-sanitization fix it exposed (`glitch_row_flag_frac`
  is NaN in metadata; FastAPI's strict JSON encoder 500s on it — same bug
  class as the GPS NaN fix, routed through existing `_sanitize()`).
- **ML panel: built, then removed on Ahmed's call.** Static facts
  duplicating metadata.json + session counts duplicating the health feed.
  Endpoint kept for the report; zero panel remnants (grep-verified).
- **Incident grouping** (`app.js`, display-only, Grouped/Flat toggle):
  fault-family events (hardware_fault + ml + failure-by-key-prefix) chain
  into incident cards across ≤60s bridges; same key refiring after a long
  quiet opens a new incident (caught by headless test before shipping);
  raw history array untouched. Validated against real dumps: fault window
  one incident, blip merged, ML transients own cards, seam refires handled.
  Headless tests: `scratch/test_frontend_logic.cjs` (node, no runner).
- **Alert triage**: filter chips (All/Critical/Warning/ML/Failures) compose
  with grouping (single-kind filters render flat); `.failure-row` red
  border; `.incident`/`.filter-chip`/`.ghost-btn` CSS added.
- **`GET /api/report?session_id=&format=json|html`**: session events (10k
  cap), per-topic coverage (`db.session_topic_stats`, new), ML facts,
  detector config snapshot. HTML single-file printable, all robot strings
  escaped. Export buttons use the current session. Verified live: json,
  html, and bad-format paths.

## 2. Verification status

- Backend suite 32 green (no detector changes → golden untouched, still
  stat 33 / resid 20 / hw 10 / gnss 12 / sensor 5 / comms 8 / failure 23 /
  ml 5).
- JS syntax clean; frontend-logic tests green (6 blocks incl. the
  pairing-bug regression test).
- Endpoint checks over real HTTP against a live boot; report verified
  against Ahmed's own live virtual-robot session already streaming
  (16 topics incl. /clock — relay exclusions confirmed working: /rosout
  present via native path, no relay duplicates).
- Pixels not verified here (no browser in this environment): 2-minute
  eyeball checklist recorded in README §6-equivalent — connect (bag or
  live), fault-window incident cards vs `events_A.json`, filters+grouping
  compose, exports validate, disconnect/reconnect clean, no console errors.

## 3. Decisions locked in Phase 5

- Detectors never negotiate; grouping is presentation-only (from the
  cascade-blindness review — unchanged).
- No live score streaming (5Hz noise nobody watches); scores stay in event
  messages + report.
- All-rows-render policy stands (Ahmed).
- Vanilla JS, no new dependencies, no schema/DB changes (one read-only
  aggregate query).

## 4. What's left (nothing codeable without new inputs)

- Real-robot unknowns (v8 §1), data wishlist (Phase 4 report §8),
  GPU/ARM target verification, `docker compose up` from a clean checkout
  in this environment (Ahmed runs it live; never run here).
- Next human steps: the §2 eyeball checklist, supervisor read-through of
  the Phase 4 report + one HTML export.
