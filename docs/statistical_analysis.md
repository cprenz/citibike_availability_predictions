# Statistical Analysis Plan

Hypothesis tests on Citi Bike station data and A/B tests for the marketing
funnel. Each test states H0/H1, defines a metric, extracts groups from the
database, checks assumptions, and reports both a p-value and a confidence
interval (p-value alone is not enough — the CI shows practical significance).

## Methodological Notes

### Observed vs forecast weather
- **Observed** weather = what actually happened; used for lag features.
- **Forecast** weather = model prediction; used for forward-looking features.
- The split is maintained throughout to avoid feature leakage at inference time.

### Central Limit Theorem
With thousands of GBFS snapshots, sample means approximate normality via the
CLT regardless of the underlying distribution. We still run Shapiro-Wilk to be
rigorous before choosing t-test vs Mann-Whitney U.

### Normality check decision tree
```
Run Shapiro-Wilk
      |
p > 0.05 (normal)     --> two-sample t-test
      |
p < 0.05 (not normal) --> Mann-Whitney U
```

### Type 1 and Type 2 errors
- **Type 1 (false positive):** reject H0 when it is true. Controlled by alpha (0.05).
- **Type 2 (false negative):** fail to reject H0 when it is false. Controlled by
  power (80%) via power analysis.

---

## Hypothesis Tests (continuous station data — t-test / Mann-Whitney U)

1. Do e-bikes get taken faster than regular bikes during rush hour?
2. Do stations near subway exits have lower availability than others?
3. Are 6–9am and 5–7pm statistically different in depletion rate?
4. Do stations near Central Park have different availability on weekends vs weekdays?
5. Is bike depletion rate significantly higher on days with precipitation?
6. Do classic bikes or e-bikes sit idle longer at low-traffic stations?
7. Is there a significant difference in dock availability between tourist-heavy
   and residential neighborhoods?

### Procedure (each hypothesis test)
1. State H0 and H1.
2. Define the metric (depletion rate, availability count, etc.).
3. Extract the two groups from the database.
4. Run Shapiro-Wilk normality check.
5. Run a two-sample t-test (or Mann-Whitney U if non-normal).
6. Calculate the 95% confidence interval for the difference between groups.
7. Report the p-value **and** the confidence interval.
8. If p < 0.05, reject H0.

---

## A/B Tests — Chi-squared (binary outcomes, marketing funnel)

1. Does targeting Brooklyn vs Manhattan yield a higher conversion rate?
2. Does targeting commuters vs general NYC users get a higher CTR?
3. Does convenience-focused vs data-focused ad copy get a higher CTR?
4. Does a push notification vs none lead to more app opens?

### Procedure (chi-squared A/B test)
1. State H0 and H1; define the metric (CTR or conversion rate).
2. Run a power analysis (baseline rate, MDE, alpha = 0.05, power = 80%) for
   required sample size.
3. Randomly serve variant A and variant B.
4. Run until the required sample size is reached — never stop early (peeking).
5. Check for sample ratio mismatch (~50/50 split).
6. Run the chi-squared test; calculate the p-value.
7. Calculate the 95% confidence interval for the difference in proportions.
8. If p < 0.05, scale the winning variant.

---

## A/B Tests — t-test (continuous outcomes, in-app behavior)

1. Does a 12-hour push notification lead to more sessions per week vs none?
2. Does a 1-hour vs 6-hour prediction interval lead to higher avg session duration?
3. Does map view vs list view lead to more sessions per week?
4. Does showing confidence intervals vs a single prediction lead to more time on
   the prediction screen?
5. Does a 12-hour vs 3-hour forecast notification lead to more predictions viewed
   per session?

### Procedure (t-test A/B test)
1. State H0 and H1; define the continuous metric (sessions, duration, etc.).
2. Run a power analysis for required sample size.
3. Randomly assign users to variant A or B.
4. Run until the sample size is reached — never stop early.
5. Check for sample ratio mismatch.
6. Run a two-sample t-test; calculate the p-value.
7. Calculate the 95% confidence interval for the difference between groups.
8. If p < 0.05 and the CI is practically meaningful, roll out the winner.

---

## Power Analysis

- **Prospective (before collecting data):** estimate how many weeks of GBFS
  snapshots are needed to detect a given MDE. Useful for planning.
- **Retrospective (after a failed test):** if we fail to reject H0, check whether
  we had enough data to detect a real effect — distinguishes "no effect" from
  "underpowered" (Type 2 error risk).

Scale of available data:
- 1 day ≈ 288 snapshots × ~1,500 stations ≈ 430,000 rows
- 1 week ≈ ~3 million rows

So statistical significance is reached quickly; the bigger concern is practical
significance, not statistical significance.

**Implementation (statsmodels):**
- `proportion_effectsize` + `NormalIndPower` for proportion tests (CTR, conversion).
- `TTestIndPower` for continuous metrics (depletion rate).

---

## Common A/B Test Pitfalls
- **Peeking:** stopping early when p < 0.05 inflates false positives.
- **Novelty effect:** users engage because something is new, not better.
- **Sample ratio mismatch:** unequal groups from a randomization bug.
- **Seasonality:** running during an unusual period skews results.
- **Multiple testing:** many simultaneous tests compound the false-positive rate —
  apply a Bonferroni correction.
- **Spillover:** boroughs are not fully isolated (Manhattan users may see Brooklyn
  ads) — acknowledge as a limitation.
