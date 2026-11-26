// SPDX-License-Identifier: MIT
/*
 * PicoKVM boot screen and boot bar on the 240x240 ST7789V framebuffer, and
 * the Recovery screen. Adapted from onekvm-nanokvm-go-machine; the PicoKVM
 * panel is not rotated.
 */
#define _POSIX_C_SOURCE 200809L
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

/* The canvas is the panel: 240x240, rows top to bottom. */
enum { WIDTH = 240, HEIGHT = 240 };
#define PANEL_FBTFT_ID "fb_st7789v"
/* Same layout as the Go: 172 rows, centered vertically. */
enum { LAYOUT_HEIGHT = 172, TOP_PAD = (HEIGHT - LAYOUT_HEIGHT) / 2 };
enum { LOGO_Y = TOP_PAD + 48, CAPTION_Y = TOP_PAD + 125 };
enum { BAR_HEIGHT = 4, BOOT_BAR_WIDTH = 160, BOOT_BAR_X = (WIDTH - BOOT_BAR_WIDTH) / 2,
       BOOT_BAR_Y = TOP_PAD + 150 };

struct splash_asset {
    unsigned width, height;
    const uint16_t *runs;
    size_t words;
};

#include "assets.h"
#include "log-font.h"

static uint16_t rgb565(unsigned r, unsigned g, unsigned b)
{
    return (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
}

static void draw_asset(uint16_t *frame, const struct splash_asset *asset, unsigned y)
{
    size_t pixel = 0;
    /* Wider artwork is clipped at the right edge instead of wrapping. */
    unsigned left = asset->width < WIDTH ? (WIDTH - asset->width) / 2 : 0;

    for (size_t i = 0; i + 1 < asset->words; i += 2) {
        for (unsigned n = 0; n < asset->runs[i]; n++, pixel++) {
            unsigned row = (unsigned)(pixel / asset->width) + y;
            unsigned col = (unsigned)(pixel % asset->width) + left;
            if (row < HEIGHT && col < WIDTH && pixel < asset->width * asset->height)
                frame[row * WIDTH + col] = asset->runs[i + 1];
        }
    }
}

static uint16_t background(void) { return rgb565(8, 11, 17); }
static uint16_t bar_track(void) { return rgb565(31, 41, 59); }
static uint16_t bar_fill(void) { return rgb565(22, 119, 255); }

/* Pixel x of boot bar row y when `fill` pixels are filled; ends are rounded. */
static uint16_t boot_bar_pixel(unsigned x, unsigned y, unsigned fill)
{
    unsigned inset = (y == 0 || y == BAR_HEIGHT - 1) ? 2 : 0;
    if (x >= inset && x + inset < fill)
        return bar_fill();
    if (x >= inset && x + inset < BOOT_BAR_WIDTH)
        return bar_track();
    return background();
}

static unsigned percent_width(unsigned percent)
{
    return BOOT_BAR_WIDTH * percent / 100;
}

static void draw_boot_bar(uint16_t *frame, unsigned percent)
{
    for (unsigned y = 0; y < BAR_HEIGHT; y++)
        for (unsigned x = 0; x < BOOT_BAR_WIDTH; x++)
            frame[(BOOT_BAR_Y + y) * WIDTH + BOOT_BAR_X + x] =
                boot_bar_pixel(x, y, percent_width(percent));
}

/* `row` is the bar's middle row. Only a boot screen drawn by this program or
 * U-Boot carries the bar. */
static int boot_bar_row_valid(const uint16_t *row)
{
    uint16_t first = row[BOOT_BAR_X], last = row[BOOT_BAR_X + BOOT_BAR_WIDTH - 1];
    return row[BOOT_BAR_X - 1] == background() &&
        row[BOOT_BAR_X + BOOT_BAR_WIDTH] == background() &&
        (first == bar_track() || first == bar_fill()) &&
        (last == bar_track() || last == bar_fill());
}

static unsigned boot_bar_row_fill(const uint16_t *row)
{
    unsigned fill = 0;
    while (fill < BOOT_BAR_WIDTH && row[BOOT_BAR_X + fill] == bar_fill())
        fill++;
    return fill;
}

static int has_boot_bar(const uint16_t *frame)
{
    return boot_bar_row_valid(frame + (BOOT_BAR_Y + 1) * WIDTH);
}

/*
 * Named checkpoints draw the bar; the screen service's first frame replaces
 * it. Between checkpoints `animate` moves steadily toward the next one over
 * its usual duration, then slows and never reaches it. Percentages follow a
 * normal boot from the U-Boot boot screen (0%) to the first screen-service
 * frame.
 *
 * U-Boot fills 2% and 4% while loading the slot (picokvm_lcd.c), then the
 * initramfs draws `initramfs`, starts the animator and marks `storage` once
 * the slot root is mounted; root-system units mark early, sysinit and basic,
 * and the screen service marks screen when it opens the panel.
 */
static const struct {
    const char *name;
    unsigned char percent;
    unsigned short next_ms;
} boot_stages[] = {
    { "initramfs", 17, 500 }, { "storage", 18, 4200 }, { "early", 25, 30000 },
    { "sysinit", 84, 500 }, { "basic", 85, 800 }, { "screen", 86, 7000 },
    { "done", 100, 0 },
};
enum { BOOT_STAGES = sizeof(boot_stages) / sizeof(boot_stages[0]) };

/* Index of the checkpoint the bar has reached, or -1 before the first. */
static int boot_stage(unsigned fill)
{
    int stage = -1;
    for (int i = 0; i < BOOT_STAGES; i++)
        if (fill >= percent_width(boot_stages[i].percent))
            stage = i;
    return stage;
}

/* Fill width `elapsed_ms` after reaching checkpoint `stage`: 90% of the way at
 * the usual time, then ever slower, never past 98%. */
static unsigned boot_stage_target(int stage, unsigned long elapsed_ms)
{
    unsigned base = percent_width(boot_stages[stage].percent);
    unsigned span = percent_width(boot_stages[stage + 1].percent) - base;
    unsigned long long usual = boot_stages[stage].next_ms, permille;

    if (elapsed_ms <= usual) {
        permille = 900 * elapsed_ms / usual;
    } else {
        unsigned long long late = elapsed_ms - usual;
        permille = 900 + 80 * late / (late + usual);
    }
    return base + (unsigned)(span * permille / 1000);
}

/*
 * Recovery: the logo above up to RECOVERY_ROWS lines of text. A newline
 * starts a line; longer lines wrap at spaces. Text that does not fit ends
 * in "...".
 */
enum { RECOVERY_LOGO_Y = 24, RECOVERY_TEXT_Y = 92, RECOVERY_LEFT = 12,
       RECOVERY_COLUMNS = (WIDTH - 2 * RECOVERY_LEFT) / LOG_FONT_WIDTH,
       RECOVERY_ROWS = (HEIGHT - RECOVERY_TEXT_Y - 8) / (LOG_FONT_HEIGHT + 1) };

static void draw_glyph(uint16_t *frame, unsigned row, unsigned col, unsigned char c)
{
    if (c < 32 || c > 126)
        c = '?';
    for (unsigned y = 0; y < LOG_FONT_HEIGHT; y++)
        for (unsigned x = 0; x < LOG_FONT_WIDTH; x++)
            if (log_font[c - 32][y] & (1u << x))
                frame[(RECOVERY_TEXT_Y + row * (LOG_FONT_HEIGHT + 1) + y) * WIDTH +
                      RECOVERY_LEFT + col * LOG_FONT_WIDTH + x] = rgb565(234, 238, 245);
}

static void draw_text(uint16_t *frame, const char *text)
{
    unsigned row = 0;

    while (*text && row < RECOVERY_ROWS) {
        size_t line = strcspn(text, "\n"), count = line;
        int last = row == RECOVERY_ROWS - 1;

        if (count > RECOVERY_COLUMNS) {
            count = RECOVERY_COLUMNS;
            while (count && text[count] != ' ')
                count--;
            if (!count)
                count = RECOVERY_COLUMNS;
        }
        /* Mark text that the last row cannot hold. */
        int cut = last && (count < line || text[line]);
        for (size_t col = 0; col < count; col++)
            draw_glyph(frame, row, (unsigned)col,
                       cut && col + 3 >= RECOVERY_COLUMNS ? '.' : (unsigned char)text[col]);
        if (cut)
            for (size_t col = count; col < RECOVERY_COLUMNS && col + 3 >= RECOVERY_COLUMNS; col++)
                draw_glyph(frame, row, (unsigned)col, '.');
        text += count;
        while (*text == ' ')
            text++;
        if (*text == '\n' && count == line)
            text++;
        row++;
    }
}

static void render_recovery(uint16_t *frame, const char *message)
{
    for (unsigned i = 0; i < WIDTH * HEIGHT; i++)
        frame[i] = background();
    draw_asset(frame, &logo_boot, RECOVERY_LOGO_Y);
    draw_text(frame, message && *message ? message : "Recovery");
}

/* The boot screen: logo, caption and the bar at `progress` percent. */
static void render(uint16_t *frame, unsigned progress)
{
    for (unsigned i = 0; i < WIDTH * HEIGHT; i++)
        frame[i] = background();
    draw_asset(frame, &logo_boot, LOGO_Y);
    draw_asset(frame, &starting, CAPTION_Y);
    draw_boot_bar(frame, progress);
}

static int valid_panel(const struct fb_var_screeninfo *var,
                       const struct fb_fix_screeninfo *fix)
{
    return var->xres == WIDTH && var->yres == HEIGHT &&
        var->xres_virtual >= WIDTH && var->yres_virtual >= HEIGHT &&
        var->xoffset == 0 && var->yoffset == 0 && var->bits_per_pixel == 16 &&
        var->red.offset == 11 && var->red.length == 5 && !var->red.msb_right &&
        var->green.offset == 5 && var->green.length == 6 && !var->green.msb_right &&
        var->blue.offset == 0 && var->blue.length == 5 && !var->blue.msb_right &&
        /* fbtft sets nonstd=1 even though these bitfields describe RGB565. */
        var->transp.length == 0 && !var->grayscale &&
        (!var->nonstd || (var->nonstd == 1 &&
         !strncmp(fix->id, PANEL_FBTFT_ID, sizeof(fix->id)))) &&
        fix->type == FB_TYPE_PACKED_PIXELS && fix->visual == FB_VISUAL_TRUECOLOR &&
        fix->line_length >= WIDTH * 2 && fix->line_length <= 4096 &&
        fix->smem_len >= fix->line_length * HEIGHT;
}

/* Byte offset of screen pixel (x, y) in the framebuffer. */
static size_t panel_offset(unsigned x, unsigned y, size_t stride)
{
    return (size_t)y * stride + (size_t)x * 2;
}

static void put_pixel(uint8_t *pixels, size_t offset, uint16_t color)
{
    pixels[offset] = (uint8_t)color;
    pixels[offset + 1] = (uint8_t)(color >> 8);
}

static uint16_t get_pixel(const uint8_t *pixels, size_t offset)
{
    return (uint16_t)(pixels[offset] | pixels[offset + 1] << 8);
}

/* Copy the canvas into the framebuffer layout; bytes past each row stay. */
static void frame_to_panel(uint8_t *pixels, size_t stride, const uint16_t *frame)
{
    for (unsigned y = 0; y < HEIGHT; y++)
        for (unsigned x = 0; x < WIDTH; x++)
            put_pixel(pixels, panel_offset(x, y, stride), frame[y * WIDTH + x]);
}

static void panel_to_frame(uint16_t *frame, const uint8_t *pixels, size_t stride)
{
    for (unsigned y = 0; y < HEIGHT; y++)
        for (unsigned x = 0; x < WIDTH; x++)
            frame[y * WIDTH + x] = get_pixel(pixels, panel_offset(x, y, stride));
}

/* Framebuffer rows holding the boot bar. */
static size_t boot_bar_band(size_t stride, size_t *size)
{
    *size = (size_t)BAR_HEIGHT * stride;
    return (size_t)BOOT_BAR_Y * stride;
}

/* Returns 0 when the panel shows no boot bar or the bar is already further. */
static int update_boot_bar(uint8_t *pixels, size_t stride, uint16_t *frame, unsigned progress)
{
    panel_to_frame(frame, pixels, stride);
    if (!has_boot_bar(frame) ||
        percent_width(progress) <= boot_bar_row_fill(frame + (BOOT_BAR_Y + 1) * WIDTH))
        return 0;
    draw_boot_bar(frame, progress);
    frame_to_panel(pixels, stride, frame);
    return 1;
}

/* Rebuild the bar's middle row from the framebuffer. */
static void panel_bar_row(uint16_t *row, const uint8_t *panel, size_t stride)
{
    for (unsigned x = BOOT_BAR_X - 1; x <= BOOT_BAR_X + BOOT_BAR_WIDTH; x++)
        row[x] = get_pixel(panel, panel_offset(x, BOOT_BAR_Y + 1, stride));
}

/* Paint bar column x on the framebuffer for a bar `fill` pixels wide. */
static void paint_bar_column(uint8_t *panel, size_t stride, unsigned x, unsigned fill)
{
    for (unsigned y = 0; y < BAR_HEIGHT; y++)
        put_pixel(panel, panel_offset(BOOT_BAR_X + x, BOOT_BAR_Y + y, stride),
                  boot_bar_pixel(x, y, fill));
}

static int open_panel(const char *path, struct fb_fix_screeninfo *fix)
{
    struct fb_var_screeninfo var;
    int fd = open(path, O_RDWR | O_CLOEXEC);

    if (fd < 0)
        return -1;
    if (ioctl(fd, FBIOGET_VSCREENINFO, &var) < 0 ||
        ioctl(fd, FBIOGET_FSCREENINFO, fix) < 0)
        goto failed;
    if (!valid_panel(&var, fix)) {
        errno = ENOTSUP;
        goto failed;
    }
    return fd;
failed:
    {
        int saved = errno;
        close(fd);
        errno = saved;
    }
    return -1;
}

/* Write a whole frame; the panel driver resends the full panel afterwards. */
static int write_panel(int fd, const uint8_t *pixels, size_t size)
{
    size_t done = 0;
    while (done < size) {
        ssize_t n = pwrite(fd, pixels + done, size - done, (off_t)done);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            if (!n)
                errno = EIO;
            return -1;
        }
        done += (size_t)n;
    }
    return 0;
}

/*
 * Bar updates go through a shared mapping: the panel driver then sends only the
 * touched pages, while write() always resends the whole panel over SPI.
 */
static uint8_t *map_panel(int fd, size_t stride)
{
    void *map = mmap(NULL, stride * HEIGHT, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    return map == MAP_FAILED ? NULL : map;
}

/* Copy the rows of [offset, offset + size) that differ from the panel. */
static void copy_changed_rows(uint8_t *panel, const uint8_t *pixels, size_t offset, size_t size,
                              size_t stride)
{
    for (size_t at = offset; at < offset + size; at += stride)
        if (memcmp(panel + at, pixels + at, stride))
            memcpy(panel + at, pixels + at, stride);
}

/* Bar writers serialize their read-modify-write cycles on the framebuffer. */
static int lock_panel(int fd)
{
    struct flock lock = { .l_type = F_WRLCK, .l_whence = SEEK_SET };
    while (fcntl(fd, F_SETLKW, &lock) < 0)
        if (errno != EINTR)
            return -1;
    return 0;
}

/* progress < 0 draws the whole frame; otherwise only an existing boot bar changes. */
static int show_panel(const char *path, uint16_t *frame, int progress)
{
    struct fb_fix_screeninfo fix;
    uint8_t *pixels;
    size_t size;
    int result = -1;
    int fd = open_panel(path, &fix);

    if (fd < 0)
        return -1;
    size = (size_t)fix.line_length * HEIGHT;
    pixels = calloc(1, size);
    if (!pixels)
        goto close_panel;
    if (progress < 0) {
        frame_to_panel(pixels, fix.line_length, frame);
        /* The lock keeps the animator from painting over this frame. */
        if (lock_panel(fd) < 0 || write_panel(fd, pixels, size) < 0)
            goto free_pixels;
        /* Leave the last frame visible; the normal screen service takes over. */
        (void)ioctl(fd, FBIOBLANK, FB_BLANK_UNBLANK);
    } else {
        size_t band, first = boot_bar_band(fix.line_length, &band);
        uint8_t *panel = map_panel(fd, fix.line_length);
        if (!panel)
            goto free_pixels;
        if (lock_panel(fd) < 0) {
            munmap(panel, size);
            goto free_pixels;
        }
        memcpy(pixels, panel, size);
        if (update_boot_bar(pixels, fix.line_length, frame, (unsigned)progress))
            copy_changed_rows(panel, pixels, first, band, fix.line_length);
        munmap(panel, size);
    }
    result = 0;
free_pixels:
    free(pixels);
close_panel:
    {
        int saved = errno;
        close(fd);
        errno = saved;
    }
    return result;
}

static int write_preview(const char *path, const uint16_t *frame)
{
    FILE *file = fopen(path, "wb");
    if (!file)
        return -1;
    if (fprintf(file, "P6\n%d %d\n255\n", WIDTH, HEIGHT) < 0)
        goto failed;
    for (unsigned i = 0; i < WIDTH * HEIGHT; i++) {
        unsigned r = (frame[i] >> 11) & 31, g = (frame[i] >> 5) & 63;
        unsigned b = frame[i] & 31;
        uint8_t rgb[] = {
            (uint8_t)((r << 3) | (r >> 2)), (uint8_t)((g << 2) | (g >> 4)),
            (uint8_t)((b << 3) | (b >> 2)),
        };
        if (fwrite(rgb, 1, sizeof(rgb), file) != sizeof(rgb))
            goto failed;
    }
    return fclose(file);
failed:
    fclose(file);
    return -1;
}

static volatile sig_atomic_t stop_animation;

static void request_stop(int signal)
{
    (void)signal;
    stop_animation = 1;
}

static unsigned long monotonic_ms(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (unsigned long)now.tv_sec * 1000 + (unsigned long)now.tv_nsec / 1000000;
}

/* Paint columns [from, fill) of a bar `fill` pixels wide on the framebuffer. */
static void paint_bar(uint8_t *panel, size_t stride, unsigned from, unsigned fill)
{
    for (unsigned x = from; x < fill; x++)
        paint_bar_column(panel, stride, x, fill);
}

/* Keep lengthening the bar on `fd` (a framebuffer with `stride`) until told to stop. */
static int animate_fd(int fd, size_t stride, unsigned long lifetime_ms)
{
    enum { TICK_MS = 80 };
    uint16_t row[WIDTH];
    uint8_t *panel = map_panel(fd, stride);
    unsigned long started = monotonic_ms(), stage_started = started;
    int stage = -2;

    if (!panel)
        return 1;
    while (!stop_animation && monotonic_ms() - started < lifetime_ms) {
        struct timespec tick = { 0, TICK_MS * 1000000L };
        struct flock unlock = { .l_type = F_UNLCK, .l_whence = SEEK_SET };
        unsigned fill, target;
        int current;

        if (lock_panel(fd) < 0)
            break;
        panel_bar_row(row, panel, stride);
        if (!boot_bar_row_valid(row))
            break;
        fill = boot_bar_row_fill(row);
        current = boot_stage(fill);
        if (current != stage) {
            stage = current;
            stage_started = monotonic_ms();
        }
        if (stage >= BOOT_STAGES - 1)
            break;
        target = stage < 0 ? fill : boot_stage_target(stage, monotonic_ms() - stage_started);
        /* Repaint the old rounded end too, then the newly filled columns. */
        if (target > fill)
            paint_bar(panel, stride, fill > 2 ? fill - 2 : 0, target);
        (void)fcntl(fd, F_SETLK, &unlock);
        nanosleep(&tick, NULL);
    }
    munmap(panel, stride * HEIGHT);
    return 0;
}

/*
 * Started by the initramfs once its boot screen is up; it survives switch_root
 * and keeps the bar moving until `finish`, until the bar disappears, or for at
 * most two minutes. It only ever lengthens the fill.
 */
static int animate(const char *device)
{
    struct fb_fix_screeninfo fix;
    struct sigaction action = { .sa_handler = request_stop };
    int fd, result;

    (void)setsid();
    signal(SIGHUP, SIG_IGN);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    fd = open_panel(device, &fix);
    if (fd < 0)
        return 1;
    result = animate_fd(fd, fix.line_length, 120000);
    close(fd);
    return result;
}

static int animation_process(pid_t pid)
{
    char path[64], args[256];
    ssize_t length;
    int fd;
    const char *name;

    snprintf(path, sizeof(path), "/proc/%d/cmdline", (int)pid);
    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return 0;
    length = read(fd, args, sizeof(args) - 1);
    close(fd);
    if (length <= 0)
        return 0;
    args[length] = '\0';
    name = strrchr(args, '/') ? strrchr(args, '/') + 1 : args;
    if (strcmp(name, "onekvm-boot-splash"))
        return 0;
    for (ssize_t i = (ssize_t)strlen(args) + 1; i < length; i += (ssize_t)strlen(args + i) + 1)
        if (!strcmp(args + i, "animate"))
            return 1;
    return 0;
}

static int process_running(pid_t pid)
{
    char path[64], stat[128];
    ssize_t length;
    const char *state;
    int fd;

    snprintf(path, sizeof(path), "/proc/%d/stat", (int)pid);
    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return 0;
    length = read(fd, stat, sizeof(stat) - 1);
    close(fd);
    if (length <= 0)
        return 0;
    stat[length] = '\0';
    state = strrchr(stat, ')');
    return state && state[1] == ' ' && state[2] != 'Z' && state[2] != 'X';
}

/* Stop `animate` before the caller draws its own first frame. */
static int finish(void)
{
    enum { MAX = 8 };
    pid_t pids[MAX];
    unsigned count = 0;
    struct dirent *entry;
    DIR *proc = opendir("/proc");

    if (!proc)
        return 1;
    while ((entry = readdir(proc)) && count < MAX) {
        char *end;
        long pid = strtol(entry->d_name, &end, 10);
        if (*end || pid <= 0 || pid == (long)getpid() || !animation_process((pid_t)pid))
            continue;
        if (!kill((pid_t)pid, SIGTERM))
            pids[count++] = (pid_t)pid;
    }
    closedir(proc);
    /* Wait up to half a second so no bar write can follow the caller's frame. */
    for (unsigned tries = 0; count && tries < 50; tries++) {
        struct timespec pause = { 0, 10 * 1000000L };
        unsigned running = 0;
        for (unsigned i = 0; i < count; i++)
            if (process_running(pids[i]))
                pids[running++] = pids[i];
        count = running;
        if (count)
            nanosleep(&pause, NULL);
    }
    return count ? 1 : 0;
}

/* A percentage, or the name of a boot checkpoint. */
static int parse_percent(const char *text)
{
    int value = 0;
    for (int i = 0; i < BOOT_STAGES; i++)
        if (!strcmp(text, boot_stages[i].name))
            return boot_stages[i].percent;
    if (!*text || strlen(text) > 3)
        return -1;
    for (; *text; text++) {
        if (*text < '0' || *text > '9')
            return -1;
        value = value * 10 + (*text - '0');
    }
    return value <= 100 ? value : -1;
}

int main(int argc, char **argv)
{
    uint16_t frame[WIDTH * HEIGHT];
    const char *preview = NULL, *device = "/dev/fb0", *message = NULL;
    int progress = 0, bar_only = 0, text_screen = 0;

    while (argc >= 3) {
        if (!strcmp(argv[1], "--output"))
            preview = argv[2];
        else if (!strcmp(argv[1], "--framebuffer"))
            device = argv[2];
        else if (!strcmp(argv[1], "--message"))
            message = argv[2];
        else if (!strcmp(argv[1], "--progress") && (progress = parse_percent(argv[2])) >= 0)
            ;
        else
            break;
        argc -= 2;
        argv += 2;
    }
    if (argc == 2 && !strcmp(argv[1], "animate") && !preview)
        return animate(device);
    if (argc == 2 && !strcmp(argv[1], "finish"))
        return finish();
    if (argc == 3 && !strcmp(argv[1], "progress") && !preview &&
        (progress = parse_percent(argv[2])) >= 0) {
        bar_only = 1;
    } else if (argc == 2 && !strcmp(argv[1], "recovery")) {
        text_screen = 1;
    } else if (argc == 2 && !strcmp(argv[1], "message") && message && *message) {
        /* The Recovery layout with other text, e.g. while the system installs. */
        text_screen = 1;
    } else if (!(argc == 2 && !strcmp(argv[1], "boot"))) {
        fprintf(stderr, "Usage: onekvm-boot-splash [--progress PERCENT|CHECKPOINT] "
                "[--output FILE.ppm | --framebuffer /dev/fb0] boot\n"
                "       onekvm-boot-splash [--message TEXT] "
                "[--output FILE.ppm | --framebuffer /dev/fb0] recovery\n"
                "       onekvm-boot-splash --message TEXT "
                "[--output FILE.ppm | --framebuffer /dev/fb0] message\n"
                "       onekvm-boot-splash [--framebuffer /dev/fb0] {progress PERCENT|CHECKPOINT | animate}\n"
                "       onekvm-boot-splash finish\n");
        return 2;
    }
    if (text_screen)
        render_recovery(frame, message);
    else if (!bar_only)
        render(frame, (unsigned)progress);
    if ((preview ? write_preview(preview, frame) : show_panel(device, frame, bar_only ? progress : -1)) < 0) {
        perror("onekvm-boot-splash");
        return 1;
    }
    return 0;
}
