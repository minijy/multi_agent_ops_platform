from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import connect, sql
from psycopg.rows import dict_row

from .domain import AmazonFinanceQueryPlan


class AmazonFinanceQueryError(RuntimeError):
    pass


class AmazonFinanceQueryTool:
    """Compile approved BI plans into parameterized, read-only SQL."""

    def __init__(self, dsn: str, *, statement_timeout_ms: int = 5000) -> None:
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms

    def _resolve_seller(self, connection: Any, requested: str | None) -> str:
        if requested:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM amazon_finance_transactions WHERE seller_id=%s LIMIT 1",
                    (requested,),
                )
                if cursor.fetchone() is None:
                    raise AmazonFinanceQueryError("seller_id 不存在或没有可查询数据")
            return requested
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT seller_id FROM amazon_finance_transactions ORDER BY seller_id LIMIT 2"
            )
            sellers = [str(row["seller_id"]) for row in cursor.fetchall()]
        if not sellers:
            raise AmazonFinanceQueryError("Amazon 结算数据表为空")
        if len(sellers) > 1:
            raise AmazonFinanceQueryError("存在多个卖家，请明确提供 seller_id")
        return sellers[0]

    @staticmethod
    def _date_filter(
        plan: AmazonFinanceQueryPlan, *, alias: str = "t"
    ) -> tuple[sql.SQL, list[Any]]:
        clauses: list[sql.SQL] = [
            sql.SQL("{}.transaction_status = 'RELEASED'").format(sql.Identifier(alias))
        ]
        parameters: list[Any] = []
        if plan.start_date:
            clauses.append(sql.SQL("{}.posted_at >= %s").format(sql.Identifier(alias)))
            parameters.append(plan.start_date)
        if plan.end_date:
            clauses.append(sql.SQL("{}.posted_at < %s").format(sql.Identifier(alias)))
            parameters.append(plan.end_date + timedelta(days=1))
        return sql.SQL(" AND ") + sql.SQL(" AND ").join(clauses), parameters

    def _statement(
        self, plan: AmazonFinanceQueryPlan
    ) -> tuple[sql.Composed, list[Any]]:
        date_filter, date_parameters = self._date_filter(plan)
        common_parameters: list[Any] = []

        if plan.metric == "overview":
            statement = sql.SQL(
                """
                SELECT count(*) AS transaction_count,
                       coalesce(sum(t.total_amount), 0) AS net_amount,
                       min(t.posted_at) AS first_posted_at,
                       max(t.posted_at) AS last_posted_at,
                       min(t.currency_code) AS currency_code
                FROM amazon_finance_transactions t
                WHERE t.seller_id=%s {date_filter}
                """
            ).format(date_filter=date_filter)
        elif plan.metric == "daily":
            statement = sql.SQL(
                """
                SELECT (t.posted_at AT TIME ZONE 'UTC')::date AS posted_date,
                       count(*) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       min(t.currency_code) AS currency_code
                FROM amazon_finance_transactions t
                WHERE t.seller_id=%s {date_filter}
                GROUP BY posted_date
                ORDER BY posted_date
                LIMIT %s
                """
            ).format(date_filter=date_filter)
            common_parameters.append(plan.limit)
        elif plan.metric == "transaction_type":
            statement = sql.SQL(
                """
                SELECT t.transaction_type,
                       count(*) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       min(t.currency_code) AS currency_code
                FROM amazon_finance_transactions t
                WHERE t.seller_id=%s {date_filter}
                GROUP BY t.transaction_type
                ORDER BY abs(sum(t.total_amount)) DESC
                LIMIT %s
                """
            ).format(date_filter=date_filter)
            common_parameters.append(plan.limit)
        elif plan.metric == "fee":
            statement = sql.SQL(
                """
                SELECT l.category_level_1 AS fee_category,
                       coalesce(l.category_level_2, l.category_level_1) AS fee_type,
                       count(*) AS line_count,
                       sum(l.amount) AS amount,
                       min(l.currency_code) AS currency_code
                FROM amazon_finance_amount_lines l
                JOIN amazon_finance_transactions t
                  ON t.seller_id=l.seller_id AND t.transaction_id=l.transaction_id
                WHERE t.seller_id=%s
                  AND l.category_level_1 IN ('AmazonFees', 'FBAFees')
                  {date_filter}
                GROUP BY l.category_level_1, coalesce(l.category_level_2, l.category_level_1)
                ORDER BY abs(sum(l.amount)) DESC
                LIMIT %s
                """
            ).format(date_filter=date_filter)
            common_parameters.append(plan.limit)
        elif plan.metric == "sku":
            statement = sql.SQL(
                """
                SELECT coalesce(i.sku, 'UNKNOWN') AS sku,
                       min(i.asin) AS asin,
                       sum(coalesce(i.quantity_shipped, 0)) AS quantity_shipped,
                       sum(coalesce(i.total_amount, 0)) AS net_amount,
                       min(i.currency_code) AS currency_code
                FROM amazon_finance_items i
                JOIN amazon_finance_transactions t
                  ON t.seller_id=i.seller_id AND t.transaction_id=i.transaction_id
                WHERE t.seller_id=%s {date_filter}
                GROUP BY coalesce(i.sku, 'UNKNOWN')
                ORDER BY abs(sum(coalesce(i.total_amount, 0))) DESC
                LIMIT %s
                """
            ).format(date_filter=date_filter)
            common_parameters.append(plan.limit)
        else:
            statement = sql.SQL(
                """
                SELECT ids.identifier_value AS settlement_id,
                       count(DISTINCT t.transaction_id) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       min(t.posted_at) AS first_posted_at,
                       max(t.posted_at) AS last_posted_at,
                       min(t.currency_code) AS currency_code
                FROM amazon_finance_transaction_identifiers ids
                JOIN amazon_finance_transactions t
                  ON t.seller_id=ids.seller_id AND t.transaction_id=ids.transaction_id
                WHERE t.seller_id=%s
                  AND ids.identifier_name='SETTLEMENT_ID'
                  {date_filter}
                GROUP BY ids.identifier_value
                ORDER BY max(t.posted_at) DESC
                LIMIT %s
                """
            ).format(date_filter=date_filter)
            common_parameters.append(plan.limit)

        return statement, date_parameters + common_parameters

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    def execute(
        self, plan: AmazonFinanceQueryPlan, *, seller_id: str | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        if not self.dsn:
            raise AmazonFinanceQueryError("ANALYTICS_DSN 未配置")
        with connect(self.dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        sql.SQL("SET LOCAL statement_timeout = {}").format(
                            sql.Literal(f"{self.statement_timeout_ms}ms")
                        )
                    )
                resolved_seller = self._resolve_seller(connection, seller_id)
                statement, parameters = self._statement(plan)
                with connection.cursor() as cursor:
                    cursor.execute(statement, [resolved_seller, *parameters])
                    rows = [
                        {key: self._json_value(value) for key, value in row.items()}
                        for row in cursor.fetchall()
                    ]
        return resolved_seller, rows
