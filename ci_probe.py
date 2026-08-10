"""Temporary probe: trips ruff format --check AND mypy in one run. Reverted after."""


def add(a: int, b: int) -> int:
    return a + b


values = [1,2,3]
result: str = add(values[0], values[1])
