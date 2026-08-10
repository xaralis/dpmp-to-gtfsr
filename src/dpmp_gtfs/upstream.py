"""What DPMP's data gets wrong, and what this project does about it.

Every correction applied to the upstream lives here. Each one is a standing
claim that the source data is wrong or incomplete, and each is a liability: if
DPMP fixes the underlying problem, the correction becomes a lie that quietly
outlives it.

The register
------------

**Neither source says which lines are trolleybus.** ``route_type`` has no
field anywhere in the API, so the split is carried as a constant below. The
CIS migration confirmed it independently rather than replacing it: the
thirteen lines published in ``NeTEx_DrahyMestske.zip`` are exactly the thirteen
listed here.

**Platform coordinates no longer exist upstream.** ``/stops`` publishes one
point per stop and there is no platform endpoint; CIS has no ``Quay`` elements
at all. Platforms therefore inherit their parent's position. Their numbers are
still real -- ``/connections`` reports them per stop -- so ``platform_code``
and the ``S{station}P{platform}`` ids survive; only the geometry is coarser.

Retired corrections
-------------------

The old API omitted stations 250, 252 and 253 and several platforms, which
this module backfilled from OpenStreetMap. ``api.mhdonline.cz`` publishes all
of them (Svítkov,západ 250; Vápenka 252; Mikulovice 178, aliased
"Mikulovice,škola"), within about 3 m and 26 m of the hand-sourced positions,
so the table is gone rather than left to drift behind the real values.
"""

# --- vehicle type -----------------------------------------------------------

TROLLEYBUS_LINES = frozenset({1, 2, 3, 4, 5, 7, 11, 12, 13, 17, 27, 30, 33})
"""Lines DPMP runs as trolleybuses; everything else is a bus.

From the CIS registry, where these (655001-655033) are published in the
separate ``draha/mestske`` archive. Baked in rather than fetched: it changes at
most once every few years, and a 52 MB download to learn thirteen numbers is a
poor trade.
"""
