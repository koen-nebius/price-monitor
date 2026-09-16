SELECT
  li.salesforce_quote_line_item_id AS salesforce_quote_line_item_id,
  li.salesforce_quote_id AS salesforce_quote_id,
  q.salesforce_opportunity_id AS salesforce_opportunity_id,
  li.salesforce_opportunity_line_item_id AS salesforce_opportunity_line_item_id,
  li.gpu AS gpu,
  li.product_name AS product_name,
  li.product_family AS product_family,
  li.qli_type AS qli_type,
  li.payment_type AS payment_type,
  li.quantity AS quantity,
  li.price_gpu_hr AS price_gpu_hr,
  li.rack_priced AS rack_priced,
  li.unit_price AS unit_price,
  li.custom_sales_price AS custom_sales_price,
  li.custom_list_price AS custom_list_price,
  li.discount AS discount_pct,
  li.approval_tier AS line_tier,
  q.approval_tier AS quote_tier,
  q.status AS quote_status,
  q.price_or_discount_approvals_status AS price_approval_status,
  q.capacity_approval_status AS capacity_approval_status,
  li.capacity_request_status AS line_capacity_status,
  toString(li.start_dt) AS start_dt,
  toString(li.end_dt) AS end_dt,
  li.number_of_hours AS number_of_hours,
  li.term_m AS term_m,
  q.has_prepayment AS has_prepayment,
  q.prepayment_amount AS prepayment_amount,
  q.segment AS quote_segment,
  li.data_center AS data_center,
  q.region AS region,
  toString(li.created_utc_dttm) AS line_created,
  toString(li.updated_utc_dttm) AS line_updated,
  toString(q.created_utc_dttm) AS quote_created,
  toString(q.updated_utc_dttm) AS quote_updated,
  toString(q.expiration_dt) AS quote_expiration,
  o.is_closed AS opp_is_closed,
  o.is_won AS opp_is_won,
  o.stage_name AS opp_stage,
  toString(o.close_dt) AS opp_close_dt,
  cr.cr_status AS cr_status,
  toString(cr.cr_approved_at) AS cr_approved_at,
  cr.cr_prepayment_pct AS cr_prepayment_pct,
  cr.cr_sales_price AS cr_sales_price,
  cr.cr_region AS cr_region,
  cr.cr_type_of_consumption AS cr_type_of_consumption
FROM (
  SELECT *,
    multiIf(gpu IN ('GB200', 'GB300') AND raw_price >= 100, raw_price / 72, raw_price) AS price_gpu_hr,
    gpu IN ('GB200', 'GB300') AND raw_price >= 100 AS rack_priced,
    multiIf(start_dt IS NOT NULL AND end_dt IS NOT NULL AND end_dt > start_dt, round(dateDiff('day', start_dt, end_dt) / 30.4, 2),
            number_of_hours > 0, round(number_of_hours / 730, 2),
            CAST(NULL, 'Nullable(Float64)')) AS term_m
  FROM (
    SELECT
      l.salesforce_quote_line_item_id AS salesforce_quote_line_item_id,
      substring(l.salesforce_quote_line_item_id, 1, 15) AS line15,
      l.salesforce_quote_id AS salesforce_quote_id,
      l.salesforce_opportunity_line_item_id AS salesforce_opportunity_line_item_id,
      coalesce(l.doc_product_name, p.product_name) AS product_name,
      multiIf(match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)vera rubin|\\bVR\\b'), 'VR',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)\\bGB300\\b'), 'GB300',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)\\bGB200\\b'), 'GB200',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)\\bB300\\b'), 'B300',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)\\bB200\\b'), 'B200',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)\\bH200\\b'), 'H200',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)\\bH100\\b'), 'H100',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)RTX'), 'RTX6000',
              match(coalesce(l.doc_product_name, p.product_name, ''), '(?i)L40S'), 'L40S',
              'OTHER') AS gpu,
      if(coalesce(l.product_family, '') = 'Token Factory'
         OR positionCaseInsensitive(coalesce(l.doc_product_name, p.product_name, ''), 'dedicated endpoint') > 0
         OR coalesce(p.family, '') = 'Token Factory'
         OR match(coalesce(p.product_code, ''), '(?i)[- ]TF$'), 'Token Factory', 'AI Cloud') AS product_family,
      l.qli_type AS qli_type,
      l.payment_type AS payment_type,
      l.quantity AS quantity,
      coalesce(l.custom_sales_price, l.unit_price) AS raw_price,
      l.unit_price AS unit_price,
      l.custom_sales_price AS custom_sales_price,
      l.custom_list_price AS custom_list_price,
      l.discount AS discount,
      l.approval_tier AS approval_tier,
      l.capacity_request_status AS capacity_request_status,
      l.start_dt AS start_dt,
      l.end_dt AS end_dt,
      l.number_of_hours AS number_of_hours,
      l.data_center AS data_center,
      l.created_utc_dttm AS created_utc_dttm,
      l.updated_utc_dttm AS updated_utc_dttm
    FROM (
      SELECT salesforce_quote_line_item_id, salesforce_quote_id, salesforce_opportunity_line_item_id, salesforce_product_id,
             doc_product_name, product_family, qli_type, payment_type, quantity, unit_price, custom_sales_price, custom_list_price,
             discount, approval_tier, capacity_request_status, start_dt, end_dt, number_of_hours, data_center,
             created_utc_dttm, updated_utc_dttm
      FROM `//home/dwh/nemax-prod/data/ods/salesforce/quote_line_item`
      WHERE NOT coalesce(is_deleted, false) AND NOT coalesce(fivetran_deleted, false)
        AND unit_of_measure IN ('gpu*hour', 'gpu-unit*hour')
    ) AS l
    LEFT JOIN (
      SELECT salesforce_product_id, product_name, product_code, family
      FROM `//home/dwh/nemax-prod/data/ods/salesforce/product`
    ) AS p ON l.salesforce_product_id = p.salesforce_product_id
  )
) AS li
JOIN (
  SELECT salesforce_quote_id, salesforce_opportunity_id, status, price_or_discount_approvals_status, capacity_approval_status,
         approval_tier, has_prepayment, prepayment_amount, segment, region, expiration_dt, created_utc_dttm, updated_utc_dttm
  FROM `//home/dwh/nemax-prod/data/ods/salesforce/quote`
  WHERE NOT coalesce(is_deleted, false) AND NOT coalesce(fivetran_deleted, false)
) AS q ON li.salesforce_quote_id = q.salesforce_quote_id
LEFT JOIN (
  SELECT salesforce_opportunity_id, is_closed, is_won, stage_name, close_dt
  FROM `//home/dwh/nemax-prod/data/ods/salesforce/opportunity`
) AS o ON q.salesforce_opportunity_id = o.salesforce_opportunity_id
LEFT JOIN (
  SELECT substring(crm_line_item_id, 1, 15) AS line15,
         argMax(status, coalesce(updated_utc_dttm, created_utc_dttm)) AS cr_status,
         argMax(approved_utc_dttm, coalesce(updated_utc_dttm, created_utc_dttm)) AS cr_approved_at,
         argMax(prepayment_percent, coalesce(updated_utc_dttm, created_utc_dttm)) AS cr_prepayment_pct,
         argMax(sales_price, coalesce(updated_utc_dttm, created_utc_dttm)) AS cr_sales_price,
         argMax(region, coalesce(updated_utc_dttm, created_utc_dttm)) AS cr_region,
         argMax(type_of_consumption, coalesce(updated_utc_dttm, created_utc_dttm)) AS cr_type_of_consumption
  FROM `//home/dwh/nemax-prod/data/ods/capacity/capacity_requests`
  WHERE crm_line_item_id LIKE '0QL%'
  GROUP BY line15
) AS cr ON li.line15 = cr.line15
WHERE li.price_gpu_hr >= 0.3 AND li.price_gpu_hr <= 50
ORDER BY q.updated_utc_dttm DESC, li.salesforce_quote_line_item_id