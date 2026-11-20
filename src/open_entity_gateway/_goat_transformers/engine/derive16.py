"""The derive functions of spec 0.16: time, distance and frames
(spec/engine-spec.md, "Version 0.16: derive functions for time, distance and
frames").

Every function returns the target's value, or None when the target stays
absent. None of them raises: an intermediate that is not finite, a division by
zero or a square root of a negative number gives None (ruling 1)."""

import math

_DEG = math.pi / 180.0  # degrees to radians, the quotient first
_RAD = 180.0 / math.pi  # radians to degrees, the quotient first (ruling 8)
_INT64 = (-(1 << 63), (1 << 63) - 1)

# WGS 84
_A = 6378137.0
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)
_B = _A * (1.0 - _F)
_E4 = _E2 * _E2
_A2 = _A * _A
_B2 = _B * _B


class _Absent(Exception):
    """An intermediate that leaves the target absent (ruling 1)."""


def _fin(x):
    if not math.isfinite(x):
        raise _Absent()
    return x


def _div(x, y):
    if y == 0:
        raise _Absent()
    return _fin(x / y)


def _sqrt(x):
    if x < 0 or not math.isfinite(x):
        raise _Absent()
    return math.sqrt(x)


if hasattr(math, "cbrt"):  # Python 3.11 and later
    def _cbrt(x):
        return _fin(math.cbrt(x))
else:  # pragma: no cover - the real cube root of older Pythons
    def _cbrt(x):
        _fin(x)
        r = abs(x) ** (1.0 / 3.0)
        if r != 0:
            r = r - (r * r * r - abs(x)) / (3.0 * r * r)  # one Newton step
        return math.copysign(r, x)


def _pow(x, y):
    try:
        return _fin(x ** y)
    except (OverflowError, ZeroDivisionError, ValueError):
        raise _Absent()


def _guard(fn):
    """Wrap a float function: `_Absent` and any arithmetic failure give
    None, and so does a result that is not finite."""
    def run(*args):
        try:
            r = fn(*[_fin(float(a)) for a in args])
        except (_Absent, ArithmeticError, ValueError):
            return None
        if r is None or not math.isfinite(r):
            return None
        return r
    return run


def _int_result(r):
    return r if _INT64[0] <= r <= _INT64[1] else None


# --- time (exact integers, ruling 3) -----------------------------------------

def sec_of(ms):
    return _int_result(int(ms) // 1000)


def nsec_of(ms):
    ms = int(ms)
    return _int_result((ms - 1000 * (ms // 1000)) * 1000000)


def ms_of(sec, nsec):
    return _int_result(int(sec) * 1000 + int(nsec) // 1000000)


def add_int(x0, x1):
    return _int_result(int(x0) + int(x1))


def sub_int(x0, x1):
    return _int_result(int(x0) - int(x1))


# --- the ellipsoid -------------------------------------------------------------

def _phi(lat):
    """lat in degrees -> φ in radians; a latitude outside [-90, 90] leaves the
    target absent (ruling 6)."""
    if not -90.0 <= lat <= 90.0:
        raise _Absent()
    return _fin(lat * _DEG)


def _w(phi):
    s = math.sin(phi)
    return _fin(1.0 - _E2 * s * s)


def _n(phi):
    return _div(_A, _sqrt(_w(phi)))


def _m(phi):
    return _div(_A * (1.0 - _E2), _pow(_w(phi), 1.5))


def _deg_lat_of(dist, lat):
    phi = _phi(lat)
    return _fin(_div(dist, _m(phi)) * _RAD)


def _deg_lon_of(dist, lat):
    phi = _phi(lat)
    c = abs(math.cos(phi))
    if c <= 1e-12:
        return None
    return _fin(_div(dist, _fin(_n(phi) * c)) * _RAD)


def _m_lat_of(ang, lat):
    phi = _phi(lat)
    return _fin(_fin(ang * _DEG) * _m(phi))


def _m_lon_of(ang, lat):
    phi = _phi(lat)
    return _fin(_fin(_fin(ang * _DEG) * _n(phi)) * abs(math.cos(phi)))


def _ecef_x(lat, lon, hae):
    phi = _phi(lat)
    lam = _fin(lon * _DEG)
    return _fin(_fin(_fin(_n(phi) + hae) * math.cos(phi)) * math.cos(lam))


def _ecef_y(lat, lon, hae):
    phi = _phi(lat)
    lam = _fin(lon * _DEG)
    return _fin(_fin(_fin(_n(phi) + hae) * math.cos(phi)) * math.sin(lam))


def _ecef_z(lat, lon, hae):
    phi = _phi(lat)
    _fin(lon * _DEG)
    return _fin(_fin(_fin(_n(phi) * (1.0 - _E2)) + hae) * math.sin(phi))


def _heikkinen(x, y, z):
    """-> (φ in radians, h): Heikkinen's closed form in the order the spec
    states."""
    p = _sqrt(_fin(_fin(x * x) + _fin(y * y)))
    zz = _fin(z * z)
    F = _fin(54.0 * _B2 * zz)
    G = _fin(_fin(p * p) + _fin((1.0 - _E2) * zz) - _E2 * (_A2 - _B2))
    if G <= 0:  # ruling 2
        raise _Absent()
    c = _div(_fin(_E4 * F * p * p), _pow(G, 3))
    s = _cbrt(_fin(1.0 + c + _sqrt(_fin(c * c + 2.0 * c))))
    k = _fin(s + 1.0 + _div(1.0, s))
    P = _div(F, _fin(3.0 * k * k * G * G))
    Q = _sqrt(_fin(1.0 + 2.0 * _E4 * P))
    inner = _fin(
        _A2 / 2.0 * (1.0 + _div(1.0, Q))
        - _div(_fin(P * (1.0 - _E2) * zz), _fin(Q * (1.0 + Q)))
        - P * p * p / 2.0
    )
    if inner < 0:  # ruling 9: the square root in r0 takes a negative argument as 0
        inner = 0.0
    r0 = _fin(_div(-P * _E2 * p, 1.0 + Q) + math.sqrt(inner))
    d = _fin(p - _E2 * r0)
    U = _sqrt(_fin(d * d + zz))
    V = _sqrt(_fin(d * d + (1.0 - _E2) * zz))
    aV = _fin(_A * V)
    z0 = _div(_fin(_B2 * z), aV)
    h = _fin(U * (1.0 - _div(_B2, aV)))
    phi = math.atan2(_fin(z + (_A2 - _B2) / _B2 * z0), p)
    return _fin(phi), h


def _lat_of_ecef(x, y, z):
    return _fin(_heikkinen(x, y, z)[0] * _RAD)


def _hae_of_ecef(x, y, z):
    return _heikkinen(x, y, z)[1]


def _lon_of_ecef(x, y, z):
    if x == 0 and y == 0:
        return None
    d = _fin(math.atan2(y, x) * _RAD)
    if d <= -180.0:  # ruling 5: the range is (-180, 180]
        d = 180.0
    elif d > 180.0:
        d = 180.0
    return d


def _deg(rad):
    return _fin(rad * _RAD)


def _rad(deg):
    return _fin(deg * _DEG)


def _add(x0, x1):
    return _fin(x0 + x1)


def _sub(x0, x1):
    return _fin(x0 - x1)


# name -> (argument count, argument type, result type, function); `add` and
# `sub` are typed by their keys (ruling 4) and are not here.
TYPED = {
    "sec_of": (1, "int", "int", sec_of),
    "nsec_of": (1, "int", "int", nsec_of),
    "ms_of": (2, "int", "int", ms_of),
    "deg": (1, "float", "float", _guard(_deg)),
    "rad": (1, "float", "float", _guard(_rad)),
    "deg_lat_of": (2, "float", "float", _guard(_deg_lat_of)),
    "deg_lon_of": (2, "float", "float", _guard(_deg_lon_of)),
    "m_lat_of": (2, "float", "float", _guard(_m_lat_of)),
    "m_lon_of": (2, "float", "float", _guard(_m_lon_of)),
    "ecef_x": (3, "float", "float", _guard(_ecef_x)),
    "ecef_y": (3, "float", "float", _guard(_ecef_y)),
    "ecef_z": (3, "float", "float", _guard(_ecef_z)),
    "lat_of_ecef": (3, "float", "float", _guard(_lat_of_ecef)),
    "lon_of_ecef": (3, "float", "float", _guard(_lon_of_ecef)),
    "hae_of_ecef": (3, "float", "float", _guard(_hae_of_ecef)),
}

# `add` and `sub`: two arguments; the target and both arguments all `int` or
# all `float`.
ARITH = {
    "add": {"int": add_int, "float": _guard(_add)},
    "sub": {"int": sub_int, "float": _guard(_sub)},
}

ARG_COUNT = dict({name: spec[0] for name, spec in TYPED.items()}, add=2, sub=2)
