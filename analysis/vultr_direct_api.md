# Vultr direct public catalogue

Verified 17 September 2026 against the unauthenticated official endpoint:
https://api.vultr.com/v2/plans-metal

The endpoint returned 23 bare-metal plans. The tracked NVIDIA models were
H100, L40S and B200, each as an eight-GPU node. No B300 plan was returned.

| Model | Whole-node on-demand USD/h | Per-GPU on-demand USD/h | Per-GPU preemptible USD/h | Deployment qualification |
|---|---:|---:|---:|---|
| H100 | 23.92 | 2.99 | 2.30 | On-demand disabled; preemptible enabled; no listed locations |
| L40S | 13.368 | 1.671 | 1.49 | On-demand disabled; preemptible enabled; no listed locations |
| B200 | 68.00 | 8.50 | 3.20 | On-demand disabled; preemptible enabled; no listed locations |

These are catalogue observations, not currently purchasable PAYG alternatives
or verified stock. They must not populate ordinary public PAYG medians. The
older aggregator B200 value of $3.50/GPU-hour is not reproduced by this API.

## Record contract

- Keep the exact `vbm-*` ID, explicit GPU count and consumption tier. Divide the
  entire node's `hourly_cost` or `hourly_cost_preemptible` by `gpu_count`.
- Only `invoice_type=hourly` is supported. Do not derive an hourly rate from a
  monthly amount or infer GPU count from VRAM. Invalid or conflicting identity,
  count, currency or price units are skipped.
- `locations` means regions valid for this plan, not a live inventory count.
  Each named region gets a price row; an empty list becomes `unspecified`.
- `price_basis=public_catalog` requires an explicit true deployment flag and
  at least one listed location. It still does not establish successful launch,
  quota, stock quantity or multi-node interconnect.
- Restricted values are `public_catalog_ondemand_disabled`,
  `public_catalog_preemptible_disabled`, `public_catalog_no_locations`, and
  `public_catalog_deployment_unknown`. All are catalogue reference rows, outside
  ordinary comparison and price-change alert populations.
- The legacy `vcpu` field holds the explicitly reported bare-metal CPU threads,
  not physical cores. `ram_gb` is the API memory field divided by 1024; it is the
  source's quantity rather than a correction to round marketing memory. In
  particular, the current H100 API reports 2,063,722 MB, approximately 2,015.35
  GiB, despite its `2048gb` plan ID.
- `storage_gb` is the source's GB per disk multiplied by disk count. It does not
  assert a RAID-usable capacity. B200 currently reports 3,576 GB x 8 = 28,608 GB.
- Unknown form factor and interconnect remain unknown. Generic H100/B200 names
  must not acquire an SXM or InfiniBand claim during downstream enrichment.

The reader follows opaque `meta.links.next` cursors with `per_page=500`, checks
the full plan count against `meta.total`, and rejects repeated pages or changing
totals. It makes only unauthenticated GET requests and has no invented fallback.

## Source and integration notes

Official field and pagination documentation:

- https://docs.vultr.com/reference/terraform/data-sources/bare_metal_plan
- https://docs.vultr.com/reference/vultr-cli/plans/metal
- https://github.com/vultr/vultr-csharp/blob/main/docs/PlansMetal.md
- https://github.com/vultr/vultr-csharp/blob/main/docs/MetaLinks.md

Suppress historical Vultr aggregator aliases in ordinary comparisons even when
the direct source fails; otherwise a known tier/configuration ambiguity silently
returns as current public PAYG. A source failure should remain failed or use the
normal clearly dated direct-source cache. This reader covers bare-metal GPU
plans; Vultr virtual and fractional GPU offers are a separate population and are
not implicitly covered by this endpoint.

Validation: `python3 -m unittest test_vultr_parse -v` plus a live read-only fetch.
