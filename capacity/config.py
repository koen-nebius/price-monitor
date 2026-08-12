"""
Capacity monitor configuration.

Provider keys deliberately match the price monitor's config.PROVIDER_TIERS
keys so the two monitors can be joined on (provider, gpu_model) later.
"""

# GPU models tracked — same set as the price monitor, plus VR (Vera Rubin)
# as a forward tripwire: fetchers emit it the day a provider lists it.
CAPACITY_GPUS = ["H100", "H200", "B200", "B300", "GB200", "GB300", "L40S", "RTX6000", "VR"]

# Display order for provider columns in the matrix (live-stock peers first,
# then footprint-only providers; the renderer skips providers with no records).
PROVIDER_ORDER = [
    "nebius", "coreweave", "lambda", "crusoe", "hyperstack", "verda",
    "scaleway", "voltage_park", "gmi", "together", "runpod", "vast",
    "sfcompute", "aws", "gcp", "azure", "oracle",
]

# Human labels for providers in exec-facing output
PROVIDER_LABELS = {
    "nebius": "Nebius", "coreweave": "CoreWeave", "lambda": "Lambda",
    "crusoe": "Crusoe", "hyperstack": "Hyperstack", "verda": "Verda",
    "scaleway": "Scaleway", "voltage_park": "Voltage Park", "gmi": "GMI Cloud",
    "together": "Together AI", "runpod": "RunPod", "vast": "Vast.ai",
    "sfcompute": "SF Compute", "aws": "AWS", "gcp": "GCP",
    "azure": "Azure", "oracle": "Oracle",
}

# Providers whose signal is a real-time capacity/stock readout (vs a static
# offering list). Used to weight the per-GPU market verdict: only live-stock
# signals count toward tight/loose; offering lists provide coverage context.
# gmi = provider-declared badges (self-reported but live); crusoe/nebius stay
# footprint-only until their live endpoints are activated (see fetcher docs).
LIVE_STOCK_PROVIDERS = {
    "lambda", "hyperstack", "verda", "scaleway", "runpod", "vast",
    "sfcompute", "voltage_park", "gmi",
    "aws",   # spot advisor now; Capacity Blocks lead time once IAM extended
}

# Fetcher registry — provider key; module of the same name under
# capacity/fetchers/ unless mapped in main.FETCHER_MODULES.
# Keyless & working today:
PROVIDERS = [
    "lambda",            # LAMBDA_API_KEY (in repo secrets) — live per-region stock
    "coreweave",         # docs matrix — footprint per AZ
    "gcp_zones",         # docs page — offering footprint per zone (emits provider=gcp)
    "azure_regions",     # retail prices API — offering footprint (emits provider=azure)
    "aws_spot_advisor",  # public S3 JSON — spot pools + interruption pressure (emits provider=aws)
    "aws_capacity_blocks",  # boto3; activates when ec2:DescribeCapacityBlockOfferings IAM lands
    "crusoe",            # docs matrix — footprint per zone
    "runpod",            # public GraphQL — live stock labels + per-DC availability
    "vast",              # public marketplace search — live offer depth
    "scaleway",          # PUBLIC availability API — live ternary per zone
    "voltage_park",      # public locations API — live GPU counts
    "gmi",               # pricing-page badges — provider-declared state
    "sfcompute",         # homepage ticker (keyless) + availability API (SFCOMPUTE_TOKEN)
    "shadeform",         # keyless aggregator — live booleans for 19 clouds (gap-filler)
    "nebius",            # outside-in footprint (docs + prices page)
    "hyperstack",        # activates when HYPERSTACK_API_KEY (free account) lands
    "together",          # activates when TOGETHER_API_KEY (free account) lands
    "verda",             # activates when VERDA_CLIENT_ID/SECRET (free account) land
]

# Consumption-type display labels
CT_LABELS = {
    "on_demand": "On-demand",
    "spot": "Spot",
    "reserved_short": "Short-term reserved",
    "committed": "Committed",
}

CONFLUENCE_SPACE_KEY = "PR"
CONFLUENCE_PAGE_TITLE = "GPU Competitor Capacity — Live Overview"
# Page id/url are written by capacity/bootstrap_confluence.py on first GHA run
# into capacity/store/confluence_page.json (committed). Render reads it there.
CONFLUENCE_CLOUD_ID = "3213098a-816e-4aeb-8073-44b4d40f3fdc"
CONFLUENCE_BASE_URL = "https://nebius.atlassian.net/wiki"
# Parent: same space as the pricing page (1831469419) so the two live together.
CONFLUENCE_PARENT_PAGE_ID = "1831469419"

SLACK_CHANNEL = "#competitor-capacity"
