from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import connect, sql
from psycopg.rows import dict_row

from ...connections import normalize_analytics_database_type
from ...mysql_connection import mysql_read_only_connection
from .domain import AmazonFinanceQueryPlan


class AmazonFinanceQueryError(RuntimeError):
    pass


class AmazonFinanceQueryTool:
    """Compile approved BI plans into parameterized, read-only SQL."""

    TRANSACTIONS = "amazon_finance_released_transactions"
    ITEMS = "amazon_finance_released_items"
    AMOUNT_LINES = "amazon_finance_released_amount_lines"
    IDENTIFIERS = "amazon_finance_released_identifiers"

    def __init__(
        self,
        dsn: str,
        *,
        statement_timeout_ms: int = 5000,
        engine: str = "postgresql",
    ) -> None:
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms
        self.engine = normalize_analytics_database_type(engine)

    def _scope_filter(
        self,
        plan: AmazonFinanceQueryPlan,
        tenant_id: str,
        marketplace_ids: tuple[str, ...] = (),
        *,
        alias: str = "t",
    ) -> tuple[sql.SQL, list[Any]]:
        if not tenant_id.strip():
            raise AmazonFinanceQueryError("tenant_id is required")
        clauses: list[sql.SQL] = [
            sql.SQL("{}.transaction_status = 'RELEASED'").format(sql.Identifier(alias)),
            sql.SQL("{}.tenant_id = %s").format(sql.Identifier(alias)),
        ]
        parameters: list[Any] = [tenant_id]
        if marketplace_ids and "*" not in marketplace_ids:
            clauses.append(
                sql.SQL("{}.marketplace_id = ANY(%s)").format(sql.Identifier(alias))
            )
            parameters.append(list(marketplace_ids))
        if plan.start_date:
            clauses.append(sql.SQL("{}.posted_at >= %s").format(sql.Identifier(alias)))
            parameters.append(plan.start_date)
        if plan.end_date:
            clauses.append(sql.SQL("{}.posted_at < %s").format(sql.Identifier(alias)))
            parameters.append(plan.end_date + timedelta(days=1))
        return sql.SQL(" AND ") + sql.SQL(" AND ").join(clauses), parameters

    def _statement(
        self,
        plan: AmazonFinanceQueryPlan,
        tenant_id: str,
        marketplace_ids: tuple[str, ...] = (),
    ) -> tuple[sql.Composed, list[Any]]:
        date_filter, date_parameters = self._scope_filter(
            plan, tenant_id, marketplace_ids
        )
        common_parameters: list[Any] = []
        transactions = sql.Identifier(self.TRANSACTIONS)
        items = sql.Identifier(self.ITEMS)
        amount_lines = sql.Identifier(self.AMOUNT_LINES)
        identifiers = sql.Identifier(self.IDENTIFIERS)

        if plan.metric == "overview":
            statement = sql.SQL(
                """
                SELECT count(*) AS transaction_count,
                       coalesce(sum(t.total_amount), 0) AS net_amount,
                       min(t.posted_at) AS first_posted_at,
                       max(t.posted_at) AS last_posted_at,
                       t.currency_code
                FROM {transactions} t
                WHERE TRUE {date_filter}
                GROUP BY t.currency_code
                ORDER BY t.currency_code
                """
            ).format(transactions=transactions, date_filter=date_filter)
        elif plan.metric == "daily":
            statement = sql.SQL(
                """
                SELECT (t.posted_at AT TIME ZONE 'UTC')::date AS posted_date,
                       count(*) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       t.currency_code
                FROM {transactions} t
                WHERE TRUE {date_filter}
                GROUP BY posted_date, t.currency_code
                ORDER BY posted_date
                LIMIT %s
                """
            ).format(transactions=transactions, date_filter=date_filter)
            common_parameters.append(plan.limit)
        elif plan.metric == "transaction_type":
            statement = sql.SQL(
                """
                SELECT t.transaction_type,
                       count(*) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       t.currency_code
                FROM {transactions} t
                WHERE TRUE {date_filter}
                GROUP BY t.transaction_type, t.currency_code
                ORDER BY abs(sum(t.total_amount)) DESC
                LIMIT %s
                """
            ).format(transactions=transactions, date_filter=date_filter)
            common_parameters.append(plan.limit)
        elif plan.metric == "fee":
            statement = sql.SQL(
                """
                SELECT l.category_level_1 AS fee_category,
                       coalesce(l.category_level_2, l.category_level_1) AS fee_type,
                       count(*) AS line_count,
                       sum(l.amount) AS amount,
                       l.currency_code
                FROM {amount_lines} l
                JOIN {transactions} t
                  ON t.seller_id=l.seller_id AND t.transaction_id=l.transaction_id
                WHERE l.category_level_1 IN ('AmazonFees', 'FBAFees')
                  {date_filter}
                GROUP BY l.category_level_1,
                         coalesce(l.category_level_2, l.category_level_1),
                         l.currency_code
                ORDER BY abs(sum(l.amount)) DESC
                LIMIT %s
                """
            ).format(
                amount_lines=amount_lines,
                transactions=transactions,
                date_filter=date_filter,
            )
            common_parameters.append(plan.limit)
        elif plan.metric == "sku":
            statement = sql.SQL(
                """
                SELECT coalesce(i.sku, 'UNKNOWN') AS sku,
                       min(i.asin) AS asin,
                       sum(coalesce(i.quantity_shipped, 0)) AS quantity_shipped,
                       sum(coalesce(i.total_amount, 0)) AS net_amount,
                       i.currency_code
                FROM {items} i
                JOIN {transactions} t
                  ON t.seller_id=i.seller_id AND t.transaction_id=i.transaction_id
                WHERE TRUE {date_filter}
                GROUP BY coalesce(i.sku, 'UNKNOWN'), i.currency_code
                ORDER BY abs(sum(coalesce(i.total_amount, 0))) DESC
                LIMIT %s
                """
            ).format(items=items, transactions=transactions, date_filter=date_filter)
            common_parameters.append(plan.limit)
        else:
            statement = sql.SQL(
                """
                SELECT ids.identifier_value AS settlement_id,
                       count(DISTINCT t.transaction_id) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       min(t.posted_at) AS first_posted_at,
                       max(t.posted_at) AS last_posted_at,
                       t.currency_code
                FROM {identifiers} ids
                JOIN {transactions} t
                  ON t.seller_id=ids.seller_id AND t.transaction_id=ids.transaction_id
                WHERE ids.identifier_name='SETTLEMENT_ID'
                  {date_filter}
                GROUP BY ids.identifier_value, t.currency_code
                ORDER BY max(t.posted_at) DESC
                LIMIT %s
                """
            ).format(
                identifiers=identifiers,
                transactions=transactions,
                date_filter=date_filter,
            )
            common_parameters.append(plan.limit)

        return statement, date_parameters + common_parameters

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    def _mysql_scope_filter(
        self,
        plan: AmazonFinanceQueryPlan,
        tenant_id: str,
        marketplace_ids: tuple[str, ...] = (),
        *,
        alias: str = "t",
    ) -> tuple[str, list[Any]]:
        if not tenant_id.strip():
            raise AmazonFinanceQueryError("tenant_id is required")
        clauses = [
            f"{alias}.transaction_status = 'RELEASED'",
            f"{alias}.tenant_id = %s",
        ]
        parameters: list[Any] = [tenant_id]
        if marketplace_ids and "*" not in marketplace_ids:
            placeholders = ", ".join(["%s"] * len(marketplace_ids))
            clauses.append(f"{alias}.marketplace_id IN ({placeholders})")
            parameters.extend(marketplace_ids)
        if plan.start_date:
            clauses.append(f"{alias}.posted_at >= %s")
            parameters.append(plan.start_date)
        if plan.end_date:
            clauses.append(f"{alias}.posted_at < %s")
            parameters.append(plan.end_date + timedelta(days=1))
        return " AND " + " AND ".join(clauses), parameters

    def _mysql_statement(
        self,
        plan: AmazonFinanceQueryPlan,
        tenant_id: str,
        marketplace_ids: tuple[str, ...] = (),
    ) -> tuple[str, list[Any]]:
        date_filter, date_parameters = self._mysql_scope_filter(
            plan, tenant_id, marketplace_ids
        )
        limit_parameters: list[Any] = []
        transactions = self.TRANSACTIONS
        items = self.ITEMS
        amount_lines = self.AMOUNT_LINES
        identifiers = self.IDENTIFIERS
        if plan.metric == "overview":
            statement = f"""
                SELECT count(*) AS transaction_count,
                       coalesce(sum(t.total_amount), 0) AS net_amount,
                       min(t.posted_at) AS first_posted_at,
                       max(t.posted_at) AS last_posted_at,
                       t.currency_code
                FROM {transactions} t
                WHERE TRUE {date_filter}
                GROUP BY t.currency_code
                ORDER BY t.currency_code
            """
        elif plan.metric == "daily":
            statement = f"""
                SELECT DATE(t.posted_at) AS posted_date,
                       count(*) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       t.currency_code
                FROM {transactions} t
                WHERE TRUE {date_filter}
                GROUP BY DATE(t.posted_at), t.currency_code
                ORDER BY posted_date
                LIMIT %s
            """
            limit_parameters.append(plan.limit)
        elif plan.metric == "transaction_type":
            statement = f"""
                SELECT t.transaction_type,
                       count(*) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       t.currency_code
                FROM {transactions} t
                WHERE TRUE {date_filter}
                GROUP BY t.transaction_type, t.currency_code
                ORDER BY abs(sum(t.total_amount)) DESC
                LIMIT %s
            """
            limit_parameters.append(plan.limit)
        elif plan.metric == "fee":
            statement = f"""
                SELECT l.category_level_1 AS fee_category,
                       coalesce(l.category_level_2, l.category_level_1) AS fee_type,
                       count(*) AS line_count,
                       sum(l.amount) AS amount,
                       l.currency_code
                FROM {amount_lines} l
                JOIN {transactions} t
                  ON t.seller_id=l.seller_id AND t.transaction_id=l.transaction_id
                WHERE l.category_level_1 IN ('AmazonFees', 'FBAFees')
                  {date_filter}
                GROUP BY l.category_level_1,
                         coalesce(l.category_level_2, l.category_level_1),
                         l.currency_code
                ORDER BY abs(sum(l.amount)) DESC
                LIMIT %s
            """
            limit_parameters.append(plan.limit)
        elif plan.metric == "sku":
            statement = f"""
                SELECT coalesce(i.sku, 'UNKNOWN') AS sku,
                       min(i.asin) AS asin,
                       sum(coalesce(i.quantity_shipped, 0)) AS quantity_shipped,
                       sum(coalesce(i.total_amount, 0)) AS net_amount,
                       i.currency_code
                FROM {items} i
                JOIN {transactions} t
                  ON t.seller_id=i.seller_id AND t.transaction_id=i.transaction_id
                WHERE TRUE {date_filter}
                GROUP BY coalesce(i.sku, 'UNKNOWN'), i.currency_code
                ORDER BY abs(sum(coalesce(i.total_amount, 0))) DESC
                LIMIT %s
            """
            limit_parameters.append(plan.limit)
        else:
            statement = f"""
                SELECT ids.identifier_value AS settlement_id,
                       count(DISTINCT t.transaction_id) AS transaction_count,
                       sum(t.total_amount) AS net_amount,
                       min(t.posted_at) AS first_posted_at,
                       max(t.posted_at) AS last_posted_at,
                       t.currency_code
                FROM {identifiers} ids
                JOIN {transactions} t
                  ON t.seller_id=ids.seller_id AND t.transaction_id=ids.transaction_id
                WHERE ids.identifier_name='SETTLEMENT_ID'
                  {date_filter}
                GROUP BY ids.identifier_value, t.currency_code
                ORDER BY max(t.posted_at) DESC
                LIMIT %s
            """
            limit_parameters.append(plan.limit)
        return statement, date_parameters + limit_parameters

    def _execute_mysql(
        self,
        plan: AmazonFinanceQueryPlan,
        tenant_id: str,
        marketplace_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        statement, parameters = self._mysql_statement(
            plan, tenant_id, marketplace_ids
        )
        with mysql_read_only_connection(
            self.dsn, timeout_ms=self.statement_timeout_ms
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return [
                    {key: self._json_value(value) for key, value in row.items()}
                    for row in cursor.fetchall()
                ]

    def execute(
        self,
        plan: AmazonFinanceQueryPlan,
        *,
        tenant_id: str,
        marketplace_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        if not self.dsn:
            raise AmazonFinanceQueryError("未配置数据库连接")
        if not tenant_id.strip():
            raise AmazonFinanceQueryError("tenant_id is required")
        if self.engine == "mysql":
            return self._execute_mysql(plan, tenant_id, marketplace_ids)
        with connect(self.dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        sql.SQL("SET LOCAL statement_timeout = {}").format(
                            sql.Literal(f"{self.statement_timeout_ms}ms")
                        )
                    )
                    cursor.execute(
                        "SELECT set_config('app.tenant_id', %s, true)",
                        (tenant_id,),
                    )
                statement, parameters = self._statement(
                    plan, tenant_id, marketplace_ids
                )
                with connection.cursor() as cursor:
                    cursor.execute(statement, parameters)
                    rows = [
                        {key: self._json_value(value) for key, value in row.items()}
                        for row in cursor.fetchall()
                    ]
        return rows
