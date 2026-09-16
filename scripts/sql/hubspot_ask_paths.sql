SELECT
  crm_line_item_id, crm_deal_id, meeting_dt, gpu, unit_raw AS unit, gpus_per_rack,
  unit_price_raw, unit_price_calculated,
  multiIf(unit_price_raw IS NULL, CAST(NULL, 'Nullable(Float64)'),
          is_rack AND gpus_per_rack IS NOT NULL, unit_price_raw / gpus_per_rack,
          is_rack, CAST(NULL, 'Nullable(Float64)'),
          unit_price_raw) AS price_gpu_hr,
  multiIf(unit_price_calculated IS NULL OR unit_price_calculated <= 0, price_gpu_hr,
          is_rack AND gpus_per_rack IS NOT NULL, unit_price_calculated / gpus_per_rack,
          is_rack, CAST(NULL, 'Nullable(Float64)'),
          unit_price_calculated) AS price_gpu_hr_effective,
  multiIf(unit_price_calculated > 0, 'unit_price_calculated',
          unit_price_raw IS NOT NULL, 'unit_price',
          CAST(NULL, 'Nullable(String)')) AS price_source,
  multiIf(resource_quantity > 0 AND is_rack AND gpus_per_rack IS NOT NULL, resource_quantity * gpus_per_rack,
          resource_quantity > 0 AND NOT is_rack, resource_quantity,
          resource_quantity > 0, CAST(NULL, 'Nullable(Float64)'),
          product_quantity > 0 AND window_hours > 0 AND is_rack AND gpus_per_rack IS NOT NULL, product_quantity / window_hours * gpus_per_rack,
          product_quantity > 0 AND window_hours > 0 AND NOT is_rack, product_quantity / window_hours,
          CAST(NULL, 'Nullable(Float64)')) AS gpu_qty,
  multiIf(resource_quantity > 0, 'resource_quantity',
          product_quantity > 0 AND window_hours > 0, 'product_quantity_over_hours',
          CAST(NULL, 'Nullable(String)')) AS gpu_qty_basis,
  resource_quantity AS resource_quantity_raw, product_quantity AS product_quantity_raw,
  consumption_start, consumption_end,
  if(consumption_start IS NOT NULL AND consumption_end IS NOT NULL,
     round(dateDiff('day', consumption_start, consumption_end) / 30.4, 2), CAST(NULL, 'Nullable(Float64)')) AS term_m,
  consumption_type_slug, payment_type_slug, deal_stage_name, line_item_status_slug, capacity_approval_status,
  lost_category, data_center_location_slug
FROM (
  SELECT *,
    lower(coalesce(unit_raw, '')) IN ('rack', 'racks') AS is_rack,
    multiIf(match(pname, 'GPU-([0-9]+)'), toInt32OrNull(extract(pname, 'GPU-([0-9]+)')),
            gpu IN ('GB200', 'GB300', 'VR') OR match(pname, '(?i)NVL ?72|vera rubin'), toInt32(72),
            CAST(NULL, 'Nullable(Int32)')) AS gpus_per_rack,
    max(unit_price_raw) OVER (PARTITION BY crm_line_item_id) AS li_max_price
  FROM (
    SELECT *,
      coalesce(multiIf(
        gpu_canon = 'H100 SXM', 'H100',
        gpu_canon IN ('L40S Intel', 'L40S AMD', 'L40S'), 'L40S',
        gpu_canon IN ('H100', 'H200', 'B200', 'B300', 'GB200', 'GB300', 'VR', 'RTX6000'), gpu_canon,
        match(pname, '(?i)vera rubin|\\bVR\\b|\\bVR200\\b'), 'VR',
        match(pname, '(?i)\\bGB300\\b'), 'GB300',
        match(pname, '(?i)\\bGB200\\b'), 'GB200',
        match(pname, '(?i)\\bB300\\b'), 'B300',
        match(pname, '(?i)\\bB200\\b'), 'B200',
        match(pname, '(?i)\\bH200\\b'), 'H200',
        match(pname, '(?i)\\bH100\\b'), 'H100',
        match(pname, '(?i)RTX ?6000'), 'RTX6000',
        match(pname, '(?i)\\bL40S\\b'), 'L40S',
        'OTHER'), 'OTHER') AS gpu
    FROM (
      SELECT crm_line_item_id, meeting_utc_dttm, toDate(meeting_utc_dttm) AS meeting_dt,
        any(crm_deal_id) AS crm_deal_id, any(resource_type_name) AS rt,
        coalesce(any(gpu_model_canonical), '') AS gpu_canon, coalesce(any(product_name), '') AS pname, any(unit) AS unit_raw,
        any(unit_price) AS unit_price_raw, any(unit_price_calculated) AS unit_price_calculated,
        any(resource_quantity) AS resource_quantity, any(product_quantity) AS product_quantity,
        toDate(any(consumption_start_utc_dttm)) AS consumption_start, toDate(any(consumption_end_utc_dttm)) AS consumption_end,
        dateDiff('hour', any(consumption_start_utc_dttm), any(consumption_end_utc_dttm)) AS window_hours,
        any(consumption_type_slug) AS consumption_type_slug, any(payment_type_slug) AS payment_type_slug,
        any(deal_stage_name) AS deal_stage_name, any(line_item_status_slug) AS line_item_status_slug,
        any(capacity_approval_status) AS capacity_approval_status, any(closed_lost_reason_desc) AS lost_category,
        any(data_center_location_slug) AS data_center_location_slug
      FROM `//home/dwh/nemax-prod/data/cdm/crm/deal_review_line_items_daily`
      WHERE meeting_utc_dttm >= toDateTime('{since} 00:00:00')
        AND (resource_type_name = 'GPU' OR resource_type_name IS NULL)
        AND coalesce(gpu_model_canonical, '') != 'No GPU'
        AND lower(coalesce(unit, 'gpu')) IN ('gpu', 'gpus', 'rack', 'racks')
      GROUP BY crm_line_item_id, meeting_utc_dttm
    )
  )
)
WHERE (rt = 'GPU' OR gpu != 'OTHER') AND li_max_price > 0
  AND cityHash64(crm_line_item_id) % {shards} = {shard}
ORDER BY crm_line_item_id, meeting_dt