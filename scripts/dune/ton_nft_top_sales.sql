-- The biggest single TON NFT sales of the last 24 hours: the exact items changing hands.
--
-- analyzer_dune.json entry:
--   {"id": 0, "title": "Biggest TON NFT sales, 24 h", "assets": true, "protocols": ["fragment", "getgems"],
--    "chains": ["TON"], "refresh_h": 6, "explorer": "https://tonviewer.com/"}
--
-- Assumes sale_price is in nanoTON (1e9 to a TON); check one known sale.
select
    nft_item_address        as nft_item,
    collection_address,
    round(sale_price / 1e9, 1) as price_ton,
    sale_type,
    owner_address           as buyer,
    prev_owner              as seller,
    date_format(block_time, '%Y-%m-%d %H:%i') as sold_at
from ton.nft_events
where type = 'sale'
  and block_date >= current_date - interval '2' day
  and block_time >= now() - interval '24' hour
  and sale_price > 0
order by sale_price desc
limit 40
