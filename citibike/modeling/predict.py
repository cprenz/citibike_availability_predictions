"""Inference — Phase 4.

Loads trained per-horizon models and serves predictions for the web app.
See HANDOFF_PLAN_2.md Phase 4.
"""

# TODO (Phase 4): implement inference.
# - Load best model per horizon from MODELS_DIR
# - Build feature vector for a (station_id, now) request
# - Return bike-count regression + availability probability per horizon
