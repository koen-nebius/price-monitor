"""
Regression test for the Together fetcher (2026-09-06).

On 3 Sep 2026 the daily digest reported Together H100 on-demand at $1.99 (-50%). The real
on-demand price was $3.99; $1.99 is Together's PREEMPTIBLE tier, rendered in a hidden
(class="hide") column that tag-stripping surfaced, and the parser's "keep the lowest" rule
then preferred it. This test feeds a synthetic page with (a) a hidden preemptible column,
(b) a separate hidden preemptible table and (c) the reserved ladder, and asserts that
on-demand stays 3.99 / 5.99 / 8.19 and committed_short_term is the cheapest ladder tier.

Run: python3 test_together_parse.py   (exit 0 = pass, 1 = fail)
"""
import sys

from fetchers.together import _parse

PAGE = """
<h2>GPU Clusters</h2>
<table>
<tr><th>Hardware</th><th>On-demand</th><th class="hide">Preemptible Compute</th></tr>
<tr><td>NVIDIA HGX H100</td><td>$3.99</td><td class="hide">$1.99</td></tr>
<tr><td>NVIDIA HGX H200</td><td>$5.99</td><td class="hide">$2.99</td></tr>
<tr><td>NVIDIA HGX B200</td><td>$8.19</td><td class="hide">$4.09</td></tr>
<tr><td>NVIDIA HGX B300</td><td>Contact us</td></tr>
</table>
<div class="hide"><h3>Preemptible Compute</h3>
<table><tr><td>NVIDIA HGX H100</td><td>$1.99</td></tr>
<tr><td>NVIDIA HGX H200</td><td>$2.99</td></tr>
<tr><td>NVIDIA HGX B200</td><td>$4.09</td></tr></table></div>
<h2>Reserve GPU capacity</h2>
<table>
<tr><th></th><th>7-30 days</th><th>31-90 days</th><th>91-180 days</th></tr>
<tr><td>NVIDIA HGX H100</td><td>$3.69</td><td>$3.45</td><td>$3.19</td></tr>
<tr><td>NVIDIA HGX H200</td><td>$5.49</td><td>$4.99</td><td>$4.15</td></tr>
<tr><td>NVIDIA HGX B200</td><td>$7.99</td><td>$7.89</td><td>$7.79</td></tr>
</table>
"""

# Visible preemptible prices must not replace the explicitly labeled on-demand column.
PAGE_UNHIDDEN = PAGE.replace('class="hide"', 'class="pre"')

# The current page combines OD and all three reserved prices in one row. This
# fixture also includes a navigation link and a different Dedicated Inference ask.
PAGE_COMBINED = """
[GPU Clusters](#gpu-clusters)
## Dedicated Inference
| Hardware | On-demand | Reserved |
| NVIDIA HGX H100 | $5.49 | Contact sales |
## GPU Clusters
On-demand
Hardware | Hourly |
| | |
| --- | --- |
| NVIDIA HGX H100 | $3.99 |
| NVIDIA HGX H200 | $5.99 |
| NVIDIA HGX B200 | $8.19 |
On-demand hourly rates and reserved capacity
| Hardware | ON-Demand | Reserved |
| --- | --- | --- |
| Pay as you go | 7-30 days | 31-90 days | 91-180 days | 181+ days |
| | | | | | |
| NVIDIA HGX H100 | $3.99 | $3.69 | $3.45 | $3.19 | Contact us |
| NVIDIA HGX H200 | $5.99 | $4.99 | $4.15 | $3.99 | Contact us |
| NVIDIA HGX B200 | $8.19 | $7.99 | $7.79 | $6.79 | Contact us |
## Sandbox
"""
PAGE_RESERVED_ONLY = """
<h2>Reserve GPU capacity</h2>
<table><tr><th>Hardware</th><th>7-30 days</th><th>31-90 days</th><th>91-180 days</th></tr>
<tr><td>NVIDIA HGX H100</td><td>$3.69</td><td>$3.45</td><td>$3.19</td></tr></table>
"""
PAGE_AMBIGUOUS = """
## GPU Clusters
| Hardware | Price |
| NVIDIA HGX H100 | $3.99 |
"""


def _by(records):
    return {(r.gpu_model, r.consumption_type): r.price_per_gpu_hour_usd for r in records}


def main() -> int:
    fails = []
    got = _by(_parse(PAGE, "2026-09-06T00:00:00+00:00"))
    exp = {("H100", "on_demand"): 3.99, ("H200", "on_demand"): 5.99, ("B200", "on_demand"): 8.19,
           ("H100", "committed_short_term"): 3.19, ("H200", "committed_short_term"): 4.15,
           ("B200", "committed_short_term"): 7.79}
    for k, v in exp.items():
        if abs(got.get(k, -1) - v) > 1e-9:
            fails.append(f"hidden-column page: {k} expected {v}, got {got.get(k)}")

    got2 = _by(_parse(PAGE_UNHIDDEN, "2026-09-06T00:00:00+00:00"))
    for gpu, v in (("H100", 3.99), ("H200", 5.99), ("B200", 8.19)):
        if abs(got2.get((gpu, "on_demand"), -1) - v) > 1e-9:
            fails.append(f"unhidden page: {gpu} on_demand expected {v}, got {got2.get((gpu, 'on_demand'))}")

    cases = [
        ("reserved only", PAGE_RESERVED_ONLY,
         {("H100", "committed_short_term"): 3.19}),
        ("reserved-only columns inside cluster section", PAGE_RESERVED_ONLY.replace("Reserve GPU capacity", "GPU Clusters"),
         {("H100", "committed_short_term"): 3.19}),
        ("reserved rows with no labeled product", PAGE_RESERVED_ONLY.replace("Reserve GPU capacity", "Pricing"), {}),
        ("ambiguous price column", PAGE_AMBIGUOUS, {}),
        ("missing cluster section", PAGE_AMBIGUOUS.replace("GPU Clusters", "Dedicated Inference").replace("Price", "On-demand"), {}),
        ("duplicate on-demand columns", PAGE_AMBIGUOUS.replace("| Price |", "| On-demand | On-demand |").replace("$3.99 |", "$3.99 | $1.99 |"), {}),
        ("combined current layout", PAGE_COMBINED,
         {("H100", "on_demand"): 3.99, ("H200", "on_demand"): 5.99, ("B200", "on_demand"): 8.19,
          ("H100", "committed_short_term"): 3.19, ("H200", "committed_short_term"): 3.99,
          ("B200", "committed_short_term"): 6.79}),
        ("on-demand price below reserved remains explicitly on-demand", PAGE.replace("$3.99", "$2.99"),
         {**exp, ("H100", "on_demand"): 2.99}),
        ("first labeled on-demand occurrence wins", PAGE_COMBINED.replace(
            "On-demand hourly rates", "| NVIDIA HGX H100 | $2.99 |\nOn-demand hourly rates"),
         {("H100", "on_demand"): 3.99, ("H200", "on_demand"): 5.99, ("B200", "on_demand"): 8.19,
          ("H100", "committed_short_term"): 3.19, ("H200", "committed_short_term"): 3.99,
          ("B200", "committed_short_term"): 6.79}),
    ]
    for name, page, expected in cases:
        actual = _by(_parse(page, "2026-09-06T00:00:00+00:00"))
        if actual != expected:
            fails.append(f"{name}: expected {expected}, got {actual}")
    for r in fails:
        print("FAIL:", r)
    print(f"PASS ({len(cases) + 2} fixtures)" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
