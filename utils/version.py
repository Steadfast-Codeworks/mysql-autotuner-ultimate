#!/usr/bin/env python3
"""
Single source of truth for the tool version.

The reports carried three independently hardcoded copies of "1.0.1" (the text
header, the JSON `meta.version`, and the HTML `<h1>`), none of which were
connected to ``MySQLAutoTunerUltimate.VERSION``. That is the same drift class
this release has been closing everywhere else: a value stated in several places
is a value that will eventually disagree with itself, and a report claiming the
wrong version is worse than one claiming none — it misdirects any bug report
filed against it.

``config_ultimate.yaml``'s ``metadata.version`` should match this too;
``--check-config`` is the place to add that assertion if it ever drifts.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2026-08-24
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

TOOL_VERSION = "1.0.4"
