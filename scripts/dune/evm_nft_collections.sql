-- NFT collections by sales in the last 24 hours on Ethereum, Base and the other EVM chains Dune follows, by venue.
--
-- analyzer_dune.json entry (one entry serves every venue it lists):
--   {"id": 0, "title": "NFT collections selling now", "assets": true,
--    "protocols": ["opensea", "blur", "magic-eden", "opensea-seaport"], "chains": ["Ethereum", "Base"], "refresh_h": 6,
--    "explorer": "https://etherscan.io/address/"}
select
    coalesce(collection, 'unnamed')                         as collection,
    blockchain,
    project                                                 as venue,
    nft_contract_address                                    as contract_address,
    count(*)                                                as sales_24h,
    count(distinct buyer)                                   as buyers_24h,
    round(sum(amount_usd))                                  as volume_usd,
    round(max(amount_usd))                                  as top_sale_usd
from nft.trades
where block_time >= now() - interval '24' hour
  and amount_usd > 0
group by 1, 2, 3, 4
order by volume_usd desc
limit 40
