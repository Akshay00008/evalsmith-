# webui/headless_optimizer.py
# Re-export shim — the real implementation lives in lib/headless_optimizer.py
# so it is available inside the Docker image (which excludes webui/ via
# .dockerignore). All Streamlit pages that already import from this module
# continue to work unchanged through this shim.

from lib.headless_optimizer import (  # noqa: F401
    IterEvent,
    run_optimization,
    pin_winning_variant,
)
