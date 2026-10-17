# fwtool FAT subset

MIT code copied from the locally supplied ma1co/Sony-Firmware-Tools checkout.
Upstream checkout revision: `72fbdd15ddcc2d2a24ccb52cf8e84b8de98c738a` (read locally; no network used).
`LICENSE.txt` preserves its original license. Only `archive/fat.py`, `io`, `util`
and the `UnixFile` definition needed by FAT are included. These modules use only
Python's standard library; no Sony assets or Python caches are included.

Local changes: the archive initializer exposes only `UnixFile`, and `writeFat`
sorts directory entries so allocation/order no longer depend on Python hash seeds.
The importer pins UTC+8 for the historical identity image's FAT timestamps.
