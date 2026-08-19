from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import connect, sql
from psycopg.rows import dict_row

from ...connections import normalize_analytics_database_type
from ...mysql_connection import mysql_read_only_connection
from .domain import ProfitReportQueryPlan


class ProfitReportQueryError(RuntimeError):
    pass


class ProfitReportQueryTool:
    TABLE = "lingxing_profit_order_transactions"

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

    @staticmethod
    def _mysql_filters(plan: ProfitReportQueryPlan) -> tuple[str, list[Any]]:
        clauses = ["TRUE"]
        parameters: list[Any] = []
        if plan.start_date:
            clauses.append("posted_datetime >= %s")
            parameters.append(plan.start_date)
        if plan.end_date:
            clauses.append("posted_datetime < %s")
            parameters.append(plan.end_date + timedelta(days=1))
        if plan.currency_code:
            clauses.append("currency_code = %s")
            parameters.append(plan.currency_code.upper())
        if plan.store_name:
            clauses.append("store_name = %s")
            parameters.append(plan.store_name)
        return " AND ".join(clauses), parameters

    def _mysql_statement(
        self, plan: ProfitReportQueryPlan
    ) -> tuple[str, list[Any]]:
        filters, parameters = self._mysql_filters(plan)
        if plan.metric == "overview":
            return f"""
                SELECT count(*) AS row_count,
                       coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                       coalesce(sum(settlement_total), 0) AS settlement_total,
                       coalesce(sum(product_sales), 0) AS product_sales_total,
                       min(posted_datetime) AS first_posted_at,
                       max(posted_datetime) AS last_posted_at,
                       min(currency_code) AS currency_code
                FROM {self.TABLE}
                WHERE {filters}
            """, parameters
        dimensions = {
            "daily": (
                "DATE(posted_datetime) AS posted_date",
                "DATE(posted_datetime)",
                "posted_date",
            ),
            "store": ("store_name", "store_name", "gross_profit"),
            "msku": (
                "coalesce(msku, 'UNKNOWN') AS msku, min(asin) AS asin",
                "coalesce(msku, 'UNKNOWN')",
                "gross_profit",
            ),
            "order": (
                "order_id, min(store_name) AS store_name, min(msku) AS msku",
                "order_id",
                "gross_profit",
            ),
            "event_source": (
                "coalesce(event_source, 'UNKNOWN') AS event_source",
                "coalesce(event_source, 'UNKNOWN')",
                "gross_profit",
            ),
        }
        select_dimension, group_dimension, order_mode = dimensions[plan.metric]
        extra_sales = (
            ", coalesce(sum(product_sales), 0) AS product_sales_total"
            if plan.metric == "msku"
            else ""
        )
        order_clause = (
            "posted_date"
            if order_mode == "posted_date"
            else "abs(coalesce(sum(settlement_gross_profit), 0)) DESC"
        )
        statement = f"""
            SELECT {select_dimension},
                   count(*) AS row_count,
                   coalesce(sum(settlement_gross_profit), 0) AS gross_profit_total,
                   coalesce(sum(settlement_total), 0) AS settlement_total,
                   min(currency_code) AS currency_code
                   {extra_sales}
            FROM {self.TABLE}
            WHERE {filters}
            GROUP BY {group_dimension}
            ORDER BY {order_clause}
            LIMIT %s
        """
        return statement, [*parameters, plan.limit]

    def _execute_mysql(
        self, plan: ProfitReportQueryPlan
    ) -> tuple[list[dict[str, Any]], int]:
        with mysql_read_only_connection(
            self.dsn, timeout_ms=self.statement_timeout_ms
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT count(*) AS total,
                           min(posted_datetime) AS first_posted_at,
                           max(posted_datetime) AS last_posted_at
                    FROM {self.TABLE}
                    """
                )
                table_stats = cursor.fetchone()
            table_total = int(table_stats["total"])
            if table_total == 0:
                raise ProfitReportQueryError(
                    "利润报表数据库表为空，请先导入数据"
                )
            statement, parameters = self._mysql_statement(plan)
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                rows = [
                    {key: self._json_value(value) for key, value in row.items()}
                    for row in cursor.fetchall()
                ]
            filters, filter_params = self._mysql_filters(plan)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT count(*) AS total FROM {self.TABLE} WHERE {filters}",
                    filter_params,
                )
                total = int(cursor.fetchone()["total"])
        self._raise_empty_result(plan, rows, total, table_total, table_stats)
        return rows, total

    @staticmethod
    def _raise_empty_result(
        plan: ProfitReportQueryPlan,
        rows: list[dict[str, Any]],
        total: int,
        table_total: int,
        table_stats: dict[str, Any],
    ) -> None:
        if rows or total:
            return
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

    def execute(self, plan: ProfitReportQueryPlan) -> tuple[list[dict[str, Any]], int]:
        if not self.dsn:
            raise ProfitReportQueryError("未配置数据库连接")
        if self.engine == "mysql":
            return self._execute_mysql(plan)
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
        self._raise_empty_result(plan, rows, total, table_total, table_stats)
        return rows, total
