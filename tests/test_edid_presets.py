"""Check the shipped EDID presets against both PicoKVM capture bridges."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from edid_modes import RESERVED_PRODUCTS, catalogue  # noqa: E402

EDID = ROOT / 'edid'


def preferred(data):
    d = data[54:72]
    clock = int.from_bytes(d[:2], 'little') * 10
    width, height = d[2] | (d[4] >> 4) << 8, d[5] | (d[7] >> 4) << 8
    hblank, vblank = d[3] | (d[4] & 15) << 8, d[6] | (d[7] & 15) << 8
    return clock, width, height, clock * 1000 / ((width + hblank) * (height + vblank))


class EdidPresetTest(unittest.TestCase):
    def setUp(self):
        self.files = sorted(EDID.glob('*.edid.bin'))

    def test_catalogue(self):
        names = {f'{w}x{h}p60.edid.bin' for w, h, _ in catalogue('picokvm')}
        self.assertEqual({p.name for p in self.files}, names)
        # The shared factory/1080p60 preset comes from onekvm-distro.
        self.assertNotIn('1920x1080p60.edid.bin', names)

    def test_bytes(self):
        for path in self.files:
            data = path.read_bytes()
            self.assertEqual(len(data), 256, path)
            self.assertEqual(data[:8], bytes.fromhex('00ffffffffffff00'), path)
            self.assertEqual(data[126], 1, path)
            self.assertEqual(data[128:130], b'\x02\x03', path)
            self.assertEqual(sum(data[:128]) % 256, 0, path)
            self.assertEqual(sum(data[128:]) % 256, 0, path)

    def test_bridge_limits(self):
        # TC358743: 640x350 to 165 MHz; four 594 Mbps CSI lanes at 16 bit/pixel
        # with 10 % headroom. Both bridges feed a 1920x1080 encoder.
        for path in self.files:
            clock, width, height, hz = preferred(path.read_bytes())
            self.assertEqual(path.name, f'{width}x{height}p60.edid.bin')
            self.assertLess(abs(hz - 60), 1, path)
            self.assertTrue(640 <= width <= 1920 and 350 <= height <= 1080, path)
            self.assertLessEqual(clock, 165000, path)
            self.assertLessEqual(width * height * 60 * 16 * 110 // 100,
                                 4 * 594_000_000, path)

    def test_unique_products(self):
        # Windows caches EDIDs per PnP product ID: one ID per preset.
        products = {}
        for path in self.files:
            product = int.from_bytes(path.read_bytes()[10:12], 'little')
            self.assertNotIn(product, RESERVED_PRODUCTS, path)
            self.assertNotIn(product, products, path)
            products[product] = path.name


if __name__ == '__main__':
    unittest.main()
