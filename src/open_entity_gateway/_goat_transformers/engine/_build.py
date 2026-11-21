"""The wires of this build (spec/engine-spec.md, Version 0.24, ruling 1).

The source tree has all seven. `engines/python/build.py --wires <set>` writes
a package that leaves out the codec modules of every other wire and replaces
this file with the set it kept."""

WIRES = ("xml", "geojson", "json", "bits", "protobuf", "text", "tlv")
