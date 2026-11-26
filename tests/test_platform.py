"""Offline kernel helper tests; set ONEKVM_PICOKVM_KERNEL to a source tree."""

import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


KERNEL = os.environ.get("ONEKVM_PICOKVM_KERNEL")


def function(source, name):
    declaration = re.search(r"^static int " + name + r"\(", source, re.M)
    start = source.index("{", declaration.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[declaration.start():end]


@unittest.skipUnless(KERNEL, "set ONEKVM_PICOKVM_KERNEL for kernel helper tests")
class KernelHelpers(unittest.TestCase):
    def run_harness(self, harness):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work)
            (path / "test.c").write_text(harness)
            compiler = shlex.split(os.environ.get("CC", "cc"))
            subprocess.run(compiler + ["-std=gnu11", "-Wall", "-Wextra", "-Werror",
                                       str(path / "test.c"), "-o", str(path / "test")], check=True)
            subprocess.run([str(path / "test")], check=True)

    def test_otp_word_reads_and_failures(self):
        root = Path(KERNEL)
        source = (root / "drivers/nvmem/rockchip-otp.c").read_text()
        constants = "\n".join(re.findall(r"^#define OTPC_.*$", source, re.M))
        harness = r"""
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define BIT(shift) (1U << (shift))
#define GENMASK(high, low) ((UINT32_MAX >> (31 - (high))) & (UINT32_MAX << (low)))
#define min_t(type, left, right) ((type)(left) < (type)(right) ? (type)(left) : (type)(right))
struct rockchip_otp { void *base; };
static u32 registers[1025];
static u8 contents[64];
static unsigned int word, reads, fail_read;
static int ecc_error;
static int rockchip_otp_ecc_enable(struct rockchip_otp *otp, bool enable)
{
    assert(otp->base == registers && enable);
    return ecc_error;
}
static void udelay(unsigned int delay) { assert(delay == 5); }
"""
        harness += constants + r"""
static void writel(u32 value, void *address)
{
    *(u32 *)address = value;
    if (address == (void *)registers + OTPC_USER_ADDR)
        word = value & 0xffff;
}
static u32 readl(void *address)
{
    assert(address == (void *)registers + OTPC_USER_Q);
    assert(word * 2 + 1 < sizeof(contents));
    return contents[word * 2] | (contents[word * 2 + 1] << 8);
}
static int rockchip_otp_wait_status(struct rockchip_otp *otp, unsigned int reg, u32 flag)
{
    assert(otp->base == registers && reg == OTPC_INT_STATUS && flag == OTPC_USER_DONE);
    return ++reads == fail_read ? -ETIMEDOUT : 0;
}
"""
        harness += function(source, "rv1106_otp_read") + r"""
int main(void)
{
    struct rockchip_otp otp = { .base = registers };
    u8 output[66];
    for (unsigned int index = 0; index < sizeof(contents); index++)
        contents[index] = index * 3 + 1;
    for (unsigned int offset = 0; offset < sizeof(contents); offset++) {
        for (size_t bytes = 0; bytes <= sizeof(contents) - offset; bytes++) {
            memset(output, 0xa5, sizeof(output));
            reads = 0;
            assert(rv1106_otp_read(&otp, offset, output + 1, bytes) == 0);
            assert(!memcmp(output + 1, contents + offset, bytes));
            assert(output[0] == 0xa5 && output[bytes + 1] == 0xa5);
            assert(registers[OTPC_USER_CTRL / 4] == OTPC_USE_USER_MASK);
        }
    }
    ecc_error = -EIO;
    assert(rv1106_otp_read(&otp, 10, output, 16) == -EIO);
    ecc_error = 0;
    for (unsigned int failure = 1; failure <= 8; failure++) {
        reads = 0;
        fail_read = failure;
        assert(rv1106_otp_read(&otp, 10, output, 16) == -ETIMEDOUT);
        assert(registers[OTPC_USER_CTRL / 4] == OTPC_USE_USER_MASK);
    }
    return 0;
}
"""
        self.run_harness(harness)

    def test_mac_gating_errors_and_local_unicast_bits(self):
        source = (Path(KERNEL) / "drivers/net/ethernet/stmicro/stmmac/dwmac-rk.c").read_text()
        harness = r"""
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
#define SHA256_DIGEST_SIZE 32
#define ETH_ALEN 6
#define EPROBE_DEFER 517
#define IS_ERR(pointer) ((uintptr_t)(pointer) >= (uintptr_t)-4095)
#define PTR_ERR(pointer) ((int)(intptr_t)(pointer))
#define ERR_PTR(error) ((void *)(intptr_t)(error))
struct device { void *of_node; };
struct nvmem_cell { int unused; };
static struct nvmem_cell cell;
static int compatible = 1, named = 1, get_error, read_error, calls;
static size_t id_length = 16;
static u8 id[16], digest_first;
static int of_device_is_compatible(void *node, const char *name)
{
    (void)node;
    assert(!strcmp(name, "rockchip,rv1106-gmac"));
    return compatible;
}
static int of_property_match_string(void *node, const char *property, const char *name)
{
    (void)node;
    assert(!strcmp(property, "nvmem-cell-names") && !strcmp(name, "cpu-id"));
    return named ? 0 : -EINVAL;
}
static int is_valid_ether_addr(const u8 *mac)
{
    return !(mac[0] & 1) && memcmp(mac, (u8[ETH_ALEN]){0}, ETH_ALEN);
}
static struct nvmem_cell *nvmem_cell_get(struct device *dev, const char *name)
{
    (void)dev;
    assert(!strcmp(name, "cpu-id"));
    calls++;
    return get_error ? ERR_PTR(get_error) : &cell;
}
static void *nvmem_cell_read(struct nvmem_cell *entry, size_t *length)
{
    assert(entry == &cell);
    *length = id_length;
    return read_error ? ERR_PTR(read_error) : id;
}
static void nvmem_cell_put(struct nvmem_cell *entry) { assert(entry == &cell); }
static int dev_err_probe(struct device *dev, int error, const char *message)
{
    (void)dev;
    (void)message;
    return error;
}
static void kfree(void *pointer) { assert(pointer == id); }
static void *memchr_inv(const void *buffer, int value, size_t length)
{
    const u8 *bytes = buffer;
    for (size_t index = 0; index < length; index++)
        if (bytes[index] != (u8)value) return (void *)(bytes + index);
    return NULL;
}
static void sha256(const u8 *buffer, size_t length, u8 *digest)
{
    assert(buffer == id && length == 16);
    for (unsigned int index = 0; index < SHA256_DIGEST_SIZE; index++)
        digest[index] = digest_first + index;
}
"""
        harness += function(source, "rk_gmac_otp_mac_address") + r"""
int main(void)
{
    struct device dev = {0};
    u8 mac[ETH_ALEN] = {0};
    compatible = 0;
    assert(rk_gmac_otp_mac_address(&dev, mac) == 0 && calls == 0);
    compatible = 1;
    named = 0;
    assert(rk_gmac_otp_mac_address(&dev, mac) == 0 && calls == 0);
    named = 1;
    mac[0] = 2;
    assert(rk_gmac_otp_mac_address(&dev, mac) == 0 && calls == 0 && mac[0] == 2);
    memset(mac, 0, sizeof(mac));
    get_error = -EPROBE_DEFER;
    assert(rk_gmac_otp_mac_address(&dev, mac) == -EPROBE_DEFER);
    get_error = 0;
    read_error = -ETIMEDOUT;
    assert(rk_gmac_otp_mac_address(&dev, mac) == -ETIMEDOUT);
    read_error = 0;
    assert(rk_gmac_otp_mac_address(&dev, mac) == -EINVAL);
    memset(id, 0xff, sizeof(id));
    assert(rk_gmac_otp_mac_address(&dev, mac) == -EINVAL);
    memset(id, 0x12, sizeof(id));
    id_length = 15;
    assert(rk_gmac_otp_mac_address(&dev, mac) == -EINVAL);
    id_length = 16;
    for (unsigned int first = 0; first < 256; first++) {
        memset(mac, 0, sizeof(mac));
        digest_first = first;
        assert(rk_gmac_otp_mac_address(&dev, mac) == 0);
        assert(mac[0] == ((first & 0xfc) | 2));
        for (unsigned int index = 1; index < ETH_ALEN; index++)
            assert(mac[index] == (u8)(first + index));
    }
    return 0;
}
"""
        self.run_harness(harness)


if __name__ == "__main__":
    unittest.main()
