"""Aperam Genk (GNK) mode of the copilot.

Switched on with ``COPILOT_DOMAIN=aperam_gnk``. In that mode:

* the context bundle is built from the GNK gold layer (``src/aperam_gnk/sql``) over
  ``metal_movements`` and ``timebased_sensor`` instead of the bottling-line simulator;
* every specialist prompt is replaced by its stainless-steel / Genk counterpart
  (``agent_server.aperam_gnk.prompts``) through the single resolver
  ``agent_server.prompts.loader.prompt_text``;
* specialists get four deterministic GNK tools (``agent_server.aperam_gnk.tools``).

With the variable unset the app behaves exactly as upstream.
"""

from __future__ import annotations

import os

DOMAIN_ENV = "COPILOT_DOMAIN"
GNK_DOMAIN = "aperam_gnk"

# Genk timestamps in metal_movements / timebased_sensor are LOCAL time (see source docs).
SITE_TZ = "Europe/Brussels"


def is_gnk() -> bool:
    """Read late (per call) so tests and the app can flip it with the environment."""
    return os.environ.get(DOMAIN_ENV, "").strip().lower() == GNK_DOMAIN
