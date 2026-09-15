"""gpuhunt cross-check parser: per-GPU normalization, spot exclusion, gpu_name map."""
from fetchers.gpuhunt import parse_min_on_demand

ROWS = [
    {"instance_name": "gpu_8x_b200_sxm6", "price": "53.52", "gpu_count": "8", "gpu_name": "B200", "spot": "False"},
    {"instance_name": "gpu_1x_b200_sxm6", "price": "6.99", "gpu_count": "1", "gpu_name": "B200", "spot": "False"},
    {"instance_name": "gpu_8x_b200_sxm6", "price": "20.00", "gpu_count": "8", "gpu_name": "B200", "spot": "True"},   # spot: ignored
    {"instance_name": "gpu_8x_a100", "price": "10.32", "gpu_count": "8", "gpu_name": "A100", "spot": "False"},        # not tracked
    {"instance_name": "rtx", "price": "3.03", "gpu_count": "1", "gpu_name": "RTXPRO6000", "spot": "False"},
    {"instance_name": "cpu", "price": "0.50", "gpu_count": "0", "gpu_name": "", "spot": "False"},                      # no GPU
    {"instance_name": "bad", "price": "abc", "gpu_count": "8", "gpu_name": "H100", "spot": "False"},                  # unparsable
]

out = parse_min_on_demand(ROWS, "lambda")
assert out[("lambda", "B200")] == 6.69, out           # 53.52 / 8 beats 6.99 / 1
assert out[("lambda", "RTX6000")] == 3.03, out
assert ("lambda", "A100") not in out
assert ("lambda", "H100") not in out
assert len(out) == 2, out
print("PASS (gpuhunt parse: 7 rows -> 2 cross-check cells)")
