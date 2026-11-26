// SPDX-License-Identifier: MIT
#define main splash_cli_main
#include "../boot-splash/onekvm-boot-splash.c"
#undef main
#include <assert.h>

static unsigned fill_width(const uint16_t *frame)
{
    unsigned width = 0;
    for (unsigned x = 0; x < BOOT_BAR_WIDTH; x++)
        width += frame[(BOOT_BAR_Y + 1) * WIDTH + BOOT_BAR_X + x] == bar_fill();
    return width;
}

static int stage_index(const char *name)
{
    for (int i = 0; i < BOOT_STAGES; i++)
        if (!strcmp(boot_stages[i].name, name))
            return i;
    assert(!"unknown checkpoint");
    return -1;
}

static struct fb_var_screeninfo panel_var(void)
{
    struct fb_var_screeninfo var = {
        .xres = WIDTH, .yres = HEIGHT, .xres_virtual = WIDTH, .yres_virtual = HEIGHT,
        .bits_per_pixel = 16, .nonstd = 1,
        .red = { 11, 5, 0 }, .green = { 5, 6, 0 }, .blue = { 0, 5, 0 },
    };
    return var;
}

static struct fb_fix_screeninfo panel_fix(size_t stride)
{
    struct fb_fix_screeninfo fix = {
        .type = FB_TYPE_PACKED_PIXELS, .visual = FB_VISUAL_TRUECOLOR,
        .line_length = (uint32_t)stride, .smem_len = (uint32_t)(stride * HEIGHT),
    };
    strncpy(fix.id, "fb_st7789v", sizeof(fix.id));
    return fix;
}

/* Only the 240x240 RGB565 ST7789V framebuffer is accepted. */
static void check_panel(void)
{
    struct fb_var_screeninfo var = panel_var();
    struct fb_fix_screeninfo fix = panel_fix(480);

    assert(valid_panel(&var, &fix));
    fix.line_length = 512;
    fix.smem_len = 512 * HEIGHT;
    assert(valid_panel(&var, &fix));
    strncpy(fix.id, "fb_st7789p3", sizeof(fix.id));
    assert(!valid_panel(&var, &fix));
    var.nonstd = 0;
    assert(valid_panel(&var, &fix));
    fix = panel_fix(480);
    var = panel_var();
    var.yres = 284;
    assert(!valid_panel(&var, &fix));
    var = panel_var();
    var.bits_per_pixel = 32;
    assert(!valid_panel(&var, &fix));
    var = panel_var();
    var.red.offset = 0;
    var.blue.offset = 11;
    assert(!valid_panel(&var, &fix));
    var = panel_var();
    fix.smem_len = 480 * HEIGHT - 2;
    assert(!valid_panel(&var, &fix));
}

/* Canvas rows are framebuffer rows; padding after each row is left alone. */
static void check_layout(size_t stride)
{
    uint16_t frame[WIDTH * HEIGHT] = {0}, back[WIDTH * HEIGHT];
    size_t size = stride * HEIGHT;
    uint8_t *pixels = malloc(size);
    assert(pixels);
    memset(pixels, 0xa5, size);
    frame[0] = 0x1234;
    frame[WIDTH - 1] = 0x5678;
    frame[(HEIGHT - 1) * WIDTH] = 0x9abc;
    frame_to_panel(pixels, stride, frame);
    assert(pixels[0] == 0x34 && pixels[1] == 0x12);
    assert(pixels[(WIDTH - 1) * 2] == 0x78 && pixels[(WIDTH - 1) * 2 + 1] == 0x56);
    assert(pixels[(HEIGHT - 1) * stride] == 0xbc && pixels[(HEIGHT - 1) * stride + 1] == 0x9a);
    for (unsigned y = 0; y < HEIGHT; y++)
        for (size_t x = WIDTH * 2; x < stride; x++)
            assert(pixels[y * stride + x] == 0xa5);
    panel_to_frame(back, pixels, stride);
    assert(!memcmp(frame, back, sizeof(frame)));
    free(pixels);
}

/* The logo, caption and bar stay inside the screen and do not overlap. */
static void check_render(void)
{
    uint16_t frame[WIDTH * HEIGHT];

    assert(logo_boot.width <= WIDTH && starting.width <= WIDTH);
    assert(LOGO_Y + logo_boot.height < CAPTION_Y);
    assert(CAPTION_Y + starting.height < BOOT_BAR_Y);
    assert(BOOT_BAR_Y + BAR_HEIGHT <= TOP_PAD + LAYOUT_HEIGHT);
    assert(BOOT_BAR_X >= 1 && BOOT_BAR_X + BOOT_BAR_WIDTH + 1 <= WIDTH);
    render(frame, 0);
    for (unsigned x = 0; x < WIDTH; x++) {
        assert(frame[x] == background());
        assert(frame[(HEIGHT - 1) * WIDTH + x] == background());
    }
    assert(has_boot_bar(frame) && fill_width(frame) == 0);
    render(frame, 4);
    assert(has_boot_bar(frame) && fill_width(frame) == percent_width(4));
}

static void check_boot_progress(size_t stride)
{
    uint16_t start[WIDTH * HEIGHT], later[WIDTH * HEIGHT], scratch[WIDTH * HEIGHT];
    size_t size = stride * HEIGHT, band, first = boot_bar_band(stride, &band);
    uint8_t *pixels = malloc(size), *expected = malloc(size), *before = malloc(size);
    assert(pixels && expected && before);

    render(start, 5);
    render(later, 60);
    assert(fill_width(start) == BOOT_BAR_WIDTH * 5 / 100);
    assert(fill_width(later) == BOOT_BAR_WIDTH * 60 / 100);
    render(scratch, 100);
    assert(fill_width(scratch) == BOOT_BAR_WIDTH);
    for (unsigned y = 0; y < HEIGHT; y++)
        for (unsigned x = 0; x < WIDTH; x++)
            if (y < BOOT_BAR_Y || y >= BOOT_BAR_Y + BAR_HEIGHT ||
                x < BOOT_BAR_X || x >= BOOT_BAR_X + BOOT_BAR_WIDTH)
                assert(start[y * WIDTH + x] == later[y * WIDTH + x]);

    /* A bar-only update keeps the caption and touches only the bar rows. */
    memset(pixels, 0xa5, size);
    frame_to_panel(pixels, stride, start);
    memcpy(before, pixels, size);
    memset(expected, 0xa5, size);
    frame_to_panel(expected, stride, later);
    assert(update_boot_bar(pixels, stride, scratch, 60) == 1);
    assert(!memcmp(pixels, expected, size));
    assert(!memcmp(pixels, before, first));
    assert(!memcmp(pixels + first + band, before + first + band, size - first - band));

    /* Without a boot bar, for example over an empty framebuffer, nothing changes. */
    memset(pixels, 0, size);
    assert(update_boot_bar(pixels, stride, scratch, 60) == 0);
    for (size_t i = 0; i < size; i++)
        assert(!pixels[i]);

    assert(parse_percent("0") == 0 && parse_percent("100") == 100 && parse_percent("085") == 85);
    assert(parse_percent("storage") == boot_stages[stage_index("storage")].percent);
    assert(parse_percent("screen") == boot_stages[stage_index("screen")].percent);
    assert(parse_percent("done") == 100);
    assert(parse_percent("Screen") < 0 && parse_percent("later") < 0);
    assert(parse_percent("") < 0 && parse_percent("101") < 0 && parse_percent("-1") < 0 &&
           parse_percent("5%") < 0 && parse_percent("1000") < 0);
    {
        char *bad[] = { "splash", "--output", "/dev/null", "progress", "50", NULL };
        assert(splash_cli_main(5, bad) == 2);
        char *range[] = { "splash", "--progress", "200", "boot", NULL };
        assert(splash_cli_main(4, range) == 2);
        char *missing[] = { "splash", "progress", NULL };
        assert(splash_cli_main(2, missing) == 2);
        char *install[] = { "splash", "install", "1", NULL };
        assert(splash_cli_main(3, install) == 2);
        /* A message screen needs its text. */
        char *blank[] = { "splash", "--output", "/dev/null", "message", NULL };
        assert(splash_cli_main(4, blank) == 2);
        char *empty[] = { "splash", "--message", "", "--output", "/dev/null", "message", NULL };
        assert(splash_cli_main(6, empty) == 2);
        char *text[] = { "splash", "--message", "Installing", "--output", "/dev/null", "message", NULL };
        assert(splash_cli_main(6, text) == 0);
    }
    free(pixels);
    free(expected);
    free(before);
}

static void write_file(int fd, const uint8_t *pixels, size_t size)
{
    assert(pwrite(fd, pixels, size, 0) == (ssize_t)size);
}

static void read_file(int fd, uint8_t *pixels, size_t size)
{
    assert(pread(fd, pixels, size, 0) == (ssize_t)size);
}

static void check_animation(size_t stride)
{
    uint16_t frame[WIDTH * HEIGHT], expected[WIDTH * HEIGHT];
    size_t size = stride * HEIGHT, band, first = boot_bar_band(stride, &band);
    uint8_t *pixels = malloc(size), *before = malloc(size);
    char path[] = "/tmp/onekvm-splash-fb-XXXXXX";
    int fd = mkstemp(path), basic = stage_index("basic"), screen = stage_index("screen");
    assert(pixels && before && fd >= 0);
    unlink(path);

    /* Stage table: rising checkpoints; easing stays short of the next one. */
    for (int i = 0; i + 1 < BOOT_STAGES; i++) {
        unsigned next = percent_width(boot_stages[i + 1].percent), last = 0;
        assert(boot_stages[i].percent < boot_stages[i + 1].percent && boot_stages[i].next_ms);
        assert(boot_stage(percent_width(boot_stages[i].percent)) == i);
        assert(boot_stage(next - 1) == i);
        for (unsigned long ms = 0; ms <= 600000; ms = ms ? ms * 2 : 10) {
            unsigned target = boot_stage_target(i, ms);
            assert(target >= last && target < next);
            last = target;
        }
    }
    assert(boot_stages[BOOT_STAGES - 1].percent == 100 && boot_stage(0) == -1);
    /* U-Boot draws 2% and 4%; the initramfs, the root-system units and the
     * screen service name the rest in this order. */
    assert(!strcmp(boot_stages[0].name, "initramfs") && boot_stages[0].percent > 4);
    assert(stage_index("storage") == 1 && stage_index("early") == 2 &&
           stage_index("sysinit") == 3 && basic == 4 && screen == 5);

    /* Painted columns match a fully rendered bar of the same width. */
    render(frame, 15);
    memset(pixels, 0xa5, size);
    frame_to_panel(pixels, stride, frame);
    paint_bar(pixels, stride, percent_width(15) - 2, 70);
    panel_to_frame(frame, pixels, stride);
    render(expected, 0);
    for (unsigned y = 0; y < BAR_HEIGHT; y++)
        for (unsigned x = 0; x < BOOT_BAR_WIDTH; x++)
            expected[(BOOT_BAR_Y + y) * WIDTH + BOOT_BAR_X + x] = boot_bar_pixel(x, y, 70);
    assert(!memcmp(frame, expected, sizeof(frame)));

    /* The animator moves the bar forward, never past the next checkpoint,
     * and writes only the bar rows. */
    render(frame, boot_stages[screen].percent);
    memset(pixels, 0xa5, size);
    frame_to_panel(pixels, stride, frame);
    write_file(fd, pixels, size);
    memcpy(before, pixels, size);
    stop_animation = 0;
    assert(animate_fd(fd, stride, 1500) == 0);
    read_file(fd, pixels, size);
    panel_to_frame(frame, pixels, stride);
    {
        unsigned fill = boot_bar_row_fill(frame + (BOOT_BAR_Y + 1) * WIDTH);
        assert(fill > percent_width(boot_stages[screen].percent) &&
               fill < percent_width(boot_stages[screen + 1].percent));
        assert(!memcmp(pixels, before, first));
        assert(!memcmp(pixels + first + band, before + first + band, size - first - band));
    }

    /* A checkpoint never shortens a longer bar. */
    assert(update_boot_bar(pixels, stride, frame, boot_stages[screen].percent) == 0);
    assert(update_boot_bar(pixels, stride, frame, boot_stages[screen + 1].percent) == 1);

    /* Without a boot bar the animator stops at once and writes nothing. */
    memset(pixels, 0, size);
    write_file(fd, pixels, size);
    assert(animate_fd(fd, stride, 5000) == 0);
    read_file(fd, pixels, size);
    for (size_t i = 0; i < size; i++)
        assert(!pixels[i]);

    close(fd);
    free(pixels);
    free(before);
}

static unsigned text_rows(const uint16_t *frame)
{
    unsigned rows = 0;
    for (unsigned row = 0; row < RECOVERY_ROWS; row++) {
        unsigned top = RECOVERY_TEXT_Y + row * (LOG_FONT_HEIGHT + 1);
        int lit = 0;
        for (unsigned y = top; y < top + LOG_FONT_HEIGHT && !lit; y++)
            for (unsigned x = 0; x < WIDTH && !lit; x++)
                lit = frame[y * WIDTH + x] == rgb565(234, 238, 245);
        rows += lit;
    }
    return rows;
}

/* Recovery: the logo, then one row per line, wrapped and cut to the panel. */
static void check_recovery(void)
{
    static uint16_t frame[WIDTH * HEIGHT];
    char many[256] = "";

    assert(RECOVERY_COLUMNS == 30 && RECOVERY_ROWS >= 10);
    assert(RECOVERY_TEXT_Y + RECOVERY_ROWS * (LOG_FONT_HEIGHT + 1) <= HEIGHT);
    render_recovery(frame, NULL);
    assert(text_rows(frame) == 1);
    for (unsigned y = 0; y < RECOVERY_LOGO_Y; y++)
        for (unsigned x = 0; x < WIDTH; x++)
            assert(frame[y * WIDTH + x] == background());
    render_recovery(frame, "Recovery mode\nhttps://192.168.100.200/\n\nUSB: the whole SD card is a drive.");
    /* The empty line stays empty; the long one wraps into two rows. */
    assert(text_rows(frame) == 4);
    for (unsigned i = 0; i < 20; i++)
        strcat(many, "line\n");
    render_recovery(frame, many);
    assert(text_rows(frame) == RECOVERY_ROWS);
    /* A word longer than a row is split instead of looping. */
    render_recovery(frame, "0123456789012345678901234567890123456789");
    assert(text_rows(frame) == 2);
}

int main(void)
{
    check_panel();
    check_recovery();
    check_layout(480);
    check_layout(512);
    check_render();
    check_boot_progress(480);
    check_boot_progress(512);
    check_animation(480);
    check_animation(512);
    puts("PicoKVM boot splash format, layout, progress, animation and Recovery tests passed");
    return 0;
}
