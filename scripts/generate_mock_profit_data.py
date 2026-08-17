#!/usr/bin/env python3
"""Generate mock profit-report fixtures (API JSON pages + LingXing-style XLSX).

Structure mirrors real samples but uses synthetic IDs, stores, and amounts only.
Default catalog: phone cases & mobile accessories (fictional brand SKUs).
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import secrets
import string
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from openpyxl import Workbook

# XLSX layout (matches import_lingxing_profit_xlsx.COLUMN_MAP labels)
XLSX_GROUP_HEADER = "基础信息"
XLSX_HEADERS = [
    "店铺",
    "国家",
    "币种",
    "下单时间",
    "付款时间",
    "发货时间",
    "结算时间",
    "转账时间",
    "延迟时间",
    "订单号",
    "Settlement Id",
    "结算编号",
    "MSKU",
    "FNSKU",
    "ASIN",
    "父asin",
    "品名",
    "SKU",
    "账单类型",
    "费用类型",
    "订单类型",
    "描述",
    "结算状态",
    "转账状态",
    "交易状态",
    "数量",
    "Listing负责人",
    "开发负责人",
    "销售额",
    "销售税",
    "买家运费",
    "买家运费税",
    "礼品包装",
    "礼品包装税",
    "积分",
    "促销折扣",
    "促销折扣税",
    "代扣代缴增值税",
    "低价值商品税",
    "市场预扣税",
    "TCS_CGST",
    "TCS_SGST",
    "TCS_IGST",
    "平台费",
    "FBA费",
    "其他交易费",
    "其他",
    "隐藏税",
    "亚马逊结算小计",
    "采购成本",
    "头程费用",
    "其他成本",
    "站外推广费",
    "结算订单毛利润",
    "结算订单毛利润率",
    "促销编码",
]

MOCK_STORES = [
    ("CoverNest-US", "美国", "USD", "ATVPDKIKX0DER", "Amazon.com"),
    ("ShieldPro-CA", "加拿大", "CAD", "A2EUQ1WTGCTBG2", "Amazon.ca"),
    ("CaseVault-UK", "英国", "GBP", "A1F83G8C2ARO7P", "Amazon.co.uk"),
    ("MobilGear-DE", "德国", "EUR", "A1PA6795UKMFR9", "Amazon.de"),
]

MOCK_OWNERS = [
    ("陈_mock", "周_mock"),
    ("刘_mock", "赵_mock"),
    ("王_mock", "孙_mock"),
    ("李_mock", "吴_mock"),
]


@dataclass(frozen=True)
class MockProduct:
    """Phone-case / mobile-accessory SKU with realistic price & FBA bands."""

    local_name: str
    msku: str
    asin: str
    listing_title: str
    unit_price: tuple[float, float]
    fba_fee: tuple[float, float]
    qty_weights: tuple[int, ...] = (1, 1, 1, 2)


# Fictional SKUs — structure like real listings, no tie to any live ASIN/brand.
MOCK_PRODUCTS: list[MockProduct] = [
    MockProduct(
        "iPhone 15 Pro 磁吸防摔壳",
        "CN-IP15P-MC-BK",
        "B0CN7A1K9M2",
        "CoverNest Magnetic Case for iPhone 15 Pro, Shockproof Matte Black, MagSafe Compatible",
        (11.99, 16.99),
        (3.25, 4.15),
    ),
    MockProduct(
        "Samsung S24 透明软壳",
        "CN-S24-CLR-TP",
        "B0CN7B2L8N3",
        "ShieldPro Clear Case for Samsung Galaxy S24, TPU Anti-Yellowing Slim Cover",
        (8.99, 13.99),
        (2.95, 3.85),
    ),
    MockProduct(
        "钢化膜 2片装",
        "CN-TG-2PK-UNIV",
        "B0CN7C3M7P4",
        "CaseVault Tempered Glass Screen Protector 2-Pack, 9H Hardness, Bubble-Free Kit",
        (6.99, 10.99),
        (2.65, 3.35),
    ),
    MockProduct(
        "USB-C 快充线 6ft",
        "CN-USBC-6FT-60W",
        "B0CN7D4N6Q5",
        "MobilGear USB C to USB C Cable 6ft, 60W PD Fast Charging for Phone & Tablet",
        (7.99, 12.99),
        (2.80, 3.60),
    ),
    MockProduct(
        "MagSafe 车载手机支架",
        "CN-MAG-MNT-AIR",
        "B0CN7E5P5R6",
        "CoverNest MagSafe Car Mount, Air Vent Phone Holder for iPhone 14/15 Series",
        (15.99, 24.99),
        (3.85, 5.10),
    ),
    MockProduct(
        "镜头保护圈 3件套",
        "CN-LENS-3PK-IP",
        "B0CN7F6Q4S7",
        "ShieldPro Camera Lens Protector 3-Pack, Metal Ring HD Glass for iPhone Pro Models",
        (9.99, 14.99),
        (2.90, 3.70),
    ),
    MockProduct(
        "手机挂绳 可调节",
        "CN-STRAP-ADJ",
        "B0CN7G7R3T8",
        "CaseVault Phone Lanyard Strap, Adjustable Wrist & Neck Cord for Case with Tab",
        (5.99, 9.99),
        (2.55, 3.20),
    ),
    MockProduct(
        "AirPods Pro 硅胶保护套",
        "CN-APP-SIL-BK",
        "B0CN7H8S2U9",
        "MobilGear Silicone Case for AirPods Pro 2, Shockproof Cover with Carabiner Black",
        (7.49, 11.99),
        (2.70, 3.45),
    ),
    MockProduct(
        "20W PD 充电头",
        "CN-PD20W-WHT",
        "B0CN7J9T1V0",
        "ShieldPro 20W USB C Wall Charger, PD Fast Charge Block for iPhone & Android",
        (9.99, 15.99),
        (3.10, 4.00),
    ),
    MockProduct(
        "手机壳+膜 组合套装",
        "CN-BND-CASE-TG",
        "B0CN7K0U0W1",
        "CoverNest Phone Case and Screen Protector Bundle, Slim Cover + Glass 2-Pack",
        (14.99, 21.99),
        (3.60, 4.80),
        qty_weights=(1, 1, 2),
    ),
]

TRANSACTION_TYPES = ["Shipment", "Refund", "ProductAdsPayment", "ServiceFee"]
EVENT_SOURCES = ["Principal", "Shipping", "Promotion", "Tax", "Fee"]
FULFILLMENT = ["AFN", "MFN"]
STATUSES = ("Disbursed", "Pending")
TRANSFER_STATUSES = ("Succeeded", "Processing")


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _fake_token(page: int) -> str:
    raw = f"MOCKTOKEN-{page:03d}-{secrets.token_hex(24)}".encode()
    return base64.b64encode(raw).decode("ascii")


def _fake_tx_id(rng: random.Random) -> str:
    alphabet = string.ascii_letters + string.digits + "_-"
    return "".join(rng.choice(alphabet) for _ in range(43))


def _order_id(seq: int) -> str:
    part_a = 900 + (seq // 1_000_000) % 100
    part_b = seq % 1_000_0000
    part_c = seq % 1_000_000
    return f"{part_a:03d}-{part_b:07d}-{part_c:06d}"


def _money(rng: random.Random, low: float, high: float) -> float:
    return round(rng.uniform(low, high), 2)


def _charm_price(rng: random.Random, low: float, high: float) -> float:
    """Pick a .99-style unit price common on Amazon accessory listings."""
    anchors = [p for p in (5.99, 6.99, 7.99, 8.99, 9.99, 10.99, 11.99, 12.99, 14.99, 15.99, 17.99, 19.99, 21.99, 24.99) if low <= p <= high]
    if anchors and rng.random() < 0.82:
        return rng.choice(anchors)
    return _money(rng, low, high)


def _pick_product(rng: random.Random) -> MockProduct:
    return MOCK_PRODUCTS[rng.randint(0, len(MOCK_PRODUCTS) - 1)]


def _pick_qty(rng: random.Random, product: MockProduct) -> int:
    return rng.choice(product.qty_weights)


def _accessory_economics(
    rng: random.Random,
    product: MockProduct,
    qty: int,
) -> dict[str, float]:
    """Accessory-like fee stack: ~15% referral, small-standard FBA, modest promo."""
    unit_price = _charm_price(rng, *product.unit_price)
    principal = round(unit_price * qty, 2)
    shipping = _money(rng, 0.0, 4.99) if rng.random() < 0.18 else 0.0
    promo_rate = rng.uniform(0, 0.12) if rng.random() < 0.35 else 0.0
    promo = -round(principal * promo_rate, 2)
    referral = -round(principal * 0.15, 2)
    fba_unit = _money(rng, *product.fba_fee)
    fba = -round(fba_unit * qty, 2)
    other_fees = _money(rng, -0.75, 0.45)
    sales_total = round(principal + shipping + promo, 2)
    expense_total = round(referral + fba + other_fees, 2)
    net = round(sales_total + expense_total, 2)
    purchase = round(principal * rng.uniform(0.26, 0.38), 2)
    logistics = round(principal * rng.uniform(0.05, 0.10), 2)
    other_cost = _money(rng, 0.0, 1.20)
    promo_fee = _money(rng, 0.0, 2.50) if rng.random() < 0.08 else 0.0
    gross = round(net - purchase - logistics - other_cost - promo_fee, 2)
    rate = round(gross / principal * 100, 2) if principal else 0.0
    return {
        "unit_price": unit_price,
        "principal": principal,
        "shipping": shipping,
        "promo": promo,
        "referral": referral,
        "fba": fba,
        "other_fees": other_fees,
        "sales_total": sales_total,
        "expense_total": expense_total,
        "net": net,
        "purchase": purchase,
        "logistics": logistics,
        "other_cost": other_cost,
        "promo_fee": promo_fee,
        "gross": gross,
        "rate": rate,
    }


def _amount(value: float, currency: str) -> dict[str, object]:
    return {"currencyAmount": value, "currencyCode": currency}


def _breakdown_leaf(rng: random.Random, btype: str, amount: float, currency: str) -> dict:
    return {
        "breakdownType": btype,
        "breakdownAmount": _amount(amount, currency),
        "breakdowns": None,
    }


def _build_api_transaction(
    rng: random.Random,
    *,
    seq: int,
    posted: datetime,
    store_idx: int,
) -> dict:
    store, country, currency, marketplace_id, marketplace_name = MOCK_STORES[store_idx % len(MOCK_STORES)]
    product = _pick_product(rng)
    qty = _pick_qty(rng, product)
    econ = _accessory_economics(rng, product, qty)
    principal = econ["principal"]
    shipping = econ["shipping"]
    promo = econ["promo"]
    fee = econ["referral"]
    fba = econ["fba"]
    sales_total = econ["sales_total"]
    expense_total = econ["expense_total"]
    net = econ["net"]

    tx_type = rng.choices(TRANSACTION_TYPES, weights=[88, 8, 2, 2])[0]
    order_id = _order_id(seq)
    settlement_id = str(26_000_000_000 + seq)
    shipment_id = str(480_000_000_000_000 + seq)
    group_id = _fake_tx_id(rng)
    deferred_id = _fake_tx_id(rng)

    item = {
        "description": product.listing_title,
        "totalAmount": _amount(round(principal + shipping + promo + fee + fba, 2), currency),
        "relatedIdentifiers": [
            {
                "itemRelatedIdentifierName": "ORDER_ADJUSTMENT_ITEM_ID",
                "itemRelatedIdentifierValue": str(160_000_000_000_000 + seq),
            }
        ],
        "breakdowns": [
            {
                "breakdownType": "ProductCharges",
                "breakdownAmount": _amount(principal, currency),
                "breakdowns": [
                    _breakdown_leaf(rng, "OurPricePrincipal", principal, currency),
                ],
            },
            {
                "breakdownType": "AmazonFees",
                "breakdownAmount": _amount(fee + fba, currency),
                "breakdowns": [
                    _breakdown_leaf(rng, "Commission", fee, currency),
                    _breakdown_leaf(rng, "FBAPerUnitFulfillmentFee", fba, currency),
                ],
            },
        ],
        "contexts": [
            {
                "asin": product.asin,
                "quantityShipped": qty,
                "sku": product.msku,
                "fulfillmentNetwork": rng.choices(FULFILLMENT, weights=[92, 8])[0],
                "contextType": "ProductContext",
            }
        ],
    }

    return {
        "sellingPartnerMetadata": {
            "sellingPartnerId": f"AMOCK{store_idx:09d}",
            "marketplaceId": marketplace_id,
            "accountType": "Standard Orders",
        },
        "transactionType": tx_type,
        "transactionId": _fake_tx_id(rng),
        "transactionStatus": "RELEASED",
        "relatedIdentifiers": [
            {"relatedIdentifierName": "FINANCIAL_EVENT_GROUP_ID", "relatedIdentifierValue": group_id},
            {"relatedIdentifierName": "SHIPMENT_ID", "relatedIdentifierValue": shipment_id},
            {"relatedIdentifierName": "SETTLEMENT_ID", "relatedIdentifierValue": settlement_id},
            {"relatedIdentifierName": "ORDER_ID", "relatedIdentifierValue": order_id},
            {"relatedIdentifierName": "DEFERRED_TRANSACTION_ID", "relatedIdentifierValue": deferred_id},
        ],
        "totalAmount": _amount(net, currency),
        "description": "Order Payment" if tx_type == "Shipment" else tx_type,
        "postedDate": posted.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "marketplaceDetails": {
            "marketplaceId": marketplace_id,
            "marketplaceName": marketplace_name,
        },
        "items": [item] if tx_type == "Shipment" else [],
        "breakdowns": [
            {
                "breakdownType": "Sales",
                "breakdownAmount": _amount(sales_total, currency),
                "breakdowns": [
                    _breakdown_leaf(rng, "ProductCharges", sales_total, currency),
                ],
            },
            {
                "breakdownType": "Expenses",
                "breakdownAmount": _amount(expense_total, currency),
                "breakdowns": [
                    _breakdown_leaf(rng, "AmazonFees", expense_total, currency),
                ],
            },
        ],
        "contexts": [{"contextType": "AddressContext"}],
    }


def _random_dt_in_month(rng: random.Random, year: int, month: int) -> datetime:
    days = monthrange(year, month)[1]
    day = rng.randint(1, days)
    hour = rng.randint(0, 23)
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    return datetime(year, month, day, hour, minute, second)


def generate_api_pages(
    *,
    output_dir: Path,
    year: int,
    months: list[int],
    pages_per_month: int,
    records_per_page: int,
    seed: int,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {}
    global_seq = 1
    for month in months:
        month_total = 0
        for page in range(1, pages_per_month + 1):
            rng = _rng(seed + year * 100 + month * 10 + page)
            transactions = []
            for _ in range(records_per_page):
                posted = _random_dt_in_month(rng, year, month)
                store_idx = rng.randint(0, len(MOCK_STORES) - 1)
                transactions.append(
                    _build_api_transaction(
                        rng,
                        seq=global_seq,
                        posted=posted,
                        store_idx=store_idx,
                    )
                )
                global_seq += 1
            payload = {
                "transactions": transactions,
                "nextToken": _fake_token(page) if page < pages_per_month else None,
            }
            filename = f"{year}-{month:02d}_page_{page:03d}.json"
            path = output_dir / filename
            path.write_text(
                json.dumps({"statusCode": 200, "payload": payload}, ensure_ascii=False),
                encoding="utf-8",
            )
            month_total += len(transactions)
        stats[f"{year}-{month:02d}"] = month_total
    return stats


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _build_xlsx_row(rng: random.Random, *, seq: int, posted: datetime) -> list[object]:
    store, country, currency, _, _ = MOCK_STORES[rng.randint(0, len(MOCK_STORES) - 1)]
    product = _pick_product(rng)
    qty = _pick_qty(rng, product)
    econ = _accessory_economics(rng, product, qty)
    listing_owner, dev_owner = MOCK_OWNERS[rng.randint(0, len(MOCK_OWNERS) - 1)]
    order_dt = posted - timedelta(days=rng.randint(3, 12), hours=rng.randint(0, 8))
    pay_dt = order_dt + timedelta(hours=rng.randint(1, 36))
    ship_dt = pay_dt + timedelta(minutes=rng.randint(0, 120))
    transfer_dt = posted + timedelta(days=rng.randint(7, 21), hours=rng.randint(0, 12))

    product_sales = _quantize(Decimal(str(econ["principal"])))
    shipping = _quantize(Decimal(str(econ["shipping"])))
    promo = _quantize(Decimal(str(econ["promo"])))
    selling_fees = _quantize(Decimal(str(econ["referral"])))
    fba_fees = _quantize(Decimal(str(econ["fba"])))
    other_fees = _quantize(Decimal(str(econ["other_fees"])))
    settlement_total = _quantize(Decimal(str(econ["net"])))
    purchase = _quantize(Decimal(str(econ["purchase"])))
    logistics = _quantize(Decimal(str(econ["logistics"])))
    other_cost = _quantize(Decimal(str(econ["other_cost"])))
    promo_fee = _quantize(Decimal(str(econ["promo_fee"])))
    gross = _quantize(Decimal(str(econ["gross"])))
    rate = _quantize(Decimal(str(econ["rate"])))

    fmt = "%Y-%m-%d %H:%M:%S"
    return [
        store,
        country,
        currency,
        order_dt.strftime(fmt),
        pay_dt.strftime(fmt),
        ship_dt.strftime(fmt),
        posted.strftime(fmt),
        transfer_dt.strftime(fmt),
        None,
        _order_id(seq),
        str(26_100_000_000 + seq),
        f"MOCK{seq:012X}"[:15],
        product.msku,
        f"X{product.asin[-9:]}",
        product.asin,
        product.asin,
        product.local_name,
        product.msku.replace("CN-", "SKU-"),
        "Standard Orders",
        rng.choice(EVENT_SOURCES),
        rng.choices(FULFILLMENT, weights=[92, 8])[0],
        product.listing_title[:120],
        rng.choice(STATUSES),
        rng.choice(TRANSFER_STATUSES),
        "Completed",
        qty,
        listing_owner,
        dev_owner,
        float(product_sales),
        float(_quantize(product_sales * Decimal("0.07"))),
        float(shipping),
        0.0,
        0.0,
        0.0,
        float(_quantize(-Decimal("0.5"))),
        float(promo),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        float(selling_fees),
        float(fba_fees),
        float(other_fees),
        0.0,
        0.0,
        float(settlement_total),
        float(purchase),
        float(logistics),
        float(other_cost),
        float(promo_fee),
        float(gross),
        float(rate),
        f"PROMO-MOCK-{seq % 10000:04d}" if rng.random() < 0.2 else None,
    ]


def generate_xlsx_for_month(
    *,
    output_path: Path,
    year: int,
    month: int,
    row_count: int,
    seed: int,
    seq_base: int,
) -> int:
    """Write one monthly XLSX; all 结算时间 fall within the given month."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append([XLSX_GROUP_HEADER] + [None] * (len(XLSX_HEADERS) - 1))
    ws.append(XLSX_HEADERS)

    for offset in range(row_count):
        seq = seq_base + offset
        rng = _rng(seed + month * 10_000 + offset)
        posted = _random_dt_in_month(rng, year, month)
        ws.append(_build_xlsx_row(rng, seq=seq, posted=posted))

    wb.save(output_path)
    return row_count


def generate_xlsx_monthly_files(
    *,
    output_dir: Path,
    year: int,
    months: list[int],
    rows_per_month: int,
    seed: int,
) -> dict[str, int]:
    """One XLSX per month, each with rows_per_month data rows."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {}
    for month in months:
        seq_base = month * 1_000_000
        filename = f"mock-利润报表-订单-Transaction-{year}-{month:02d}.xlsx"
        path = output_dir / filename
        count = generate_xlsx_for_month(
            output_path=path,
            year=year,
            month=month,
            row_count=rows_per_month,
            seed=seed,
            seq_base=seq_base,
        )
        stats[f"{year}-{month:02d}"] = count
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "fixtures" / "mock_data",
        help="Root output directory",
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--months", type=str, default="1-7", help="Month range, e.g. 1-7")
    parser.add_argument("--pages-per-month", type=int, default=5)
    parser.add_argument("--records-per-page", type=int, default=500)
    parser.add_argument(
        "--xlsx-rows-per-month",
        type=int,
        default=7476,
        help="Data rows per monthly XLSX file (default matches original sample)",
    )
    parser.add_argument("--seed", type=int, default=20260817)
    return parser.parse_args()


def _parse_months(spec: str) -> list[int]:
    if "-" in spec:
        start_s, end_s = spec.split("-", 1)
        start, end = int(start_s), int(end_s)
        return list(range(start, end + 1))
    return [int(part) for part in spec.split(",") if part.strip()]


def main() -> None:
    args = parse_args()
    months = _parse_months(args.months)
    root = args.output_dir.expanduser().resolve()
    api_dir = root / "api_pages"
    xlsx_dir = root / "xlsx"

    api_stats = generate_api_pages(
        output_dir=api_dir,
        year=args.year,
        months=months,
        pages_per_month=args.pages_per_month,
        records_per_page=args.records_per_page,
        seed=args.seed,
    )
    xlsx_stats = generate_xlsx_monthly_files(
        output_dir=xlsx_dir,
        year=args.year,
        months=months,
        rows_per_month=args.xlsx_rows_per_month,
        seed=args.seed,
    )
    xlsx_total = sum(xlsx_stats.values())

    readme = root / "README.md"
    readme.write_text(
        "\n".join(
            [
                "# Mock 利润报表数据",
                "",
                "结构与原始样本一致，全部为虚构数据，与真实订单/店铺无任何关联。",
                "默认品类：手机壳 / 手机配件（虚构品牌 CoverNest、ShieldPro 等）。",
                "",
                "## 目录",
                "",
                f"- `api_pages/` — Amazon listTransactions 分页 JSON（{args.pages_per_month} 页/月 × {args.records_per_page} 条/页）",
                f"- `xlsx/` — 领星利润报表导出，**每月一个文件**，各 {args.xlsx_rows_per_month} 行（结算时间在对应月份内）",
                "",
                "## 生成命令",
                "",
                "```bash",
                "python3 scripts/generate_mock_profit_data.py",
                "```",
                "",
                "## 统计",
                "",
                *(f"- `{month}` API 记录: {api_stats[month]}" for month in sorted(api_stats)),
                *(f"- `{month}` XLSX 行数: {xlsx_stats[month]}" for month in sorted(xlsx_stats)),
                f"- XLSX 合计: {xlsx_total} 行（{len(months)} 个月）",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Wrote API pages under {api_dir}")
    for month, count in sorted(api_stats.items()):
        print(f"  {month}: {count} transactions ({args.pages_per_month} pages)")
    print(f"Wrote monthly XLSX under {xlsx_dir}")
    for month, count in sorted(xlsx_stats.items()):
        print(f"  {month}: {count} rows -> mock-利润报表-订单-Transaction-{month}.xlsx")
    print(f"README {readme}")


if __name__ == "__main__":
    main()
