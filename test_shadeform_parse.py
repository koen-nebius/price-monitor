"""Shadeform parser: cents->USD per GPU, spot from availability, skip list, cheapest wins."""
from fetchers.shadeform import parse

ITEMS = [
    {"cloud": "verda", "shade_instance_type": "B300x8", "gpu_type": "B300", "num_gpus": 8, "hourly_price": 6164,
     "interconnect": "sxm6", "nvlink": True, "vcpus": 240, "memory_in_gb": 2040,
     "availability": [{"region": "helsinki-finland-2", "display_name": "FI, Central", "available": False, "rental_type": "on_demand"}]},
    {"cloud": "lambdalabs", "shade_instance_type": "B200x8", "gpu_type": "B200", "num_gpus": 8, "hourly_price": 5352,
     "interconnect": "sxm6", "nvlink": False, "availability": [
         {"region": "dulles-usa-1", "display_name": "US, Dulles, VA", "available": False, "rental_type": "on_demand"},
         {"region": "dulles-usa-1", "display_name": "US, Dulles, VA", "available": True, "rental_type": "spot", "hourly_price": "26.76"}]},
    {"cloud": "lambdalabs", "shade_instance_type": "B200x1", "gpu_type": "B200", "num_gpus": 1, "hourly_price": 699,
     "interconnect": "sxm6", "nvlink": False, "availability": []},
    {"cloud": "excesssupply", "shade_instance_type": "H200x8", "gpu_type": "H200", "num_gpus": 8, "hourly_price": 3200, "availability": []},  # skipped
    {"cloud": "massedcompute", "shade_instance_type": "A100x8", "gpu_type": "A100_80G", "num_gpus": 8, "hourly_price": 1000, "availability": []},  # not tracked
    {"cloud": "hyperstack", "shade_instance_type": "RTX", "gpu_type": "RTXPro6000", "num_gpus": 1, "hourly_price": 180, "interconnect": "pcie", "availability": []},
]

recs = {(r.provider, r.gpu_model, r.consumption_type): r for r in parse(ITEMS, "2026-09-15T00:00:00+00:00")}
v = recs[("sf_verda", "B300", "on_demand")]
assert v.price_per_gpu_hour_usd == 7.705 and v.gpu_count == 8 and v.region == "FI, Central" and v.form_factor == "SXM", v
l = recs[("sf_lambdalabs", "B200", "on_demand")]
assert l.price_per_gpu_hour_usd == 6.69, l          # 8x $53.52 beats 1x $6.99
s = recs[("sf_lambdalabs", "B200", "spot")]
assert s.price_per_gpu_hour_usd == 3.345, s          # $26.76 / 8
assert not any(p.startswith("sf_excesssupply") for p, _, _ in recs)
assert ("sf_massedcompute", "A100", "on_demand") not in recs
r = recs[("sf_hyperstack", "RTX6000", "on_demand")]
assert r.price_per_gpu_hour_usd == 1.8 and r.form_factor == "PCIe", r
assert len(recs) == 4, sorted(recs)
print("PASS (shadeform parse: 6 items -> 4 records)")
