# Reserve/committed GPU pricing: external source map

Deep research 2026-07-07 (101-agent verified sweep; 23 claims survived 3-vote
adversarial verification). Purpose: fill the monitor's committed-pricing gap
(previously only hyperscaler list + #price-intelligence field intel).

## Integrated into the monitor (2026-07-07)
| Source | What | Access | Status |
|---|---|---|---|
| Vast.ai reserved API | 1-6mo prepaid marketplace offers, per-offer $/GPU-hr, region | free, unauth (curl-style UA required; browser UAs get 403) | LIVE — fetchers/vast_reserved.py, ct=reserved_short |
| SF Compute fills API | transacted forward/term window prices ($/node-hr ÷ 8), resold reserved contracts | free bearer token (SFCOMPUTE_TOKEN; `sf tokens create` after signup) | BUILT, token-gated — fetchers/sfcompute_fills.py; provider registers only when token set. STATUS 2026-07-07: Koen WAITLISTED (manual application review; competitor-domain signup may be rejected — if so, leave parked; do not sign up with a personal email to route around their review). Monitor already carries their public H100 spot price via the existing sfcompute provider. |
Both render in "Short-term reserved market (1-6 month commitments)" on the main
page and the spot/auction page. Deliberately ct=reserved_short so they never mix
into the 1yr+ committed benchmark tables.

## Evaluate commercially
- **SemiAnalysis GPU Pricing Index**: the only public committed TERM STRUCTURE —
  monthly survey of 100+ participants, 25th-75th pct contract ranges, 10 tenors
  (OD→5yr, 25% prepay assumed 3m+), 9 SKUs incl B200/B300/GB200/GB300. Free tier:
  H100 1yr index only. Full data + API (11M datapoints): sales@semianalysis.com,
  institutional pricing unpublished. Koen checking existing org access in
  #product-team-internal (2026-07-07).

## Parked (blockers documented)
- **Compute Exchange**: reserved listings with 1-36mo terms from verified
  neoclouds; research verified prices ARE published, but the public site is a
  Framer/JS shell with no unauthenticated listing API found (probed 2026-07-07:
  app.compute.exchange serves an SPA shell; /marketplace 404). Needs an account
  or headless browser. Forward contracts are RFQ-only either way.

## Watch list (not live)
- **CME x Silicon Data "Compute futures"** (announced 2026-05-12) and
  **ICE x Ornn** (2026-05-19): both pending regulatory review, zero published
  prices, and both reference ON-DEMAND indices — a forward on on-demand, not a
  committed-deal benchmark. Ornn OCPI covers H100/H200/B200/A100/RTX5090 only
  (B300 marketing claim not borne out by published series).
- **Silicon Data SDH100RT**: deep private-deal inputs but normalizes everything
  to an on-demand equivalent — no committed terms published.

## Verified-empty categories
Nothing survived verification for: brokers/consultancies publishing deal
benchmarks, forums/X feeds leaking committed prices, 2025-26 news articles with
hard committed datapoints. Public leakage of real negotiated deals is rare →
#price-intelligence field intel (loss side) + reserve_wins.csv (our win side)
remain irreplaceable and have no external substitute.
