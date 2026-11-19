# Review instructions — kitchen

Ideal customer: unknown, Sam to fill.

## #1 priority: Keep code complexity down

Cut feature creep and over-engineering. Keep changes DRY and avoid
conditionals wherever possible. For low-user products, prefer reusable
patterns—including model judgment—over brittle regex, even when that costs
more or adds latency. Optimize for rapid iteration to product-market fit.
