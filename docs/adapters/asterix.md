# ASTERIX category 062

The `asterix-cat062` adapter writes each track as one ASTERIX data block holding one category 062 (system track data) record, and sends it as one UDP datagram, unicast or multicast. The encoding follows EUROCONTROL's CAT062 specification, edition 1.21 ([EUROCONTROL publication page](https://www.eurocontrol.int/publication/cat062-eurocontrol-specification-surveillance-data-exchange-asterix-part-9-category-062)).

| Part | Status |
|---|---|
| encoder for the items below | built |
| every record decoded by asterix4py 1.0.6 (MIT, its CAT062 edition 1.18 description) and compared field by field, for the two example tracks and edge positions (poles, ±180°, end of day) | measured (`tests/test_asterix.py`) |
| UDP delivery | measured against a listener on one machine |

## Items

| Item | Written | Value |
|---|---|---|
| I062/010 data source identifier | always (mandatory) | the destination's `sac` and `sic` |
| I062/015 service identification | when the destination gives `service_id` | |
| I062/070 time of track information | always (mandatory) | the track's time, 1/128 s since UTC midnight |
| I062/105 position in WGS-84 | always | latitude and longitude, LSB 180/2²⁵ degree |
| I062/185 velocity, Cartesian | when the mapping has `motion` | Vx east, Vy north, LSB 0.25 m/s |
| I062/040 track number | always (mandatory) | `asterix.track_number`, or a number per uid in the order uids are first seen |
| I062/080 track status | always (mandatory) | MON (monosensor), MRH (geometric) when an altitude is sent, SRC from `asterix.altitude_source`, SIM from `asterix.simulated` |
| I062/130 geometric altitude | when the mapping gives an altitude | metres above the ellipsoid in feet, LSB 6.25 ft |

The name is not written: the specification's note to I062/245 says that item shall not be used. A value outside an item's range (an altitude above 150 000 ft, a speed beyond 8192 m/s, a track number above 65535) refuses the record.

## Worked example

The weather-station Position record ([worked example](../mapping.md#worked-example)), with `sac: 0`, `sic: 0`:

```text
3e 0019 99 0d 10  0000  6a4180  00b86d72 fe5bd433  0001  a5 80  0048
CAT LEN  FSPEC     010   070     105 lat  105 lon   040   080    130
```

| Item | Bytes | Decoded by asterix4py |
|---|---|---|
| 070 | `6a4180` | 54403 s: 15:06:43 |
| 105 | `00b86d72 fe5bd433` | 64.83780026°, -147.71640122° |
| 040 | `0001` | track 1 |
| 080 | `a5 80` | MON 1, MRH 1, SRC 1 (GNSS), CNF 0, SIM 1 |
| 130 | `0048` | 450 ft (136.5 m) |

## Destination and block

```yaml
  - name: asterix-consumer
    adapter: asterix-cat062
    address: "239.255.10.62:8600"   # the group or host its owner chose
    sac: 25
    sic: 4
    service_id: 1                   # optional
    ttl: 1

# in a mapping file
asterix:
  track_number: { field: buoy_number }   # optional
  altitude_source: gnss                  # none (default), gnss, 3d-radar, triangulation, multilateration, ...
  simulated: true
```

The specification is EUROCONTROL's and is linked, not copied. CroatiaControl's `asterix` decoder (GPL-2.0) can be used as an external tool; it is not a dependency.
