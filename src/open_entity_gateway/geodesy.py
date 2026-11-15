"""WGS 84 Earth-centred, Earth-fixed to geodetic coordinates.

The same method, constants and stopping rule as first-bite's cot transform
(cotify/src/event.rs, `ecef_to_geodetic`), so one capture gives one position
whichever of the two writes the event.
"""

from __future__ import annotations

import math

WGS84_A = 6_378_137.0
WGS84_INVERSE_F = 298.257_223_563
LATITUDE_TOLERANCE = 1e-15
MAX_ITERATIONS = 10


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Latitude and longitude in degrees and height above the ellipsoid in
    metres, by Bowring's method. On the polar axis the longitude is 0."""
    f = 1.0 / WGS84_INVERSE_F
    b = WGS84_A * (1.0 - f)
    e2 = f * (2.0 - f)
    second_e2 = e2 / (1.0 - e2)

    p = math.hypot(x, y)
    longitude = 0.0 if p == 0.0 else math.atan2(y, x)

    parametric = math.atan2(z * WGS84_A, p * b)
    latitude = 0.0
    for _ in range(MAX_ITERATIONS):
        sin, cos = math.sin(parametric), math.cos(parametric)
        following = math.atan2(z + second_e2 * b * sin**3, p - e2 * WGS84_A * cos**3)
        step = abs(following - latitude)
        latitude = following
        if step < LATITUDE_TOLERANCE:
            break
        parametric = math.atan2((1.0 - f) * math.sin(latitude), math.cos(latitude))

    sin, cos = math.sin(latitude), math.cos(latitude)
    altitude = p * cos + z * sin - WGS84_A * math.sqrt(1.0 - e2 * sin * sin)
    return math.degrees(latitude), math.degrees(longitude), altitude
