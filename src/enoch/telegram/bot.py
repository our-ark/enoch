"""Compatibility import for the channel-neutral Enoch application.

New integrations should import :mod:`enoch.application`.  Replacing this
module in ``sys.modules`` keeps historical patch/import paths working while
there remains only one application implementation.
"""

from __future__ import annotations

import sys

from enoch import application as _application


sys.modules[__name__] = _application
