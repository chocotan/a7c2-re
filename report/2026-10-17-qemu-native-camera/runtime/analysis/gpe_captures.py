"""Select bounded source spans from captured BitBlit parameters; no pixel interpretation."""
import re
import struct


def raster_sources(text):
    sources = {}
    for hex_data in re.findall(r'\bGPE_PARAMETERS ([0-9a-f]+)', text):
        if len(hex_data) != 288:
            continue
        p = struct.unpack('<36I', bytes.fromhex(hex_data))
        address = p[14] | p[15] << 32
        x, y, width, height, stride, fmt = p[16], p[17], p[18], p[19], p[22], p[23]
        bpp = {0: 2, 1: 4, 2: 1}.get(fmt, 0)
        if not (bpp and 0 < width <= 8192 and 0 < height <= 8192 and 0 < stride <= 16384
                and x + width <= stride and y <= 16383):
            continue
        size = ((y + height - 1) * stride + x + width) * bpp
        if not (0 < size <= 0x1000000 and 0x410000000 <= address <= 0x500000000 - size):
            continue
        key = (address, fmt, stride)
        if key not in sources:
            sources[key] = {'address': address, 'format': fmt, 'row_pixels': stride, 'bytes': size}
        sources[key]['bytes'] = max(sources[key]['bytes'], size)
    return list(sources.values())


if __name__ == '__main__':
    p = [0] * 36
    p[14], p[15] = 0x10e26400, 4
    p[16], p[17], p[18], p[19], p[22], p[23] = 8, 4, 32, 16, 64, 0
    def line(words):
        return 'GPE_PARAMETERS ' + struct.pack('<36I', *words).hex()
    expected = {'address': 0x410e26400, 'format': 0, 'row_pixels': 64, 'bytes': 2512}
    assert raster_sources(line(p)) == [expected]
    assert raster_sources(line(p) + '\n' + line(p)) == [expected]
    for index, value in ((18, 0), (19, 0xffffffff), (22, 16), (23, 3), (15, 0)):
        bad = p.copy(); bad[index] = value
        assert not raster_sources(line(bad))
    assert not raster_sources('GPE_PARAMETERS 00')
    print('PASS: source span, de-duplication, bounds and malformed capture rejection')
