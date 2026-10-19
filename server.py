#!/usr/bin/env python3
"""Historical launcher; use an env -u PYTHONPATH wrapper for startup isolation."""

import os

os.environ.pop("PYTHONPATH", None)

from kleinanzeigen_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
