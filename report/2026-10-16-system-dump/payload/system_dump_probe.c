/* A7C2 read-only system-state probe, built as an LD_PRELOAD constructor.
 *
 * The camera's Jiritsu DEBUG path starts a child whose argv is exactly
 * "/bin/sh", "-c", "/bin/date -s 070421422017.26". A glibc preload entry makes
 * this constructor run in that child before the shell reaches main(). The probe
 * acts only when /proc/self/cmdline matches those three fields, removes the
 * preload entry first to shorten the exposure window, performs bounded
 * read-only work, writes one result record, then exits so the fixed date
 * command never runs.
 *
 * No device writes, no shell, no network. Camera paths are fixed; the
 * A7C2_PROBE_ROOT variable exists only to run this file under host tests.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef BLKGETSIZE
#define BLKGETSIZE 0x1260
#endif

#define PREFIX_MAX 4096U
#define TEXT_MAX (8U * 1024U)
#define OUT_MAX (96U * 1024U)
#define RESULT_PATH "/tmp/jiritsu/a7c2-probe-result.bin"
#define PRELOAD_PATH "/etc/ld.so.preload"
#define REPORT_MAGIC "A7C2-PROBE-1"

static const char debug_argv[] = "/bin/sh\0-c\0/bin/date -s 070421422017.26\0";
static const char *const targets[] = {"/dev/nflasha7", "/dev/nflasha15"};
static const char *const texts[] = {
    "/proc/mounts", "/etc/fstab", "/etc/fstab.ext", "/etc/ld.so.preload"
};

struct buffer {
    char data[OUT_MAX];
    size_t len;
    int overflow;
};

static void put(struct buffer *out, const char *text, size_t size)
{
    if (out->overflow) return;
    if (out->len + size > sizeof(out->data)) {
        out->overflow = 1;
        return;
    }
    memcpy(out->data + out->len, text, size);
    out->len += size;
}

static void put_text(struct buffer *out, const char *text)
{
    put(out, text, strlen(text));
}

static void put_u64(struct buffer *out, unsigned long long value)
{
    char digits[24];
    size_t used = 0;
    do {
        digits[used++] = (char)('0' + (int)(value % 10ULL));
        value /= 10ULL;
    } while (value != 0ULL);
    for (size_t i = 0; i < used / 2; i++) {
        char swap = digits[i];
        digits[i] = digits[used - 1 - i];
        digits[used - 1 - i] = swap;
    }
    put(out, digits, used);
}

static void put_i64(struct buffer *out, long long value)
{
    if (value < 0) {
        put_text(out, "-");
        put_u64(out, (unsigned long long)(-(value + 1)) + 1ULL);
        return;
    }
    put_u64(out, (unsigned long long)value);
}

static void put_hex(struct buffer *out, const char *data, size_t size)
{
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < size; i++) {
        unsigned char byte = (unsigned char)data[i];
        char pair[2] = {hex[byte >> 4], hex[byte & 0x0fU]};
        put(out, pair, sizeof(pair));
        if (out->overflow) return;
    }
}

/* Camera paths stay literal; host tests may relocate them under a temp root. */
static void resolve_path(char *out, size_t size, const char *path)
{
    const char *root = getenv("A7C2_PROBE_ROOT");
    if (root == NULL || *root == '\0' || strncmp(path, "/proc/", 6) == 0) {
        size_t need = strlen(path);
        if (need + 1 > size) {
            out[0] = '\0';
            return;
        }
        memcpy(out, path, need + 1);
        return;
    }
    size_t root_len = strlen(root);
    size_t path_len = strlen(path);
    if (root_len + path_len + 1 > size) {
        out[0] = '\0';
        return;
    }
    memcpy(out, root, root_len);
    memcpy(out + root_len, path, path_len + 1);
}

static int read_bounded(int fd, char *out, size_t size, size_t *used)
{
    size_t total = 0;
    while (total < size) {
        ssize_t got = read(fd, out + total, size - total);
        if (got < 0) return -1;
        if (got == 0) break;
        total += (size_t)got;
    }
    *used = total;
    return 0;
}

static int write_all(int fd, const char *data, size_t size)
{
    size_t done = 0;
    while (done < size) {
        ssize_t wrote = write(fd, data + done, size - done);
        if (wrote <= 0) return -1;
        done += (size_t)wrote;
    }
    return 0;
}

/* Match the single documented DEBUG command line, byte for byte. */
static int gate_matches(void)
{
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    char data[256];
    size_t used = 0;
    int failed = read_bounded(fd, data, sizeof(data), &used);
    close(fd);
    if (failed) return 0;
    if (used != sizeof(debug_argv) - 1) return 0;
    return memcmp(data, debug_argv, used) == 0;
}

static void report_text(struct buffer *out, const char *path)
{
    char resolved[1024];
    resolve_path(resolved, sizeof(resolved), path);
    put_text(out, "TEXT\t");
    put_text(out, resolved);
    int fd = resolved[0] == '\0' ? -1 : open(resolved, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        put_text(out, "\tOPEN_ERROR\t");
        put_i64(out, errno);
        put_text(out, "\n");
        return;
    }
    static char data[TEXT_MAX];
    size_t used = 0;
    int failed = read_bounded(fd, data, sizeof(data), &used);
    close(fd);
    if (failed) {
        put_text(out, "\tREAD_ERROR\t");
        put_i64(out, errno);
        put_text(out, "\n");
        return;
    }
    put_text(out, "\tSIZE\t");
    put_u64(out, used);
    put_text(out, "\tCONTENT_HEX\t");
    put_hex(out, data, used);
    put_text(out, "\n");
}

static void report_block(struct buffer *out, const char *path)
{
    char resolved[1024];
    resolve_path(resolved, sizeof(resolved), path);
    put_text(out, "BLOCK\t");
    put_text(out, resolved);
    int fd = resolved[0] == '\0' ? -1 : open(resolved, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        put_text(out, "\tOPEN_ERROR\t");
        put_i64(out, errno);
        put_text(out, "\n");
        return;
    }
    unsigned long sectors = 0;
    int ioctl_error = ioctl(fd, BLKGETSIZE, &sectors) == 0 ? 0 : errno;
    static char prefix[PREFIX_MAX];
    ssize_t got = read(fd, prefix, sizeof(prefix));
    int read_error = got < 0 ? errno : 0;
    close(fd);
    put_text(out, "\tBLKGETSIZE_ERROR\t");
    put_i64(out, ioctl_error);
    put_text(out, "\tSECTORS\t");
    put_u64(out, sectors);
    put_text(out, "\tCAPACITY_BYTES\t");
    put_u64(out, (unsigned long long)sectors * 512ULL);
    put_text(out, "\tREAD_ERROR\t");
    put_i64(out, read_error);
    put_text(out, "\tREAD_BYTES\t");
    put_i64(out, got < 0 ? 0 : (long long)got);
    if (got > 0) {
        put_text(out, "\tPREFIX_HEX\t");
        put_hex(out, prefix, (size_t)got);
    }
    put_text(out, "\n");
}

__attribute__((constructor)) static void a7c2_probe(void)
{
    if (!gate_matches()) return;

    struct buffer out;
    memset(&out, 0, sizeof(out));
    put_text(&out, REPORT_MAGIC "\n");
    put_text(&out, "PROBE\tuid=");
    put_u64(&out, (unsigned long long)getuid());
    put_text(&out, "\teuid=");
    put_u64(&out, (unsigned long long)geteuid());
    put_text(&out, "\tgid=");
    put_u64(&out, (unsigned long long)getgid());
    put_text(&out, "\tegid=");
    put_u64(&out, (unsigned long long)getegid());
    put_text(&out, "\n");

    /* Clear the gate before the longer work so later execs are unaffected. */
    char preload[1024];
    resolve_path(preload, sizeof(preload), PRELOAD_PATH);
    if (preload[0] != '\0') unlink(preload);

    for (size_t i = 0; i < sizeof(targets) / sizeof(targets[0]); i++) report_block(&out, targets[i]);
    for (size_t i = 0; i < sizeof(texts) / sizeof(texts[0]); i++) report_text(&out, texts[i]);

    char result[1024];
    resolve_path(result, sizeof(result), RESULT_PATH);
    int fd = result[0] == '\0' ? -1 : open(result, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        /* The buffer always holds a valid prefix; overflow only truncates the tail. */
        write_all(fd, out.data, out.len);
        if (out.overflow) write_all(fd, "TRUNCATED\n", 10);
        close(fd);
    }
    /* Exit before the fixed date command runs, so the camera clock is not set. */
    _exit(0);
}
