"""A long, executable monthly cohort analysis; no comments or padding lines."""

from datetime import date


def large_query() -> str:
    ctes, unions = [], []
    for year in range(2022, 2027):
        for month in range(1, 13):
            start = date(year, month, 1)
            until = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
            name = f"month_{year}_{month:02}"
            ctes.append(f"""{name} AS (
    SELECT
        c.region AS region,
        COUNT(o.id) AS order_count,
        SUM(o.net_total) AS net_revenue,
        AVG(o.net_total) AS mean_order,
        MIN(o.net_total) AS minimum_order,
        MAX(o.net_total) AS maximum_order,
        SUM(CASE WHEN o.active THEN o.net_total ELSE 0 END) AS active_revenue,
        COUNT(DISTINCT o.customer_id) AS customer_count,
        SUM(o.quantity) AS total_quantity,
        SUM(o.net_total) / NULLIF(COUNT(o.id), 0) AS revenue_per_order
    FROM public.orders AS o
    JOIN public.customers AS c
        ON c.id = o.customer_id
    WHERE o.ordered_on >= DATE '{start}'
        AND o.ordered_on < DATE '{until}'
    GROUP BY c.region
)""")
            unions.append(f"""    SELECT DATE '{start}' AS month, region, order_count, net_revenue,
        mean_order, minimum_order, maximum_order, active_revenue,
        customer_count, total_quantity, revenue_per_order
    FROM {name}""")
    ctes.append("monthly AS (\n" + "\n    UNION ALL\n".join(unions) + "\n)")
    return (
        "WITH\n"
        + ",\n".join(ctes)
        + """
SELECT
    month,
    region,
    order_count,
    net_revenue,
    mean_order,
    minimum_order,
    maximum_order,
    active_revenue,
    customer_count,
    total_quantity,
    revenue_per_order,
    LAG(net_revenue) OVER (PARTITION BY region ORDER BY month) AS previous_revenue,
    SUM(net_revenue) OVER (
        PARTITION BY region ORDER BY month
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_revenue,
    RANK() OVER (PARTITION BY month ORDER BY net_revenue DESC) AS revenue_rank
FROM monthly
ORDER BY month, region NULLS LAST
"""
    )


REFERENCE_SQL = """
WITH monthly AS (
 SELECT date_trunc('month',o.ordered_on)::date AS month, c.region,
        count(o.id) AS order_count, sum(o.net_total) AS net_revenue,
        avg(o.net_total) AS mean_order, min(o.net_total) AS minimum_order,
        max(o.net_total) AS maximum_order,
        sum(CASE WHEN o.active THEN o.net_total ELSE 0 END) AS active_revenue,
        count(DISTINCT o.customer_id) AS customer_count, sum(o.quantity) AS total_quantity,
        sum(o.net_total)/nullif(count(o.id),0) AS revenue_per_order
 FROM public.orders o JOIN public.customers c ON c.id=o.customer_id
 WHERE o.ordered_on >= DATE '2022-01-01' AND o.ordered_on < DATE '2027-01-01'
 GROUP BY date_trunc('month',o.ordered_on)::date,c.region
)
SELECT month,region,order_count,net_revenue,mean_order,minimum_order,maximum_order,
       active_revenue,customer_count,total_quantity,revenue_per_order,
       lag(net_revenue) OVER (PARTITION BY region ORDER BY month) AS previous_revenue,
       sum(net_revenue) OVER (PARTITION BY region ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_revenue,
       rank() OVER (PARTITION BY month ORDER BY net_revenue DESC) AS revenue_rank
FROM monthly ORDER BY month,region NULLS LAST
"""
