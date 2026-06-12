"""Model training — Phase 3.

Trains one regression and one classification model per horizon
(10min, 1hr, 3hr, 6hr, 12hr, 24hr, multi-day) using walk-forward CV.
See HANDOFF_PLAN_2.md Phase 3 for the full spec.
"""

# TODO (Phase 3): implement training pipeline.
# - Time-based train/test split (no shuffling)
# - TimeSeriesSplit walk-forward CV within the training set
# - Benchmark Linear / Ridge / XGBRegressor (regression)
# - Benchmark Logistic / XGBClassifier (classification)
# - Persist best model per horizon to MODELS_DIR
