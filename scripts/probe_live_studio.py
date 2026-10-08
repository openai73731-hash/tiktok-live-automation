"""Developer convenience entry point; requires the project's editable installation."""
from app.windows.probe import cli

if __name__ == "__main__":
    raise SystemExit(cli())
