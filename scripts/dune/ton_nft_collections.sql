-- TON NFT collections by sales in the last 24 hours, with the 24 hours before for comparison.
-- Fragment's sales are here too: Telegram usernames, anonymous numbers and gifts are TON collections.
--
-- analyzer_dune.json entry:
--   {"id": 0, "title": "TON collections selling now", "assets": true, "protocols": ["fragment", "getgems"],
--    "chains": ["TON"], "refresh_h": 6, "explorer": "https://tonviewer.com/"}
--
-- Assumes sale_price is in nanoTON (1e9 to a TON), as TON amounts are everywhere else on Dune; check one known sale.
with sales as (
    select collection_address, sale_price / 1e9 as price_ton, owner_address as buyer, block_time
    from ton.nft_events
    where type = 'sale'
      and block_date >= current_date - interval '3' day
      and block_time >= now() - interval '48' hour
      and sale_price > 0
      and collection_address is not null
)
select
    collection_address,
    count_if(block_time >= now() - interval '24' hour)                                   as sales_24h,
    count(distinct case when block_time >= now() - interval '24' hour then buyer end)    as buyers_24h,
    round(sum(case when block_time >= now() - interval '24' hour then price_ton end), 1) as volume_ton,
    round(sum(case when block_time <  now() - interval '24' hour then price_ton end), 1) as prev_day_ton,
    round(100.0 * (sum(case when block_time >= now() - interval '24' hour then price_ton end)
          / nullif(sum(case when block_time < now() - interval '24' hour then price_ton end), 0) - 1), 1) as volume_change,
    round(max(case when block_time >= now() - interval '24' hour then price_ton end), 1) as top_sale_ton
from sales
group by 1
having sum(case when block_time >= now() - interval '24' hour then price_ton end) > 0
order by volume_ton desc
limit 40
