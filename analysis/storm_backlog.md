# STORM audit backlog (2026-07-06)

Multi-perspective audit of the daily monitor outputs (7 reader personas: pricing PM,
finance, sales AE, capacity, exec, data-quality, PVM auction designer). Full findings
in storm_audit_2026-07-06.json. The 5 cheap/high fixes shipped in commit dde8889.
Remaining items, ranked; owner = Koen unless noted.

## Needs a decision (not just code)
1. BAND-INPUTS TABLE: pricing PM wants floor/anchor/cap computed per SKU on the page.
   CONFLICT: publishes Nebius's pricing formula to a page sales + broad audience can
   read (negotiation leverage if leaked). Options: (a) don't publish, keep in
   pricing_strategy.py runs; (b) restricted Confluence page; (c) publish only the
   anchor (peer median x1.05), keep floor/cap internal.
2. SELF-MOVE ATTRIBUTION (urgent before the ~Aug price change lands): every delta
   ("-4% vs median", "Changed in 24h", 30d trend) silently shifts when NEBIUS moves
   its own price. When H200/B200/B300 +5% go live, the daily post will report
   position changes that are our own move. Fix: detect nebius-price day-over-day
   change and lead with "we repriced: H200 4.50->4.73; position deltas reflect our
   move, not the market."
3. FIELD-INTEL CONFIDENTIALITY: rows carry competitor + deal specifics (HUMAIN 4k
   B300 London, LAI ~5k B200...). Now marked "internal only" but governance
   (page restrictions?) is an owner decision.
4. WIN-SIDE BIAS: #price-intelligence logs scary quotes and losses, never wins, so
   the pack structurally over-reports price pressure. Consider asking AEs to log
   wins-at-price, or add a static caveat to the field-intel sections.

## Medium effort, high value
5. Spot/auction page restructure for RFC 055 (PVM auctions): split "Market floor
   reference" into spot-clearing anchor (hyperscaler spot median + SF Compute) vs
   negotiated-committed floor (field intel, term shown); stop min()-ing across
   mechanisms; add 30d spot volatility (min/median/max) from store/history.csv.
6. Spot hygiene everywhere: route regional spot tables + battlecard spot numbers
   through _representative_spot_floor's phantom filter; label spot==OD cells as
   "OD fallback, no real spot signal" (Azure H200/GB200 rows).
7. Peer-median trend: track the median series per SKU in history.csv (today's 30d
   trend follows only the cheapest peer's own price, not the anchor input).
8. Action-flag lifecycle: flags have no state/age/retirement (B300 lost-deal
   re-fires until the row ages out at 90d, then vanishes silently). Add first-seen
   date + a way to acknowledge ("reviewed, holding").

## Small
9. Battlecard: add the "cheaper neocloud/marketplace quote" objection card (most
   common AE objection; numbers already on the page) + AWS negotiated $1.80 line
   with escalate-don't-argue guidance.
10. Exec-benchmark caption: "cheapest available per provider" -> "cheapest
    cluster-class (8x-node) list price" + note the B300/L40S all-peers fallback.
11. Date + age-cap the SemiAnalysis "sold out across all GPU types (Apr 2026)"
    claim like committed prices already are.
12. GB300 exec fallback: when the overall-lowest deal's term has no Nebius tier,
    compare the lowest deal at a term we do offer.
13. Field-intel dedup: name-insensitive key misses Undisclosed->named twins and
    over-collapses genuine joint offers (Verda+CIVO); needs a smarter rule.
