"""Entry point: ``python -m app.mcp`` or ``finledger-mcp``."""

from app.mcp.server import mcp


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
