from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import connect, sql
from psycopg.rows import dict_row

from .domain import ProfitReportQueryPlan


class ProfitReportQueryError(RuntimeError):
    pass


class ProfitReportQueryTool:
    TABLE = "lingxing_profit_order_transactions"

    def __init__(self, dsn: str, *, statement_timeout_ms: int = 5000) -> None:
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    def _filters(
        self, plan: ProfitReportQueryPlan
    ) -> tuple[sql.SQL, list[Any]]:
        clauses: list[sql.SQL] = [sql.SQL("TRUE")]
        parameters: list[Any] = []
        if plan.start_date:
            clauses.append(sql.SQL("posted_datetime >= %s"))
            parameters.append(plan.start_date)
        if plan.end_date:
            clauses.append(sql.SQL("posted_datetime < %s"))
            parameters.append(plan.end_date + timedelta(days=1))
        if plan.currency_code:
            clauses.append(sql.SQL("currency_code = %s"))
            parameters.append(plan.currency_code.upper())
        if plan.store_name:
            clauses.append(sql.SQL("store_name = %s"))
            parameters.append(plan.store_name)
        return sql.SQL(" AND ").join(clauses), parameters

    def _statement(
        self, plan: ProfitReportQueryPlan
    ) -> tuple[sql.Composed, list[Any]]:
        filters, parameters = self._filters(plan)
        table = sql.Identifier(self.TABLE)

        if plan.metric == "overview":
            statement = sql.SQL(
                """
                SELECT count(*) AS row_count,
                       coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                       coalesce(sum(settlement_total), 0) AS settlement_total,
                       coalesce(sum(product_sales), 0) AS product_sales_total,
                       min(posted_datetime) AS first_posted_at,
                       max(posted_datetime) AS last_posted_at,
                       min(currency_code) AS currency_code
                FROM {table}
                WHERE {filters}
                """
            ).format(table=table, filters=filters)
            return statement, parameters
        if plan.metric == "daily":
            statement = sql.SQL(
                """
                SELECT posted_datetime::date AS posted_date,
                       count(*) AS row_count,
                       coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                       coalesce(sum(settlement_total), 0) AS settlement_total,
                       min(currency_code) AS currency_code
                FROM {table}
                WHERE {filters}
                GROUP BY posted_date
                ORDER BY posted_date
                LIMIT %s
                """
            ).format(table=table, filters=filters)
            return statement, [*parameters, plan.limit]
        if plan.metric == "store":
            statement = sql.SQL(
                """
                SELECT store_name,
                       count(*) AS row_count,
                       coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                       coalesce(sum(settlement_total), 0) AS settlement_total,
                       min(currency_code) AS currency_code
                FROM {table}
                WHERE {filters}
                GROUP BY store_name
                ORDER BY abs(coalesce(sum(settlement_gross_profit), 0)) DESC
                LIMIT %s
                """
            ).format(table=table, filters=filters)
            return statement, [*parameters, plan.limit]
        if plan.metric == "msku":
            statement = sql.SQL(
                """
                SELECT coalesce(msku, 'UNKNOWN') AS msku,
                       min(asin) AS asin,
                       count(*) AS row_count,
                       coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                       coalesce(sum(product_sales), 0) AS product_sales_total,
                       min(currency_code) AS currency_code
                FROM {table}
                WHERE {filters}
                GROUP BY coalesce(msku, 'UNKNOWN')
                ORDER BY abs(coalesce(sum(settlement_gross_profit), 0)) DESC
                LIMIT %s
                """
            ).format(table=table, filters=filters)
            return statement, [*parameters, plan.limit]
        if plan.metric == "order":
            statement = sql.SQL(
                """
                SELECT order_id,
                       min(store_name) AS store_name,
                       min(msku) AS msku,
                       count(*) AS row_count,
                       coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                       coalesce(sum(settlement_total), 0) AS settlement_total,
                       min(currency_code) AS currency_code
                FROM {table}
                WHERE {filters}
                GROUP BY order_id
                ORDER BY abs(coalesce(sum(settlement_gross_profit), 0)) DESC
                LIMIT %s
                """
            ).format(table=table, filters=filters)
            return statement, [*parameters, plan.limit]
        statement = sql.SQL(
            """
            SELECT coalesce(event_source, 'UNKNOWN') AS event_source,
                   count(*) AS row_count,
                   coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                   coalesce(sum(settlement_total), 0) AS settlement_total,
                   min(currency_code) AS currency_code
            FROM {table}
            WHERE {filters}
            GROUP BY coalesce(event_source, 'UNKNOWN')
            ORDER BY abs(coalesce(sum(settlement_gross_profit), 0)) DESC
            LIMIT %s
            """
        ).format(table=table, filters=filters)
        return statement, [*parameters, plan.limit]

    def execute(self, plan: ProfitReportQueryPlan) -> tuple[list[dict[str, Any]], int]:
        if not self.dsn:
            raise ProfitReportQueryError("ANALYTICS_DSN 未配置")
        with connect(self.dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        sql.SQL("SET LOCAL statement_timeout = {}").format(
                            sql.Literal(f"{self.statement_timeout_ms}ms")
                        )
                    )
                table = sql.Identifier(self.TABLE)
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            """
                            SELECT count(*) AS total,
                                   min(posted_datetime) AS first_posted_at,
                                   max(posted_datetime) AS last_posted_at
                            FROM {table}
                            """
                        ).format(table=table)
                    )
                    table_stats = cursor.fetchone()
                table_total = int(table_stats["total"])
                if table_total == 0:
                    raise ProfitReportQueryError(
                        "利润报表本地表为空，请先导入 XLSX 数据"
                    )
                statement, parameters = self._statement(plan)
                with connection.cursor() as cursor:
                    cursor.execute(statement, parameters)
                    rows = [
                        {key: self._json_value(value) for key, value in row.items()}
                        for row in cursor.fetchall()
                    ]
                with connection.cursor() as cursor:
                    filters, filter_params = self._filters(plan)
                    count_statement = sql.SQL(
                        "SELECT count(*) AS total FROM {table} WHERE {filters}"
                    ).format(
                        table=sql.Identifier(self.TABLE),
                        filters=filters,
                    )
                    cursor.execute(count_statement, filter_params)
                    total = int(cursor.fetchone()["total"])
        if not rows and total == 0:
            first = table_stats["first_posted_at"]
            last = table_stats["last_posted_at"]
            range_hint = ""
            if first and last:
                first_day = first.date() if hasattr(first, "date") else first
                last_day = last.date() if hasattr(last, "date") else last
                range_hint = (
                    f"当前库内结算时间范围：{first_day} ~ {last_day}，"
                    f"共 {table_total} 行。"
                )
            raise ProfitReportQueryError(
                "指定日期或筛选条件下没有利润报表记录。"
                + (f" {range_hint}" if range_hint else "")
                + " 请调整 start_date / end_date 后重试。"
            )
        return rows, total
