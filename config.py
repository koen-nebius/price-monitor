NEBIUS_GPUS = ["H100", "H200", "B200", "B300", "GB200", "GB300", "L40S"]

# Map normalized GPU model → provider-specific instance types / SKUs
# Each entry: {"instance_type": str, "gpu_count": int, "vcpu": int, "ram_gb": float}
GPU_MAP = {
    "aws": {
        "H100": [
            {"instance_type": "p5.48xlarge",  "gpu_count": 8, "vcpu": 192, "ram_gb": 2048},
        ],
        "H200": [
            {"instance_type": "p5e.48xlarge",  "gpu_count": 8, "vcpu": 192, "ram_gb": 2048},
            {"instance_type": "p5en.48xlarge", "gpu_count": 8, "vcpu": 192, "ram_gb": 2048},
        ],
        "B200": [
            {"instance_type": "p6-b200.48xlarge", "gpu_count": 8, "vcpu": 192, "ram_gb": 2048},
        ],
        "B300": [
            {"instance_type": "p6-b300.48xlarge", "gpu_count": 8, "vcpu": 192, "ram_gb": 4096},
        ],
        "L40S": [
            # Exclude g6e.8xlarge (32 vCPU/GPU) and g6e.16xlarge (64 vCPU/GPU) —
            # CPU-heavy instances that inflate per-GPU price; not representative for GPU comparison
            {"instance_type": "g6e.xlarge",   "gpu_count": 1,  "vcpu": 4,   "ram_gb": 16},
            {"instance_type": "g6e.2xlarge",  "gpu_count": 1,  "vcpu": 8,   "ram_gb": 32},
            {"instance_type": "g6e.4xlarge",  "gpu_count": 1,  "vcpu": 16,  "ram_gb": 64},
            {"instance_type": "g6e.12xlarge", "gpu_count": 4,  "vcpu": 48,  "ram_gb": 192},
            {"instance_type": "g6e.24xlarge", "gpu_count": 4,  "vcpu": 96,  "ram_gb": 384},
            {"instance_type": "g6e.48xlarge", "gpu_count": 8,  "vcpu": 192, "ram_gb": 768},
        ],
    },
    "gcp": {
        "H100": [
            {"instance_type": "a3-highgpu-1g",  "gpu_count": 1, "vcpu": 26,  "ram_gb": 234},
            {"instance_type": "a3-highgpu-2g",  "gpu_count": 2, "vcpu": 52,  "ram_gb": 468},
            {"instance_type": "a3-highgpu-4g",  "gpu_count": 4, "vcpu": 104, "ram_gb": 936},
            {"instance_type": "a3-highgpu-8g",  "gpu_count": 8, "vcpu": 208, "ram_gb": 1872},
        ],
        "H200": [
            {"instance_type": "a3-ultragpu-8g", "gpu_count": 8, "vcpu": 208, "ram_gb": 1872},
            {"instance_type": "a3-megagpu-8g",  "gpu_count": 8, "vcpu": 208, "ram_gb": 1872},
        ],
    },
    "azure": {
        "H100": [
            {"instance_type": "Standard_ND96isr_H100_v5", "gpu_count": 8, "vcpu": 96, "ram_gb": 900},
            # MI300X removed — AMD GPU, not H100
        ],
        "H200": [
            {"instance_type": "Standard_ND96isr_H200_v5",       "gpu_count": 8, "vcpu": 96,  "ram_gb": 1100},
        ],
        "GB200": [
            {"instance_type": "Standard_ND128isr_NDR_GB200_v6", "gpu_count": 4, "vcpu": 128, "ram_gb": 900},
        ],
        "GB300": [
            {"instance_type": "Standard_ND128isr_GB300_v6",     "gpu_count": 4, "vcpu": 128, "ram_gb": 864},
        ],
    },
}

# AWS regions to check (those with GPU instance availability)
AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
    "ap-southeast-1", "ap-northeast-1",
]

# GCP regions to check
GCP_REGIONS = [
    "us-central1", "us-east4", "us-west1",
    "europe-west4", "europe-west1",
    "asia-southeast1", "asia-northeast1",
]

# Azure regions to check
AZURE_REGIONS = [
    "eastus", "eastus2", "westus2", "westus3",
    "westeurope", "northeurope", "germanywestcentral",
    "southeastasia", "japaneast",
]

PROVIDERS = ["aws", "gcp", "azure", "coreweave", "lambda", "crusoe", "nebius", "nebius_committed", "computeprices"]

# ---------------------------------------------------------------------------
# IREN (formerly Iris Energy) — GPU cloud competitor mentioned in sales calls.
# Not yet listed on ComputePrices.com. Add pricing here when available:
# MANUAL_PRICES[("iren", "H100", "on_demand", "us-east")] = X.XX
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Provider tiers — used to segment competitive analysis
# ---------------------------------------------------------------------------
# hyperscaler:       Bundled SLA, CPU/networking, enterprise contracts. High
#                    rack-rate sticker price; real customer price is 40-57%
#                    lower at 3yr committed. Not apples-to-apples with Nebius.
# raw_gpu_cloud:     GPU-first providers. Direct competitive set for Nebius.
#                    Price comparisons here are the most actionable.
# managed_inference: Abstracted inference platforms (per-token billing model).
#                    Include for awareness but exclude from raw compute comps.
# ---------------------------------------------------------------------------
PROVIDER_TIERS = {
    # Full-platform cloud providers with enterprise contracts, SLAs, and committed pricing.
    # Oracle Cloud (cp_oracle) is included here; its prices come from ComputePrices.com
    # and should be treated as directional estimates until verified against OCI directly.
    "hyperscaler": [
        "aws", "gcp", "azure", "cp_oracle",
    ],
    # All GPU cloud providers tracked (used for broad market sweep and raw price tables).
    # Includes commodity rental marketplaces — not used for executive positioning tables.
    "raw_gpu_cloud": [
        "nebius", "coreweave", "lambda", "crusoe",
        "cp_hyperstack", "cp_voltage", "cp_runpod", "cp_digitalocean",
        "cp_genesis", "cp_denvr-dataworks", "cp_massedcompute",
        "cp_oblivus", "cp_gmi-cloud", "cp_atlas-cloud", "cp_seeweb",
        "cp_civo", "cp_tensordock", "cp_latitude", "cp_acecloud",
        "cp_scaleway", "cp_paperspace", "cp_jarvis", "cp_sesterce",
        "cp_upcloud", "cp_beyond-pl", "cp_koyeb", "cp_ionet",
        "cp_vast", "cp_vultr", "cp_verda", "cp_akamai",
        "cp_packet-ai", "cp_gcore",
    ],
    # Named enterprise GPU cloud peers — used in Slack positioning and the executive
    # benchmark table. Criteria: GPU-first or significant GPU cloud business, meaningful
    # owned capacity (1,000+ GPUs), enterprise SLAs, active and solvent.
    #
    # Excluded from enterprise tier:
    #   - Genesis Cloud (cp_genesis): confirmed in liquidation 2025 — pricing stale/unreliable
    #   - Sesterce (cp_sesterce): broker/aggregator reselling spare capacity, no owned infra
    #   - Denvr Dataworks (cp_denvr-dataworks): only $10.8M raised, ~1,024 H100s — too small
    #   - Commodity GPU rental marketplaces (TensorDock, Vast.ai, RunPod)
    #   - General VPS providers (DigitalOcean, Vultr, UpCloud) — GPU is a side product
    #   - Kubernetes-first clouds (Civo) not competing in raw GPU workloads
    #   - Developer ML platforms (Paperspace/DO) targeting hobbyists, not enterprise
    # IREN: GPU cloud competitor named in sales calls; not yet on ComputePrices.com.
    #   Add to this list once pricing is confirmed.
    "enterprise_gpu_cloud": [
        "nebius", "coreweave", "lambda", "crusoe",
        "cp_hyperstack",       # NexGen Cloud — $1B AI Supercloud, thousands of H100s, UK
        "cp_voltage",          # Voltage Park — 24,000 H100s, $1B Navigation Fund, US
        "cp_gmi-cloud",        # GMI Cloud — $12B sovereign AI initiative, NVIDIA partner, APAC
        "cp_scaleway",         # Scaleway (Iliad Group) — serious European cloud, up to 504-GPU clusters
        "cp_gcore",            # Gcore — expanding European GPU cloud, Luxembourg/Helsinki/Portugal
    ],
    "managed_inference": [
        "cp_deep-infra", "cp_fal-ai", "cp_together-ai",
        "cp_hyperbolic", "cp_theta-edgecloud", "cp_salad",
    ],
}

# Reverse lookup: provider → tier
def provider_tier(provider: str) -> str:
    p = provider.lower()
    for tier, members in PROVIDER_TIERS.items():
        if p in members:
            return tier
    # cp_ prefix but not explicitly mapped → treat as raw_gpu_cloud
    if p.startswith("cp_"):
        return "raw_gpu_cloud"
    return "unknown"

# Alert threshold: only notify on Slack for price changes above this magnitude
# Applied to raw_gpu_cloud + hyperscaler providers only.
ALERT_THRESHOLD_PCT = 3.0   # 3%

# Manual price overrides — used when a provider's pricing page doesn't list public prices.
# These are injected by the fetcher and flagged with consumption_type "on_demand" unless specified.
# Set price_per_gpu_hour_usd; gpu_count defaults to 1.
# Update these when you get quotes from the provider's sales team.
MANUAL_PRICES = {
    # Crusoe prices obtained via sales — update when public pricing appears
    # ("provider", "gpu_model", "consumption_type", "region"): price_per_gpu_hour_usd
    # Example: ("crusoe", "B200", "on_demand", "us-east"): 3.50,
}

# ---------------------------------------------------------------------------
# Nebius committed / reserved pricing — effective April 23rd 2026
# Source: internal Pricing Model AE sheet
# Structure: gpu_model → volume_tier → commitment_months → prepayment_pct → $/GPU/hr
# Prepayment options: "100pct" (all upfront), "50pct" (half upfront), "30pct" (30% upfront)
# Volume tiers: "below_512" (standard, accessible to all) and "above_512" (enterprise)
# ---------------------------------------------------------------------------
NEBIUS_COMMITTED_PRICES = {
    "H100": {
        "below_512": {
            9:  {"100pct": 2.64, "50pct": 2.67, "30pct": 2.69},
            12: {"100pct": 2.40, "50pct": 2.43, "30pct": 2.45},
            18: {"100pct": 2.21, "50pct": 2.23, "30pct": 2.25},
            24: {"100pct": 2.15, "50pct": 2.18, "30pct": 2.21},
        },
        "above_512": {
            9:  {"100pct": 2.54, "50pct": 2.57, "30pct": 2.59},
            12: {"100pct": 2.30, "50pct": 2.33, "30pct": 2.35},
            18: {"100pct": 2.11, "50pct": 2.13, "30pct": 2.15},
            24: {"100pct": 2.05, "50pct": 2.08, "30pct": 2.11},
        },
    },
    "H200": {
        "below_512": {
            9:  {"100pct": 3.19, "50pct": 3.22, "30pct": 3.24},
            12: {"100pct": 2.90, "50pct": 2.92, "30pct": 2.95},
            18: {"100pct": 2.61, "50pct": 2.63, "30pct": 2.65},
            24: {"100pct": 2.50, "50pct": 2.53, "30pct": 2.56},
        },
        "above_512": {
            9:  {"100pct": 3.09, "50pct": 3.12, "30pct": 3.14},
            12: {"100pct": 2.80, "50pct": 2.82, "30pct": 2.85},
            18: {"100pct": 2.51, "50pct": 2.53, "30pct": 2.55},
            24: {"100pct": 2.40, "50pct": 2.43, "30pct": 2.46},
        },
    },
    "B200": {
        "below_512": {
            12: {"100pct": 4.99, "50pct": 5.10, "30pct": 5.16},
            24: {"100pct": 4.65, "50pct": 4.75, "30pct": 4.80},
            36: {"100pct": 4.25, "50pct": 4.35, "30pct": 4.40},
        },
        "above_512": {
            12: {"100pct": 4.89, "50pct": 5.00, "30pct": 5.06},
            24: {"100pct": 4.55, "50pct": 4.65, "30pct": 4.70},
            36: {"100pct": 4.15, "50pct": 4.25, "30pct": 4.30},
        },
    },
    "B300": {
        "below_512": {
            12: {"100pct": 5.39, "50pct": 5.50, "30pct": 5.56},
            24: {"100pct": 5.05, "50pct": 5.15, "30pct": 5.20},
            36: {"100pct": 4.65, "50pct": 4.75, "30pct": 4.80},
        },
        "above_512": {
            12: {"100pct": 5.29, "50pct": 5.40, "30pct": 5.46},
            24: {"100pct": 4.95, "50pct": 5.05, "30pct": 5.10},
            36: {"100pct": 4.55, "50pct": 4.65, "30pct": 4.70},
        },
    },
    "GB300": {
        # Only listed in above-512 tier on the pricing sheet
        "above_512": {
            12: {"100pct": 5.88, "50pct": 5.99, "30pct": 6.10},
            24: {"100pct": 5.62, "50pct": 5.72, "30pct": 5.83},
            36: {"100pct": 5.19, "50pct": 5.41, "30pct": 5.57},
        },
    },
}

# Consumption-type suffix for non-canonical prepayment tiers
NEBIUS_COMMITTED_CT_SUFFIX = {
    "100pct": "",        # canonical — no suffix, used in main comparisons
    "50pct":  "_50pct",
    "30pct":  "_30pct",
}

# Map commitment period (months) → base consumption_type
NEBIUS_COMMITTED_CT_MAP = {
    9:  "committed_9mo",
    12: "committed_1yr",
    18: "committed_18mo",
    24: "committed_2yr",
    36: "committed_3yr",
}

CONFLUENCE_CLOUD_ID = "3213098a-816e-4aeb-8073-44b4d40f3fdc"
CONFLUENCE_SPACE_KEY = "PR"
CONFLUENCE_PAGE_ID = "1831469419"
CONFLUENCE_PAGE_TITLE = "GPU Competitor Pricing — Live Overview"
CONFLUENCE_PAGE_URL = "https://nebius.atlassian.net/wiki/spaces/PR/pages/1831469419/GPU+Competitor+Pricing+Live+Overview"

SLACK_CHANNEL = "#competitor-pricing"
